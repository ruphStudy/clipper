"""SQLAlchemy 2.0 ORM models."""
from datetime import date, datetime, timezone
from enum import Enum as PyEnum
from typing import Optional

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    false,
    text,
    true,
)
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
    credentials_ref: Mapped[Optional[str]] = mapped_column(String(512))  # token file name only, never secrets
    channel_id: Mapped[Optional[str]] = mapped_column(String(64))
    display_name: Mapped[Optional[str]] = mapped_column(String(255))
    last_auth_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    auth_error: Mapped[Optional[str]] = mapped_column(String(255))

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
    clip_min_sec: Mapped[Optional[int]] = mapped_column(Integer)  # None = use default rules
    clip_max_sec: Mapped[Optional[int]] = mapped_column(Integer)
    notes: Mapped[Optional[str]] = mapped_column(Text)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    payout_type: Mapped[Optional[str]] = mapped_column(String(32))
    requires_application: Mapped[Optional[bool]] = mapped_column(Boolean)
    is_verified: Mapped[Optional[bool]] = mapped_column(Boolean)
    description: Mapped[Optional[str]] = mapped_column(Text)
    budget_spent: Mapped[Optional[float]] = mapped_column(Float)
    progress_pct: Mapped[Optional[float]] = mapped_column(Float)
    funded_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    is_live: Mapped[Optional[bool]] = mapped_column(Boolean, default=True, server_default=true())
    score_breakdown: Mapped[Optional[dict]] = mapped_column(JSON)
    score_reasons: Mapped[Optional[list]] = mapped_column(JSON)
    est_per_clip: Mapped[Optional[float]] = mapped_column(Float)
    burn_per_day: Mapped[Optional[float]] = mapped_column(Float)
    days_left: Mapped[Optional[float]] = mapped_column(Float)
    scored_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    # Posting rules (None = use defaults; see app.rules.get_rules)
    banned_words: Mapped[Optional[list]] = mapped_column(JSON)
    post_platforms: Mapped[Optional[list]] = mapped_column(JSON)
    caption_template: Mapped[Optional[str]] = mapped_column(Text)
    credit_text: Mapped[Optional[str]] = mapped_column(String(255))
    disclosure_tag: Mapped[Optional[str]] = mapped_column(String(64))
    max_posts_per_day: Mapped[Optional[int]] = mapped_column(Integer)
    burn_captions: Mapped[Optional[bool]] = mapped_column(Boolean)
    language: Mapped[Optional[str]] = mapped_column(String(16))
    rules_confirmed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    sources: Mapped[list["Source"]] = relationship(
        back_populates="campaign", cascade="all, delete-orphan", passive_deletes=True
    )
    clips: Mapped[list["Clip"]] = relationship(
        back_populates="campaign", cascade="all, delete-orphan", passive_deletes=True
    )
    snapshots: Mapped[list["CampaignSnapshot"]] = relationship(
        back_populates="campaign", cascade="all, delete-orphan", passive_deletes=True
    )


class CampaignSnapshot(Base):
    """Point-in-time budget/creator numbers for burn-rate tracking."""

    __tablename__ = "campaign_snapshots"
    __table_args__ = (Index("ix_campaign_snapshots_campaign_captured", "campaign_id", "captured_at"),)

    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id", ondelete="CASCADE"))
    budget_left: Mapped[Optional[float]] = mapped_column(Float)
    budget_spent: Mapped[Optional[float]] = mapped_column(Float)
    creators_count: Mapped[Optional[int]] = mapped_column(Integer)
    progress_pct: Mapped[Optional[float]] = mapped_column(Float)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    campaign: Mapped[Campaign] = relationship(back_populates="snapshots")


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
    attempts: Mapped[Optional[int]] = mapped_column(Integer, default=0, server_default=text("0"))
    next_retry_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    error_permanent: Mapped[Optional[bool]] = mapped_column(Boolean, default=False, server_default=false())
    downloaded_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    file_deleted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    transcribed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    transcribe_attempts: Mapped[Optional[int]] = mapped_column(Integer, default=0, server_default=text("0"))
    language: Mapped[Optional[str]] = mapped_column(String(16))
    speech_ratio: Mapped[Optional[float]] = mapped_column(Float)
    word_count: Mapped[Optional[int]] = mapped_column(Integer)
    moments_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    moments_attempts: Mapped[Optional[int]] = mapped_column(Integer, default=0, server_default=text("0"))
    moments_method: Mapped[Optional[str]] = mapped_column(String(16))  # "ai" | "heuristic"

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
    file_deleted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    thumb_path: Mapped[Optional[str]] = mapped_column(String(1024))
    render_attempts: Mapped[Optional[int]] = mapped_column(Integer, default=0, server_default=text("0"))
    duration_sec: Mapped[Optional[float]] = mapped_column(Float)
    layout: Mapped[Optional[str]] = mapped_column(String(16))
    rendered_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    compliance: Mapped[Optional[dict]] = mapped_column(JSON)  # {"ok": bool, "errors": [], "warnings": []}
    compliance_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    platform_captions: Mapped[Optional[dict]] = mapped_column(JSON)  # {platform: render_for_platform output}
    reviewed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    approved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    edited: Mapped[Optional[bool]] = mapped_column(Boolean, default=False, server_default=false())

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
    visibility: Mapped[Optional[str]] = mapped_column(String(32))  # public | unlisted | private | private_locked
    mode: Mapped[Optional[str]] = mapped_column(String(16))  # "api" | "manual"
    attempts: Mapped[Optional[int]] = mapped_column(Integer, default=0, server_default=text("0"))
    notified_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    reminder_sent: Mapped[Optional[bool]] = mapped_column(Boolean, default=False, server_default=false())
    submit_alert_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    submit_reminded: Mapped[Optional[bool]] = mapped_column(Boolean, default=False, server_default=false())
    submit_missed: Mapped[Optional[bool]] = mapped_column(Boolean, default=False, server_default=false())
    alert_message_id: Mapped[Optional[int]] = mapped_column(Integer)
    likes: Mapped[Optional[int]] = mapped_column(Integer)
    comments: Mapped[Optional[int]] = mapped_column(Integer)
    stats_error: Mapped[Optional[str]] = mapped_column(String(255))
    milestone_hit: Mapped[Optional[int]] = mapped_column(Integer, default=0, server_default=text("0"))

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


class CampaignEarning(Base):
    """An actual payout received for a campaign (entered by the user)."""

    __tablename__ = "campaign_earnings"

    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id", ondelete="CASCADE"), index=True)
    amount_usd: Mapped[float] = mapped_column(Float)
    period_end: Mapped[date] = mapped_column(Date)
    note: Mapped[Optional[str]] = mapped_column(String(255))


class AiUsage(Base):
    """One LLM API call (successful or not) for cost tracking."""

    __tablename__ = "ai_usage"

    purpose: Mapped[str] = mapped_column(String(64))
    model: Mapped[str] = mapped_column(String(64))
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    ok: Mapped[bool] = mapped_column(Boolean, default=False)


class AppSetting(Base):
    """Key/value runtime setting."""

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(255), unique=True)
    value: Mapped[str] = mapped_column(Text)
