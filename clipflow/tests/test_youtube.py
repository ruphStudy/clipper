"""YouTube auth + uploader tests: googleapiclient and InstalledAppFlow mocked, no network."""
import json
import logging
import os
import stat
from datetime import datetime, timedelta, timezone

import httplib2
import pytest
from google.auth.exceptions import RefreshError
from google.oauth2.credentials import Credentials
from googleapiclient.errors import HttpError
from sqlalchemy import select

from app import config
from app.db import get_engine, get_session, init_db
from app.db.enums import CampaignStatus, ClipStatus, Platform, PostStatus, SourceKind, SourceStatus
from app.db.models import Account, AppSetting, Campaign, Clip, Post, Source
from app.uploader import youtube, youtube_auth
from app.uploader.youtube import upload_youtube
from app.uploader.youtube_auth import SCOPES, AuthError, authorize, get_credentials

TOKEN, REFRESH, SECRET = "ya29.TOKEN-SECRET-123", "1//REFRESH-SECRET-456", "GOCSPX-CLIENT-SECRET-789"
NOW = datetime(2026, 11, 18, 18, 0, tzinfo=timezone.utc)  # 10:00 PT


def make_creds(expired=False) -> Credentials:
    expiry = datetime.utcnow() + (timedelta(hours=-1) if expired else timedelta(hours=1))
    return Credentials(token=TOKEN, refresh_token=REFRESH, token_uri="https://oauth2.googleapis.com/token",
                       client_id="cid.apps.googleusercontent.com", client_secret=SECRET, scopes=SCOPES, expiry=expiry)


def http_error(status, reason="backendError"):
    return HttpError(httplib2.Response({"status": status}),
                     json.dumps({"error": {"code": status, "errors": [{"reason": reason}]}}).encode())


class FakeRequest:
    def __init__(self, script):
        self.script = list(script)

    def next_chunk(self):
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class FakeYT:
    def __init__(self, chunk_script=None, privacy="public", channel=("UC123", "My Channel"), list_status=None):
        self.chunk_script = chunk_script or [(None, {"id": "vid123"})]
        self.privacy, self.channel, self.list_status = privacy, channel, list_status
        self.inserts: list = []
        self.calls = 0

    def channels(self):
        outer = self

        class C:
            def list(self, **kw):
                class R:
                    def execute(self_inner):
                        return {"items": [{"id": outer.channel[0], "snippet": {"title": outer.channel[1]}}]}
                return R()
        return C()

    def videos(self):
        outer = self

        class V:
            def insert(self, part, body, media_body):
                outer.calls += 1
                outer.inserts.append(body)
                return FakeRequest(outer.chunk_script)

            def list(self, id, part):
                class R:
                    def execute(self_inner):
                        st = outer.list_status or {"privacyStatus": outer.privacy, "uploadStatus": "processed"}
                        return {"items": [{"id": id, "status": st}]}
                return R()
        return V()


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(config.settings, "SECRETS_DIR", str(tmp_path / "secrets"))
    monkeypatch.setattr(config.settings, "YOUTUBE_PRIVACY", "public")
    monkeypatch.setattr(config.settings, "YOUTUBE_DAILY_UPLOAD_CAP", 90)
    secrets_file = tmp_path / "client_secret.json"
    secrets_file.write_text("{}")
    monkeypatch.setattr(config.settings, "YOUTUBE_CLIENT_SECRETS_PATH", str(secrets_file))
    notes: list = []
    monkeypatch.setattr(youtube, "notify", lambda *a, **k: notes.append(a))
    monkeypatch.setattr(youtube_auth, "notify", lambda *a, **k: notes.append(a))
    eng = init_db(get_engine(str(tmp_path / "y.db")))
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"\0" * 2048)
    with get_session(eng) as s:
        s.add(Campaign(id=1, external_id="c", name="C", url="u", status=CampaignStatus.joined))
        s.add(Source(id=1, campaign_id=1, url="https://a.test/1.mp4", kind=SourceKind.direct,
                     status=SourceStatus.processed))
    return {"engine": eng, "video": video, "notes": notes, "tmp": tmp_path}


def add_account(env, **kw) -> int:
    ref = "youtube_UC123.json"
    youtube_auth._write_private(youtube_auth.token_path(ref), make_creds(kw.pop("expired", False)).to_json())
    with get_session(env["engine"]) as s:
        a = Account(platform=Platform.youtube, handle=kw.pop("handle", "@me"), credentials_ref=ref,
                    channel_id="UC123", is_active=True, **kw)
        s.add(a)
        s.flush()
        return a.id


def add_clip(env, status=ClipStatus.approved, compliance=None, path=None) -> int:
    with get_session(env["engine"]) as s:
        c = Clip(campaign_id=1, source_id=1, start_sec=0, end_sec=20, hook_title="Hook", status=status,
                 hashtags=["#brand", "#Ring Drone", "#ad"], file_path=str(path or env["video"]), moment_score=5,
                 compliance=compliance or {"ok": True, "errors": [], "warnings": []},
                 platform_captions={"youtube": {"title": "Wait for it #shorts", "description": "Desc #brand #ad"}})
        s.add(c)
        s.flush()
        return c.id


def run(env, clip_id, account_id, fake, **kw):
    return upload_youtube(clip_id, account_id, engine=env["engine"], service_factory=lambda creds: fake,
                          sleep=lambda s: None, now=kw.pop("now", NOW), **kw)


def get(env, model, rid):
    with get_session(env["engine"]) as s:
        return s.get(model, rid)


# --- auth ---------------------------------------------------------------------------

def test_authorize_saves_private_token_and_upserts(env):
    class Flow:
        def run_local_server(self, **kw):
            assert kw["access_type"] == "offline" and kw["prompt"] == "consent" and kw["port"] == 0
            return make_creds()

    fake = FakeYT()
    acct = authorize("@me", engine=env["engine"], flow_factory=lambda path, scopes: Flow(),
                     build_fn=lambda *a, **k: fake)
    assert acct.credentials_ref == "youtube_UC123.json" and "/" not in acct.credentials_ref
    token = env["tmp"] / "secrets" / "youtube_UC123.json"
    assert stat.S_IMODE(os.stat(token).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(token.parent).st_mode) == 0o700
    assert json.loads(token.read_text())["refresh_token"] == REFRESH
    again = authorize("@renamed", engine=env["engine"], flow_factory=lambda path, scopes: Flow(),
                      build_fn=lambda *a, **k: fake)
    with get_session(env["engine"]) as s:
        rows = s.scalars(select(Account)).all()
    assert len(rows) == 1 and again.id == acct.id and rows[0].handle == "@renamed"
    assert rows[0].display_name == "My Channel" and rows[0].channel_id == "UC123"


def test_get_credentials_refresh_and_failure(env, monkeypatch):
    aid = add_account(env, expired=True)
    refreshed: list = []

    def fake_refresh(self, request):
        refreshed.append(1)
        self.token = "ya29.NEW-TOKEN"
        self.expiry = datetime.utcnow() + timedelta(hours=1)

    monkeypatch.setattr(Credentials, "refresh", fake_refresh)
    acct = get(env, Account, aid)
    creds = get_credentials(acct, env["engine"], request_factory=lambda: None)
    assert refreshed == [1] and creds.token == "ya29.NEW-TOKEN"
    assert json.loads((env["tmp"] / "secrets" / "youtube_UC123.json").read_text())["token"] == "ya29.NEW-TOKEN"

    add_account(env, expired=True, handle="@other")

    def bad_refresh(self, request):
        raise RefreshError("invalid_grant: Token has been expired or revoked.")

    monkeypatch.setattr(Credentials, "refresh", bad_refresh)
    with pytest.raises(AuthError):
        get_credentials(get(env, Account, aid), env["engine"], request_factory=lambda: None)
    with pytest.raises(AuthError):
        get_credentials(get(env, Account, aid), env["engine"], request_factory=lambda: None)
    assert get(env, Account, aid).auth_error == "reauthorize needed"
    assert len(env["notes"]) == 1  # warned once


# --- upload -------------------------------------------------------------------------

def test_upload_success(env, caplog):
    aid, cid = add_account(env), add_clip(env)
    fake = FakeYT()
    with caplog.at_level(logging.DEBUG):
        post, msg = run(env, cid, aid, fake)
    assert post.status == PostStatus.posted and post.post_url == "https://www.youtube.com/shorts/vid123"
    assert post.visibility == "public" and post.error is None and post.posted_at is not None
    body = fake.inserts[0]
    assert body["snippet"]["tags"] == ["brand", "Ring Drone", "ad"] and body["snippet"]["categoryId"] == "24"
    assert body["snippet"]["title"] == "Wait for it #shorts" and body["status"]["privacyStatus"] == "public"
    assert body["status"]["selfDeclaredMadeForKids"] is False and body["status"]["containsSyntheticMedia"] is False
    assert get(env, Clip, cid).status == ClipStatus.posted
    for secret in (TOKEN, REFRESH, SECRET):
        assert secret not in caplog.text and secret not in msg


def test_retry_on_503(env):
    aid, cid = add_account(env), add_clip(env)
    fake = FakeYT(chunk_script=[http_error(503), (None, None), (None, {"id": "vid9"})])
    post, _ = run(env, cid, aid, fake)
    assert post.status == PostStatus.posted and post.platform_post_id == "vid9"


def test_locked_private_detected_and_warned_once_per_day(env):
    aid = add_account(env)
    a, b = add_clip(env), add_clip(env)
    post, _ = run(env, a, aid, FakeYT(privacy="private"))
    assert post.status == PostStatus.posted and post.visibility == "private_locked"
    assert post.error == "locked private: API project not audited"
    run(env, b, aid, FakeYT(privacy="private"))
    assert len(env["notes"]) == 1
    c = add_clip(env)
    run(env, c, aid, FakeYT(privacy="private"), now=NOW + timedelta(days=1))
    assert len(env["notes"]) == 2
    d = add_clip(env)
    post, _ = run(env, d, aid, FakeYT(privacy="private"), privacy="private")
    assert post.visibility == "private" and post.error is None


def test_quota_exceeded_blocks_until_pt_midnight(env):
    aid, a, b = add_account(env), add_clip(env), add_clip(env)
    post, msg = run(env, a, aid, FakeYT(chunk_script=[http_error(403, "quotaExceeded")]))
    assert post.status == PostStatus.failed and "quotaExceeded" in post.error
    assert get(env, Clip, a).status == ClipStatus.approved
    with get_session(env["engine"]) as s:
        until = datetime.fromisoformat(s.scalar(select(AppSetting).where(AppSetting.key == "youtube_quota_block_until")).value)
    assert until == datetime(2026, 11, 19, 8, 0, tzinfo=timezone.utc)  # next midnight PT
    fake = FakeYT()
    post, msg = run(env, b, aid, fake)
    assert post is None and "quota exhausted" in msg and fake.calls == 0
    post, _ = run(env, b, aid, fake, now=until + timedelta(minutes=1))
    assert post.status == PostStatus.posted


def test_preconditions_refuse_without_api_call(env, monkeypatch):
    aid = add_account(env)
    fake = FakeYT()
    bad = add_clip(env, compliance={"ok": False, "errors": ["no audio stream"], "warnings": []})
    assert run(env, bad, aid, fake) == (None, "clip has compliance errors")
    rendered = add_clip(env, status=ClipStatus.rendered)
    assert "not approved" in run(env, rendered, aid, fake)[1]
    missing = add_clip(env, path=env["tmp"] / "nope.mp4")
    assert run(env, missing, aid, fake) == (None, "clip file missing")
    done = add_clip(env)
    assert run(env, done, aid, fake)[0].status == PostStatus.posted
    with get_session(env["engine"]) as s:
        s.get(Clip, done).status = ClipStatus.approved
    assert "already posted" in run(env, done, aid, fake)[1]
    monkeypatch.setattr(config.settings, "YOUTUBE_DAILY_UPLOAD_CAP", 1)
    assert "daily YouTube upload cap" in run(env, add_clip(env), aid, fake)[1]
    with get_session(env["engine"]) as s:
        s.get(Account, aid).auth_error = "reauthorize needed"
    monkeypatch.setattr(config.settings, "YOUTUBE_DAILY_UPLOAD_CAP", 90)
    assert "reauthorize needed" in run(env, add_clip(env), aid, fake)[1]
    assert fake.calls == 1
    with get_session(env["engine"]) as s:
        assert s.scalar(select(Post).where(Post.clip_id == bad)) is None


def test_uploads_today_uses_pt_day(env):
    aid = add_account(env)
    with get_session(env["engine"]) as s:
        c = Clip(campaign_id=1, source_id=1, start_sec=0, end_sec=1, status=ClipStatus.posted, hashtags=[])
        s.add(c)
        s.flush()
        s.add(Post(clip_id=c.id, account_id=aid, platform=Platform.youtube, status=PostStatus.posted,
                   posted_at=datetime(2026, 11, 18, 7, 30, tzinfo=timezone.utc)))  # 23:30 PT the day before
    with get_session(env["engine"]) as s:
        assert youtube.uploads_today(s, NOW) == 0
        assert youtube.uploads_today(s, datetime(2026, 11, 18, 7, 45, tzinfo=timezone.utc)) == 1


def test_tags_capped():
    tags = youtube.tags_from_hashtags([f"#tag{i:03d}longwordxxxxx" for i in range(100)])
    assert len(",".join(tags)) <= 450 and tags[0] == "tag000longwordxxxxx"


def test_posts_page_and_accounts(env, monkeypatch):
    from fastapi.testclient import TestClient

    from app.dashboard.app import create_app

    aid, cid = add_account(env), add_clip(env)
    run(env, cid, aid, FakeYT(privacy="private"))
    monkeypatch.setattr(config.settings, "DASHBOARD_PASSWORD", "pw")
    client = TestClient(create_app(env["engine"]))
    client.auth = (config.settings.DASHBOARD_USER, "pw")
    page = client.get("/posts", params={"tab": "posted"}).text
    assert "locked private" in page and "https://www.youtube.com/shorts/vid123" in page
    assert TOKEN not in page
    settings_page = client.get("/settings").text
    assert "@me" in settings_page and "Active" in settings_page
    client.post(f"/accounts/{aid}/toggle")
    assert get(env, Account, aid).is_active is False
    approved = add_clip(env)
    detail = client.get(f"/clips/{approved}").text
    assert "No active YouTube account" in detail
    client.post(f"/accounts/{aid}/toggle")
    assert "Upload to YouTube now" in client.get(f"/clips/{approved}").text
