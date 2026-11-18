"""Instagram connect + Reels publishing tests (httpx MockTransport for graph + rupload, no network)."""
import json
import logging
import os
import stat
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import select

from app import config
from app.db import get_engine, get_session, init_db
from app.db.enums import CampaignStatus, ClipStatus, Platform, PostStatus, SourceKind, SourceStatus
from app.db.models import Account, Campaign, Clip, Post, Source
from app.uploader import instagram_auth
from app.uploader.common import AuthError
from app.uploader.instagram import upload_instagram
from app.uploader.instagram_auth import connect_instagram, debug_token

SHORT, LONG, PAGE_TOKEN, APP_SECRET = "EAAshort-SECRET", "EAAlong-SECRET", "EAApage-SECRET-TOKEN", "app-secret-XYZ"
NOW = datetime(2026, 11, 18, 10, 0, tzinfo=timezone.utc)
V = "v26.0"


class FakeMeta:
    """Programmable Graph API + rupload endpoint."""

    def __init__(self, pages=None, statuses=("IN_PROGRESS", "FINISHED"), quota=(0, 100), errors=None,
                 rupload_fail_first=False):
        self.pages = pages if pages is not None else [
            {"id": "P1", "name": "Clip Page", "access_token": PAGE_TOKEN,
             "instagram_business_account": {"id": "17841400000000001", "username": "clipper"}}]
        self.statuses, self.quota = list(statuses), quota
        self.errors = errors or {}  # path suffix -> (status, error dict)
        self.rupload_fail_first = rupload_fail_first
        self.requests: list[httpx.Request] = []
        self.uploaded = b""

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path, host = request.url.path, request.url.host
        if host == "rupload.facebook.com":
            if self.rupload_fail_first:
                self.rupload_fail_first = False
                raise httpx.ConnectError("connection reset", request=request)
            self.uploaded = request.read()
            return httpx.Response(200, json={"success": True})
        for suffix, (status, err) in self.errors.items():
            if path.endswith(suffix):
                return httpx.Response(status, json={"error": err})
        if path == f"/{V}/oauth/access_token":
            return httpx.Response(200, json={"access_token": LONG, "token_type": "bearer"})
        if path == f"/{V}/me/accounts":
            return httpx.Response(200, json={"data": self.pages})
        if path == f"/{V}/debug_token":
            return httpx.Response(200, json={"data": {"is_valid": True, "expires_at": 0,
                                                      "scopes": ["instagram_content_publish", "pages_show_list"]}})
        if path.endswith("/content_publishing_limit"):
            return httpx.Response(200, json={"data": [{"quota_usage": self.quota[0],
                                                       "config": {"quota_total": self.quota[1], "quota_duration": 86400}}]})
        if path.endswith("/media") and request.method == "POST":
            return httpx.Response(200, json={"id": "C123"})
        if path == f"/{V}/C123":
            return httpx.Response(200, json={"status_code": self.statuses.pop(0) if self.statuses else "IN_PROGRESS",
                                             "status": "Error: video codec not supported"})
        if path.endswith("/media_publish"):
            return httpx.Response(200, json={"id": "M999"})
        if path == f"/{V}/M999":
            return httpx.Response(200, json={"permalink": "https://www.instagram.com/reel/ABC/",
                                             "media_product_type": "REELS"})
        return httpx.Response(404, json={"error": {"message": f"unexpected {path}", "code": 100}})


@pytest.fixture
def env(tmp_path, monkeypatch):
    for key, val in {"SECRETS_DIR": str(tmp_path / "secrets"), "META_APP_ID": "123", "META_APP_SECRET": APP_SECRET,
                     "META_GRAPH_VERSION": V, "INSTAGRAM_DAILY_CAP": 20, "INSTAGRAM_SHARE_TO_FEED": True}.items():
        monkeypatch.setattr(config.settings, key, val)
    notes: list = []
    monkeypatch.setattr(instagram_auth, "notify", lambda *a, **k: notes.append(a))
    eng = init_db(get_engine(str(tmp_path / "ig.db")))
    video = tmp_path / "clip.mp4"
    video.write_bytes(os.urandom(3 * 1024 * 1024 + 17))
    with get_session(eng) as s:
        s.add(Campaign(id=1, external_id="c", name="C", url="u", status=CampaignStatus.joined))
        s.add(Source(id=1, campaign_id=1, url="https://a.test/1.mp4", kind=SourceKind.direct,
                     status=SourceStatus.processed))
    return {"engine": eng, "video": video, "notes": notes, "tmp": tmp_path}


def client_for(fake):
    return httpx.Client(transport=httpx.MockTransport(fake))


def connect(env, fake=None, hint=None):
    return connect_instagram(SHORT, hint, engine=env["engine"], client=client_for(fake or FakeMeta()))


def add_clip(env, **kw) -> int:
    with get_session(env["engine"]) as s:
        c = Clip(campaign_id=1, source_id=1, start_sec=0, end_sec=20, hook_title="Hook", hashtags=["#brand"],
                 status=kw.pop("status", ClipStatus.approved), file_path=str(env["video"]),
                 compliance={"ok": True, "errors": [], "warnings": []},
                 platform_captions={"instagram": {"platform": "instagram", "caption": "Hook #brand #ad"}}, **kw)
        s.add(c)
        s.flush()
        return c.id


def upload(env, clip_id, account_id, fake, now=NOW):
    return upload_instagram(clip_id, account_id, engine=env["engine"], client=client_for(fake),
                            sleep=lambda s: None, now=now)


def get(env, model, rid):
    with get_session(env["engine"]) as s:
        return s.get(model, rid)


# --- connect -----------------------------------------------------------------------------

def test_connect_saves_private_token_and_upserts(env):
    fake = FakeMeta()
    acct = connect(env, fake)
    assert (acct.handle, acct.channel_id, acct.display_name) == ("clipper", "17841400000000001", "Clip Page")
    assert acct.credentials_ref == "instagram_17841400000000001.json"
    path = env["tmp"] / "secrets" / acct.credentials_ref
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    data = json.loads(path.read_text())
    assert data["page_token"] == PAGE_TOKEN and data["ig_username"] == "clipper" and data["page_id"] == "P1"
    exchange = fake.requests[0].url.params
    assert exchange["grant_type"] == "fb_exchange_token" and exchange["fb_exchange_token"] == SHORT
    assert fake.requests[1].headers["authorization"] == f"OAuth {LONG}"
    again = connect(env)
    with get_session(env["engine"]) as s:
        assert len(s.scalars(select(Account)).all()) == 1 and again.id == acct.id


def test_connect_page_choice_errors(env):
    two = [{"id": "P1", "name": "Page One", "access_token": "t1",
            "instagram_business_account": {"id": "1", "username": "one"}},
           {"id": "P2", "name": "Page Two", "access_token": "t2",
            "instagram_business_account": {"id": "2", "username": "two"}},
           {"id": "P3", "name": "No IG", "access_token": "t3"}]
    with pytest.raises(AuthError) as err:
        connect(env, FakeMeta(pages=two))
    assert "Page One (@one)" in str(err.value) and "Page Two (@two)" in str(err.value) and "t1" not in str(err.value)
    assert connect(env, FakeMeta(pages=two), hint="@two").handle == "two"
    assert connect(env, FakeMeta(pages=two), hint="Page One").handle == "one"
    with pytest.raises(AuthError, match="no Facebook Page with a linked Instagram"):
        connect(env, FakeMeta(pages=[two[2]]))


def test_debug_token(env):
    acct = connect(env)
    info = debug_token(acct, env["engine"], client=client_for(FakeMeta()))
    assert info["is_valid"] and "instagram_content_publish" in info["scopes"]

    class Invalid(FakeMeta):
        def __call__(self, request):
            if request.url.path.endswith("/debug_token"):
                return httpx.Response(200, json={"data": {"is_valid": False}})
            return super().__call__(request)

    with pytest.raises(AuthError):
        debug_token(acct, env["engine"], client=client_for(Invalid()))
    assert get(env, Account, acct.id).auth_error == "reconnect needed" and len(env["notes"]) == 1


# --- upload --------------------------------------------------------------------------------

def test_upload_happy_path(env, caplog):
    acct, cid = connect(env), add_clip(env)
    fake = FakeMeta()
    with caplog.at_level(logging.DEBUG):
        post, msg = upload(env, cid, acct.id, fake)
    assert post.status == PostStatus.posted and post.post_url == "https://www.instagram.com/reel/ABC/"
    assert post.platform_post_id == "M999" and post.visibility == "public"
    assert get(env, Clip, cid).status == ClipStatus.posted
    media = next(r for r in fake.requests if r.url.path.endswith("/media") and r.method == "POST")
    form = dict(x.split("=", 1) for x in media.content.decode().split("&"))
    assert form["media_type"] == "REELS" and form["upload_type"] == "resumable" and form["share_to_feed"] == "true"
    assert form["thumb_offset"] == "1000" and "Hook" in httpx.QueryParams(media.content.decode())["caption"]
    rup = next(r for r in fake.requests if r.url.host == "rupload.facebook.com")
    assert str(rup.url) == f"https://rupload.facebook.com/ig-api-upload/{V}/C123"
    assert rup.headers["authorization"] == f"OAuth {PAGE_TOKEN}" and rup.headers["offset"] == "0"
    assert rup.headers["file_size"] == str(env["video"].stat().st_size)
    assert fake.uploaded == env["video"].read_bytes()
    assert sum(1 for r in fake.requests if r.url.path == f"/{V}/C123") == 2
    for secret in (SHORT, LONG, PAGE_TOKEN, APP_SECRET):
        assert secret not in caplog.text and secret not in msg


def test_container_error_and_timeout(env):
    acct = connect(env)
    post, msg = upload(env, add_clip(env), acct.id, FakeMeta(statuses=["IN_PROGRESS", "ERROR"]))
    assert post.status == PostStatus.failed and "container error: Error: video codec not supported" in post.error
    post, msg = upload(env, add_clip(env), acct.id, FakeMeta(statuses=[]))
    assert post.status == PostStatus.failed and post.error == "instagram: processing timeout"


def test_publish_limit_refused_before_container(env):
    acct, cid = connect(env), add_clip(env)
    fake = FakeMeta(quota=(25, 25))
    assert upload(env, cid, acct.id, fake) == (None, "instagram publish limit reached")
    assert not any(r.url.path.endswith("/media") for r in fake.requests)
    with get_session(env["engine"]) as s:
        assert s.scalar(select(Post)) is None


def test_error_190_marks_reconnect_once(env):
    acct = connect(env)
    fake = FakeMeta(errors={"/media": (400, {"message": "Error validating access token", "code": 190})})
    post, msg = upload(env, add_clip(env), acct.id, fake)
    assert post.status == PostStatus.failed and msg.startswith("auth:")
    assert get(env, Account, acct.id).auth_error == "reconnect needed" and len(env["notes"]) == 1
    post, msg = upload(env, add_clip(env), acct.id, FakeMeta())
    assert post is None and "reconnect needed" in msg and len(env["notes"]) == 1


def test_2207042_blocks_account_for_24h(env):
    acct = connect(env)
    fake = FakeMeta(errors={"/media_publish": (400, {"message": "limit", "code": 9, "error_subcode": 2207042})})
    post, msg = upload(env, add_clip(env), acct.id, fake)
    assert post.status == PostStatus.failed and post.error == "rate limited (account blocked for 24h)"
    nxt = FakeMeta()
    post, msg = upload(env, add_clip(env), acct.id, nxt)
    assert post is None and "rate-limited until" in msg and nxt.requests == []
    post, _ = upload(env, add_clip(env), acct.id, FakeMeta(), now=NOW + timedelta(hours=25))
    assert post.status == PostStatus.posted


def test_api_rate_codes(env):
    acct = connect(env)
    fake = FakeMeta(errors={"/media": (400, {"message": "Application request limit reached", "code": 4})})
    post, msg = upload(env, add_clip(env), acct.id, fake)
    assert post.error == "api rate limit, retry later"


def test_rupload_network_error_retried_once(env):
    acct = connect(env)
    fake = FakeMeta(rupload_fail_first=True)
    post, _ = upload(env, add_clip(env), acct.id, fake)
    assert post.status == PostStatus.posted
    assert sum(1 for r in fake.requests if r.url.host == "rupload.facebook.com") == 2


def test_shared_preconditions(env):
    acct = connect(env)
    fake = FakeMeta()
    rendered = add_clip(env, status=ClipStatus.rendered)
    assert "not approved" in upload(env, rendered, acct.id, fake)[1]
    done = add_clip(env)
    assert upload(env, done, acct.id, fake)[0].status == PostStatus.posted
    with get_session(env["engine"]) as s:
        s.get(Clip, done).status = ClipStatus.approved
    assert "already posted to clipper" in upload(env, done, acct.id, FakeMeta())[1]
    config.settings.INSTAGRAM_DAILY_CAP = 1
    try:
        assert "daily Instagram upload cap reached (1)" in upload(env, add_clip(env), acct.id, FakeMeta())[1]
    finally:
        config.settings.INSTAGRAM_DAILY_CAP = 20


def test_posts_page_shows_permalink(env, monkeypatch):
    from fastapi.testclient import TestClient

    from app.dashboard.app import create_app

    acct = connect(env)
    upload(env, add_clip(env), acct.id, FakeMeta())
    monkeypatch.setattr(config.settings, "DASHBOARD_PASSWORD", "pw")
    client = TestClient(create_app(env["engine"]))
    client.auth = (config.settings.DASHBOARD_USER, "pw")
    page = client.get("/posts", params={"tab": "posted"}).text
    assert "https://www.instagram.com/reel/ABC/" in page and "permalink" in page and PAGE_TOKEN not in page
    approved = add_clip(env)
    assert "Upload to Instagram now" in client.get(f"/clips/{approved}").text
