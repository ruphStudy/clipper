"""SQLAlchemy 2.0 ORM models."""
from datetime import datetime, timezone
from enum import Enum as PyEnum
from typing import Optional

from sqlalchemy import JSON, Boolean, DateTime, Enum, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from app.db.enums import CampaignStatus, ClipStatus, Platform, PostStatus, SourceKind, SourceStatus


def utcnow() -> datetime:
    """Current UTC time."""
    return datetime.now(timezone.utc)


def str_enum(enum_cls: type[PyEnum]) -> Enum:
    """Store an enum as its string value."""
    return Enum(
        enum_cls,
        native_enum=False,
        length=32,
        validate_strings=True,
        values_callable=lambda e: [m.value for m in e],
    )


class Base(DeclarativeBase):
    """Declarative base with id and timestamps."""

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class Account(Base):
    """A social account clips are posted to."""

    __tablename__ = "accounts"
    __table_args__ = (UniqueConstraint("platform", "handle"),)

    platform: Mapped[Platform] = mapped_column(str_enum(Platform))
    handle: Mapped[str] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    daily_post_limit: Mapped[int] = mapped_column(Integer, default=4)
    credentials_ref: Mapped[Optional[str]] = mapped_column(String(512))

    posts: Mapped[list["Post"]] = relationship(back_populates="account")


class Campaign(Base):
    """A clipping campaign."""

    __tablename__ = "campaigns"

    external_id: Mapped[str] = mapped_column(String(255), unique=True)
    name: Mapped[str] = mapped_column(String(512))
    brand: Mapped[Optional[str]] = mapped_column(String(255))
    url: Mapped[str] = mapped_column(String(1024))
    campaign_type: Mapped[Optional[str]] = mapped_column(String(64))
    platforms: Mapped[list] = mapped_column(JSON, default=list)
    cpm_rate: Mapped[Optional[float]] = mapped_column(Float)
    budget_total: Mapped[Optional[float]] = mapped_column(Float)
    budget_left: Mapped[Optional[float]] = mapped_column(Float)
    min_payout: Mapped[Optional[float]] = mapped_column(Float)
    max_payout_per_clip: Mapped[Optional[float]] = mapped_column(Float)
    creators_count: Mapped[Optional[int]] = mapped_column(Integer)
    score: Mapped[float] = mapped_column(Float, default=0)
    status: Mapped[CampaignStatus] = mapped_column(
        str_enum(CampaignStatus), default=CampaignStatus.discovered, index=True
    )
    rules_text: Mapped[Optional[str]] = mapped_column(Text)
    required_hashtags: Mapped[list] = mapped_column(JSON, default=list)
    required_mentions: Mapped[list] = mapped_column(JSON, default=list)
    clip_min_sec: Mapped[int] = mapped_column(Integer, default=15)
    clip_max_sec: Mapped[int] = mapped_column(Integer, default=60)
    notes: Mapped[Optional[str]] = mapped_column(Text)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    sources: Mapped[list["Source"]] = relationship(
        back_populates="campaign", cascade="all, delete-orphan", passive_deletes=True
    )
    clips: Mapped[list["Clip"]] = relationship(
        back_populates="campaign", cascade="all, delete-orphan", passive_deletes=True
    )


class Source(Base):
    """A source video belonging to a campaign."""

    __tablename__ = "sources"
    __table_args__ = (UniqueConstraint("campaign_id", "url"),)

    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id", ondelete="CASCADE"))
    url: Mapped[str] = mapped_column(String(1024))
    kind: Mapped[SourceKind] = mapped_column(str_enum(SourceKind))
    status: Mapped[SourceStatus] = mapped_column(
        str_enum(SourceStatus), default=SourceStatus.pending, index=True
    )
    local_path: Mapped[Optional[str]] = mapped_column(String(1024))
    duration_sec: Mapped[Optional[float]] = mapped_column(Float)
    transcript_path: Mapped[Optional[str]] = mapped_column(String(1024))
    error: Mapped[Optional[str]] = mapped_column(Text)

    campaign: Mapped[Campaign] = relationship(back_populates="sources")
    clips: Mapped[list["Clip"]] = relationship(
        back_populates="source", cascade="all, delete-orphan", passive_deletes=True
    )


class Clip(Base):
    """A rendered clip cut from a source."""

    __tablename__ = "clips"

    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id", ondelete="CASCADE"))
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id", ondelete="CASCADE"))
    start_sec: Mapped[float] = mapped_column(Float)
    end_sec: Mapped[float] = mapped_column(Float)
    hook_title: Mapped[Optional[str]] = mapped_column(String(512))
    caption: Mapped[Optional[str]] = mapped_column(Text)
    hashtags: Mapped[list] = mapped_column(JSON, default=list)
    file_path: Mapped[Optional[str]] = mapped_column(String(1024))
    moment_score: Mapped[Optional[float]] = mapped_column(Float)
    status: Mapped[ClipStatus] = mapped_column(str_enum(ClipStatus), default=ClipStatus.rendered, index=True)
    reject_reason: Mapped[Optional[str]] = mapped_column(String(512))

    campaign: Mapped[Campaign] = relationship(back_populates="clips")
    source: Mapped[Source] = relationship(back_populates="clips")
    posts: Mapped[list["Post"]] = relationship(
        back_populates="clip", cascade="all, delete-orphan", passive_deletes=True
    )


class Post(Base):
    """A clip posted (or scheduled) to an account."""

    __tablename__ = "posts"
    __table_args__ = (UniqueConstraint("clip_id", "account_id"),)

    clip_id: Mapped[int] = mapped_column(ForeignKey("clips.id", ondelete="CASCADE"))
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"))
    platform: Mapped[Platform] = mapped_column(str_enum(Platform))
    status: Mapped[PostStatus] = mapped_column(str_enum(PostStatus), default=PostStatus.scheduled, index=True)
    scheduled_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), index=True)
    posted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    submitted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    platform_post_id: Mapped[Optional[str]] = mapped_column(String(255))
    post_url: Mapped[Optional[str]] = mapped_column(String(1024))
    views: Mapped[int] = mapped_column(Integer, default=0)
    earnings_est: Mapped[float] = mapped_column(Float, default=0)
    last_checked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    error: Mapped[Optional[str]] = mapped_column(Text)

    clip: Mapped[Clip] = relationship(back_populates="posts")
    account: Mapped[Account] = relationship(back_populates="posts")
    stats: Mapped[list["PostStat"]] = relationship(
        back_populates="post", cascade="all, delete-orphan", passive_deletes=True
    )


class PostStat(Base):
    """A view-count snapshot for a post."""

    __tablename__ = "post_stats"

    post_id: Mapped[int] = mapped_column(ForeignKey("posts.id", ondelete="CASCADE"))
    views: Mapped[int] = mapped_column(Integer)
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    post: Mapped[Post] = relationship(back_populates="stats")


class AppSetting(Base):
    """Key/value runtime setting."""

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(255), unique=True)
    value: Mapped[str] = mapped_column(Text)
