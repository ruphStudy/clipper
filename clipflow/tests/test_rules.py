"""Rules model, regex suggestions, captions, and the rules/settings UI."""
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import config
from app.dashboard.app import create_app
from app.db import get_engine, get_session, init_db
from app.db.models import AppSetting, Campaign
from app.rules import Rules, get_rules, validate
from app.rules.caption import check_text, render_caption, render_for_platform
from app.rules.extract import suggest_from_text
from app.rules.model import DEFAULT_TEMPLATE, normalize_banned, normalize_hashtags, normalize_mentions


@pytest.fixture(autouse=True)
def my_platforms(monkeypatch):
    monkeypatch.setattr(config.settings, "MY_PLATFORMS", "youtube,instagram")


def camp(**kw):
    base = dict(external_id="r1", name="Ring Drone", url="https://cr.test/r1", campaign_type="clipping",
                platforms=["instagram", "tiktok", "youtube"])
    base.update(kw)
    return Campaign(**base)


# --- model ---------------------------------------------------------------

def test_get_rules_defaults():
    r = get_rules(camp(), defaults=None)
    assert (r.clip_min_sec, r.clip_max_sec, r.max_posts_per_day, r.burn_captions, r.language) == (15, 60, 3, True, "en")
    assert r.hashtags == [] and r.caption_template == DEFAULT_TEMPLATE and r.confirmed is False
    assert r.post_platforms == ["youtube", "instagram"]


def test_get_rules_campaign_override():
    c = camp(clip_min_sec=20, clip_max_sec=45, required_hashtags=["Ring", "#AD"], required_mentions=["ring"],
             banned_words=[" Swear "], post_platforms=["instagram"], credit_text="Credit: @ring",
             max_posts_per_day=5, burn_captions=False)
    r = get_rules(c)
    assert (r.clip_min_sec, r.clip_max_sec) == (20, 45)
    assert r.hashtags == ["#ring", "#ad"] and r.mentions == ["@ring"] and r.banned_words == ["swear"]
    assert r.post_platforms == ["instagram"] and r.credit_text == "Credit: @ring"
    assert r.max_posts_per_day == 5 and r.burn_captions is False


def test_get_rules_app_settings_defaults(tmp_path):
    eng = init_db(get_engine(str(tmp_path / "r.db")))
    with get_session(eng) as s:
        s.add(AppSetting(key="default_rules", value=json.dumps({"max_posts_per_day": 7, "language": "hi",
                                                               "hashtags": ["#clips"], "clip_max_sec": 90})))
        s.add(camp())
    with get_session(eng) as s:
        r = get_rules(s.scalar(select(Campaign)))
    assert r.max_posts_per_day == 7 and r.language == "hi" and r.hashtags == ["#clips"]
    assert (r.clip_min_sec, r.clip_max_sec) == (15, 90)
    eng.dispose()


def test_normalizers():
    assert normalize_hashtags("#Ring, ring  #AD ad #two words") == ["#ring", "#ad", "#two", "#words"]
    assert normalize_hashtags(["# spaced tag", "#X"]) == ["#spacedtag", "#x"]
    assert normalize_mentions("@Ring ring, @other") == ["@ring", "@other"]
    assert normalize_banned("  Swear ,swear\nBAD word ") == ["swear", "bad word"]


def test_validate_errors():
    ok = Rules(post_platforms=["youtube"])
    assert validate(ok) == []
    assert any("5 ≤ min < max ≤ 180" in e for e in validate(Rules(clip_min_sec=3, post_platforms=["youtube"])))
    assert validate(Rules(clip_min_sec=60, clip_max_sec=30, post_platforms=["youtube"]))
    assert validate(Rules(clip_max_sec=200, post_platforms=["youtube"]))
    assert any("Unknown post platforms" in e for e in validate(Rules(post_platforms=["myspace"])))
    assert any("at least one platform" in e for e in validate(Rules(post_platforms=[])))
    assert any("30 hashtags" in e for e in validate(Rules(hashtags=[f"#t{i}" for i in range(31)],
                                                          post_platforms=["x"])))
    assert any("{hook}" in e for e in validate(Rules(caption_template="no hook", post_platforms=["x"])))


# --- suggestions -----------------------------------------------------------

def test_suggest_tags_and_mentions():
    s = suggest_from_text("Use #Ring and #RingDrone, tag @ring and @Ring.Official. Email help@ring.com")
    assert s["hashtags"] == ["#ring", "#ringdrone"]
    assert s["mentions"] == ["@ring", "@ring.official"]
    assert "disclosure_tag" not in s and "banned_hints" not in s


@pytest.mark.parametrize("text,expected", [
    ("Clips must be 15-60 seconds", {"clip_min_sec": 15, "clip_max_sec": 60}),
    ("at least 20 sec please", {"clip_min_sec": 20}),
    ("min 30s", {"clip_min_sec": 30}),
    ("keep it under 90 seconds", {"clip_max_sec": 90}),
    ("max 1 minute", {"clip_max_sec": 60}),
    ("between 10 and 45 seconds", {"clip_min_sec": 10, "clip_max_sec": 45}),
])
def test_suggest_lengths(text, expected):
    s = suggest_from_text(text)
    assert {k: s[k] for k in ("clip_min_sec", "clip_max_sec") if k in s} == expected


def test_suggest_disclosure_and_banned_hints():
    s = suggest_from_text("Add #ad to every post.\nDo not use copyrighted music\nNo swearing allowed!\n"
                          "Reposts are not allowed. Gambling content is prohibited.")
    assert s["disclosure_tag"] == "#ad" and "#ad" not in s.get("hashtags", [])
    assert s["banned_hints"] == ["Do not use copyrighted music", "No swearing allowed!", "Reposts are not allowed.",
                                 "Gambling content is prohibited."]
    assert suggest_from_text("This is a paid partnership")["disclosure_tag"] == "#ad"
    assert suggest_from_text("plain text") == {}


# --- captions --------------------------------------------------------------

def full_rules(**kw):
    base = dict(hashtags=["#ring", "#drone"], mentions=["@ring"], disclosure_tag="#ad", credit_text="Credit: @ring",
                post_platforms=["youtube", "instagram", "x"], banned_words=["ass"])
    base.update(kw)
    return Rules(**base)


def test_render_caption():
    out = render_caption(full_rules(), "  Big   moment  ", extra_hashtags=["#Drone", "fyp"])
    assert out == "Big moment\n\nCredit: @ring\n@ring #ring #drone #fyp #ad"
    bare = render_caption(Rules(caption_template="{hook}\n\n\n{credit}\n\n{mentions}  {hashtags} {disclosure}"), "Hi")
    assert bare == "Hi"


def test_render_for_platform_youtube():
    r = full_rules()
    yt = render_for_platform(r, "Short hook", "youtube")
    assert yt["title"] == "Short hook #shorts" and len(yt["title"]) <= 100 and "#ring" in yt["description"]
    long_title = render_for_platform(r, "x" * 150, "youtube")["title"]
    assert len(long_title) <= 100 and not long_title.endswith("#shorts")
    fits = render_for_platform(r, "y" * 92, "youtube")["title"]
    assert len(fits) == 100 and fits.endswith(" #shorts")
    no_room = render_for_platform(r, "y" * 93, "youtube")["title"]
    assert no_room == "y" * 93


@pytest.mark.parametrize("platform,limit,key", [("instagram", 2200, "caption"), ("x", 280, "text")])
def test_long_hook_trimmed_keeps_required(platform, limit, key):
    r = full_rules()
    out = render_for_platform(r, "word " * 1000, platform)[key]
    assert len(out) <= limit
    assert check_text(r, out) == []
    assert out.startswith("word") and "…" in out


def test_check_text():
    r = full_rules()
    assert check_text(r, "hook @ring #ring #drone #ad first class") == []
    problems = check_text(r, "hook #ring kick ASS")
    assert "missing hashtag #drone" in problems and "missing mention @ring" in problems
    assert "missing disclosure #ad" in problems and "banned word: ass" in problems
    assert check_text(r, "#ring #drone @ring #ads") == ["missing disclosure #ad"]


# --- dashboard --------------------------------------------------------------

@pytest.fixture
def engine(tmp_path):
    eng = init_db(get_engine(str(tmp_path / "ui.db")))
    with get_session(eng) as s:
        s.add(camp(description="Post 15-45 seconds clips. Use #RingDrone and tag @ring. Add #ad.\n"
                               "Do not use music."))
    yield eng
    eng.dispose()


@pytest.fixture
def client(engine, monkeypatch):
    monkeypatch.setattr(config.settings, "DASHBOARD_PASSWORD", "pw")
    monkeypatch.setattr(config.settings, "DASHBOARD_USER", "admin")
    c = TestClient(create_app(engine))
    c.auth = ("admin", "pw")
    return c


def load(engine):
    with get_session(engine) as s:
        return s.scalar(select(Campaign))


FORM = {"clip_min_sec": "20", "clip_max_sec": "50", "hashtags": "#Ring ringdrone", "mentions": "ring",
        "banned_words": "music, swear", "post_platforms_present": "1", "post_platforms": ["instagram"],
        "caption_template": "{hook}\n{mentions} {hashtags} {disclosure}", "credit_text": "", "disclosure_tag": "#ad",
        "max_posts_per_day": "2", "burn_captions": "1", "language": "en"}


def test_detail_shows_rules_suggestions_preview(client, engine):
    page = client.get(f"/campaigns/{load(engine).id}").text
    assert "rules ?" in page and "Suggested from campaign rules" in page and "#ringdrone" in page
    assert "Do not use music." in page and "Caption preview" in page and "This moment changed everything" in page
    assert "rules ?" in client.get("/campaigns").text


def test_post_rules_saves_and_confirms(client, engine):
    cid = load(engine).id
    r = client.post(f"/campaigns/{cid}/rules", data=FORM, follow_redirects=False)
    assert r.status_code == 303
    c = load(engine)
    assert c.rules_confirmed_at is not None and (c.clip_min_sec, c.clip_max_sec) == (20, 50)
    assert c.required_hashtags == ["#ring", "#ringdrone"] and c.required_mentions == ["@ring"]
    assert c.banned_words == ["music", "swear"] and c.post_platforms == ["instagram"]
    assert c.max_posts_per_day == 2 and c.burn_captions is True and c.disclosure_tag == "#ad"
    page = client.get(f"/campaigns/{cid}").text
    assert "rules ✓" in page and ">OK<" in page
    # a template without {mentions} is flagged in the preview
    client.post(f"/campaigns/{cid}/rules", data=dict(FORM, caption_template="{hook}\n{hashtags} {disclosure}"))
    assert "missing mention @ring" in client.get(f"/campaigns/{cid}").text


def test_post_rules_invalid(client, engine):
    cid = load(engine).id
    bad = dict(FORM, clip_min_sec="70", clip_max_sec="30", caption_template="no placeholder", post_platforms=["x"])
    r = client.post(f"/campaigns/{cid}/rules", data=bad)
    assert r.status_code == 400
    assert "5 ≤ min &lt; max ≤ 180" in r.text and "{hook}" in r.text and "does not accept: x" in r.text
    c = load(engine)
    assert c.rules_confirmed_at is None and c.clip_min_sec is None


def test_apply_suggestions_merges(client, engine):
    with get_session(engine) as s:
        s.scalar(select(Campaign)).required_hashtags = ["#mine"]
    cid = load(engine).id
    r = client.post(f"/campaigns/{cid}/rules/apply-suggestions", follow_redirects=False)
    assert r.status_code == 303
    c = load(engine)
    assert c.required_hashtags == ["#mine", "#ringdrone"] and c.required_mentions == ["@ring"]
    assert (c.clip_min_sec, c.clip_max_sec, c.disclosure_tag) == (15, 45, "#ad")
    assert c.banned_words is None and c.rules_confirmed_at is None


def test_settings_save_and_load(client, engine):
    page = client.get("/settings")
    assert page.status_code == 200 and "DASHBOARD_PASSWORD" in page.text and "TIMEZONE" in page.text
    data = {k: v for k, v in FORM.items() if not k.startswith("post_platforms")}
    data.update(max_posts_per_day="6", language="hi")
    assert client.post("/settings", data=data, follow_redirects=False).status_code == 303
    with get_session(engine) as s:
        stored = json.loads(s.scalar(select(AppSetting).where(AppSetting.key == "default_rules")).value)
        assert stored["max_posts_per_day"] == 6 and stored["language"] == "hi"
        assert "post_platforms" not in stored
        assert get_rules(s.scalar(select(Campaign))).max_posts_per_day == 6
    assert 'value="6"' in client.get("/settings").text
    bad = client.post("/settings", data=dict(data, caption_template="nope"))
    assert bad.status_code == 400 and "{hook}" in bad.text
