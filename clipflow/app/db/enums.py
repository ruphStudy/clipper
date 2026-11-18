"""String enums stored as plain strings in the DB."""
from enum import Enum


class Platform(str, Enum):
    """Social platform."""

    youtube = "youtube"
    instagram = "instagram"
    tiktok = "tiktok"
    x = "x"


class CampaignStatus(str, Enum):
    """Campaign lifecycle."""

    discovered = "discovered"
    selected = "selected"
    joined = "joined"
    paused = "paused"
    ended = "ended"


class SourceKind(str, Enum):
    """Where a source video lives."""

    drive = "drive"
    youtube = "youtube"
    twitch = "twitch"
    kick = "kick"
    direct = "direct"
    other = "other"


class SourceStatus(str, Enum):
    """Source processing state."""

    pending = "pending"
    downloading = "downloading"
    downloaded = "downloaded"
    processing = "processing"
    processed = "processed"
    failed = "failed"


class ClipStatus(str, Enum):
    """Clip review/publish state."""

    candidate = "candidate"  # moment picked, not rendered yet
    rendering = "rendering"
    rendered = "rendered"
    approved = "approved"
    rejected = "rejected"
    scheduled = "scheduled"
    posted = "posted"
    failed = "failed"


class PostStatus(str, Enum):
    """Post upload/submission state."""

    scheduled = "scheduled"
    uploading = "uploading"
    manual_pending = "manual_pending"  # handed to the user to post by hand
    posted = "posted"
    submitted = "submitted"
    failed = "failed"
