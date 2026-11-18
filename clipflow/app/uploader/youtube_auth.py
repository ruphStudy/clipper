"""YouTube OAuth: authorize a channel (browser, CLI only), load/refresh tokens. Never logs secrets."""
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from sqlalchemy import Engine, select

from app.config import settings
from app.db import get_session
from app.db.enums import Platform
from app.db.models import Account, AppSetting
from app.logger import get_logger
from app.notify import notify
from app.uploader.common import AuthError  # noqa: F401  (re-exported for callers)

log = get_logger("clipflow.youtube.auth")

SCOPES = ["https://www.googleapis.com/auth/youtube.upload", "https://www.googleapis.com/auth/youtube.readonly"]
REAUTH = "reauthorize needed"


def token_path(credentials_ref: str) -> Path:
    """Absolute path of a token file; credentials_ref is a bare file name inside SECRETS_DIR."""
    return Path(settings.SECRETS_DIR) / Path(credentials_ref).name


def _write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def build_service(credentials, build_fn: Optional[Callable] = None):
    """YouTube Data API v3 client."""
    if build_fn is None:
        from googleapiclient.discovery import build as build_fn
    return build_fn("youtube", "v3", credentials=credentials, cache_discovery=False)


def _channel(service) -> tuple[str, str]:
    resp = service.channels().list(mine=True, part="id,snippet").execute()
    items = resp.get("items") or []
    if not items:
        raise AuthError("this Google account has no YouTube channel")
    return items[0]["id"], items[0].get("snippet", {}).get("title", "")


def authorize(handle: str, engine: Optional[Engine] = None, flow_factory: Optional[Callable] = None,
              build_fn: Optional[Callable] = None) -> Account:
    """Run the browser consent flow, store the token (600) and upsert the YouTube account."""
    secrets_file = settings.YOUTUBE_CLIENT_SECRETS_PATH
    if not secrets_file or not Path(secrets_file).is_file():
        raise AuthError("YOUTUBE_CLIENT_SECRETS_PATH is not set or the file does not exist")
    if flow_factory is None:
        from google_auth_oauthlib.flow import InstalledAppFlow
        flow_factory = InstalledAppFlow.from_client_secrets_file
    flow = flow_factory(secrets_file, scopes=SCOPES)
    creds = flow.run_local_server(port=0, open_browser=True, access_type="offline", prompt="consent")
    channel_id, title = _channel(build_service(creds, build_fn))
    ref = f"youtube_{channel_id}.json"
    _write_private(token_path(ref), creds.to_json())
    handle = handle.strip()
    with get_session(engine) as s:
        acct = s.scalar(select(Account).where(Account.platform == Platform.youtube, Account.channel_id == channel_id))
        if acct is None:
            acct = s.scalar(select(Account).where(Account.platform == Platform.youtube, Account.handle == handle))
        if acct is None:
            acct = Account(platform=Platform.youtube, handle=handle)
            s.add(acct)
        acct.handle, acct.channel_id, acct.display_name = handle, channel_id, title
        acct.credentials_ref, acct.last_auth_at, acct.auth_error = ref, datetime.now(timezone.utc), None
        acct.is_active = True
        s.flush()
        warn = s.scalar(select(AppSetting).where(AppSetting.key == f"yt_auth_warned_{acct.id}"))
        if warn is not None:
            s.delete(warn)
        s.expunge(acct)
    log.info("youtube account authorized: %s (%s)", title, channel_id)
    return acct


def _mark_reauth(account_id: int, engine: Optional[Engine]) -> None:
    key = f"yt_auth_warned_{account_id}"
    first = False
    with get_session(engine) as s:
        acct = s.get(Account, account_id)
        if acct is not None:
            acct.auth_error = REAUTH
        if not s.scalar(select(AppSetting).where(AppSetting.key == key)):
            s.add(AppSetting(key=key, value=datetime.now(timezone.utc).isoformat()))
            first = True
    if first:
        try:
            handle = acct.handle if acct is not None else f"#{account_id}"
            notify("YouTube login expired", f"Account {handle} needs re-authorization: run yt-auth {handle}",
                   level="warning")
        except Exception as exc:  # noqa: BLE001
            log.warning("auth notify failed: %s", type(exc).__name__)


def get_credentials(account: Account, engine: Optional[Engine] = None, request_factory: Optional[Callable] = None):
    """Load the account's token, refreshing (and saving) it if expired. Raises AuthError if unusable."""
    from google.auth.exceptions import RefreshError
    from google.oauth2.credentials import Credentials

    if not account.credentials_ref:
        raise AuthError("account has no stored credentials (run yt-auth)")
    path = token_path(account.credentials_ref)
    if not path.is_file():
        _mark_reauth(account.id, engine)
        raise AuthError("token file missing (run yt-auth)")
    try:
        creds = Credentials.from_authorized_user_info(json.loads(path.read_text(encoding="utf-8")), SCOPES)
    except (ValueError, KeyError) as exc:
        _mark_reauth(account.id, engine)
        raise AuthError("token file unreadable (run yt-auth)") from exc
    if not creds.valid:
        if not creds.refresh_token:
            _mark_reauth(account.id, engine)
            raise AuthError(REAUTH)
        if request_factory is None:
            from google.auth.transport.requests import Request as request_factory
        try:
            creds.refresh(request_factory())
        except RefreshError as exc:
            _mark_reauth(account.id, engine)
            raise AuthError(REAUTH) from exc
        _write_private(path, creds.to_json())
    return creds


def whoami(account: Account, engine: Optional[Engine] = None, build_fn: Optional[Callable] = None) -> dict:
    """{'channel_id', 'title'} of the authorized channel."""
    channel_id, title = _channel(build_service(get_credentials(account, engine), build_fn))
    return {"channel_id": channel_id, "title": title}
