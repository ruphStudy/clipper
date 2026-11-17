"""Database layer tests (temp SQLite only)."""
import pytest
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import IntegrityError

from app.db import get_engine, get_session, init_db
from app.db.enums import Platform, PostStatus
from app.db.models import Account, Campaign, Clip, Post, Source

TABLES = {"accounts", "campaigns", "sources", "clips", "posts", "post_stats", "app_settings"}


@pytest.fixture
def engine(tmp_path):
    eng = init_db(get_engine(str(tmp_path / "test.db")))
    yield eng
    eng.dispose()


def _campaign(ext="c1"):
    return Campaign(external_id=ext, name="Camp", url="https://example.com/c", platforms=["youtube"])


def _chain(s):
    camp = _campaign()
    src = Source(campaign=camp, url="https://example.com/v.mp4", kind="direct")
    clip = Clip(campaign=camp, source=src, start_sec=1.0, end_sec=31.0)
    acct = Account(platform=Platform.youtube, handle="@me")
    post = Post(clip=clip, account=acct, platform=Platform.youtube)
    s.add_all([camp, acct, post])
    return camp


def test_create_all_tables(engine):
    assert set(inspect(engine).get_table_names()) == TABLES


def test_chain_and_relationships(engine):
    with get_session(engine) as s:
        _chain(s)
    with get_session(engine) as s:
        camp = s.scalars(select(Campaign)).one()
        assert camp.status == "discovered" and camp.required_hashtags == []
        src = camp.sources[0]
        clip = src.clips[0]
        assert clip.campaign.id == camp.id and camp.clips[0].id == clip.id
        post = clip.posts[0]
        assert post.status == PostStatus.scheduled
        assert post.account.handle == "@me" and post.account.posts[0].id == post.id
        assert camp.created_at is not None and camp.updated_at is not None


def test_unique_external_id(engine):
    with get_session(engine) as s:
        s.add(_campaign("dup"))
    with pytest.raises(IntegrityError):
        with get_session(engine) as s:
            s.add(_campaign("dup"))


def test_delete_campaign_cascades(engine):
    with get_session(engine) as s:
        _chain(s)
    with get_session(engine) as s:
        s.delete(s.scalars(select(Campaign)).one())
    with get_session(engine) as s:
        assert s.scalars(select(Source)).all() == []
        assert s.scalars(select(Clip)).all() == []
        assert s.scalars(select(Post)).all() == []
        assert len(s.scalars(select(Account)).all()) == 1


def test_foreign_keys_pragma_on(engine):
    with engine.connect() as conn:
        assert conn.execute(text("PRAGMA foreign_keys")).scalar() == 1


def test_get_session_rolls_back(engine):
    with pytest.raises(RuntimeError):
        with get_session(engine) as s:
            s.add(_campaign("rb"))
            s.flush()
            raise RuntimeError("boom")
    with get_session(engine) as s:
        assert s.scalars(select(Campaign)).all() == []
