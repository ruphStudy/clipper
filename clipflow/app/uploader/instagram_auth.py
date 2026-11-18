"""Instagram (Facebook Login) connection: long-lived Page token for an IG professional account.

Tokens travel in headers where the Graph API allows it; the endpoints that require query parameters
(token exchange, debug_token) are never logged (httpx request logging is silenced). Never logs secrets.
"""
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx
from sqlalchemy import Engine, select

from app.config import settings
from app.db import get_session
from app.db.enums import Platform
from app.db.models import Account
from app.logger import get_logger
from app.notify import notify
from app.uploader.common import AuthError, get_setting, set_setting

log = get_logger("clipflow.instagram.auth")
logging.getLogger("httpx").setLevel(logging.WARNING)  # request URLs can carry tokens
logging.getLogger("httpcore").setLevel(logging.WARNING)

TIMEOUT = 30.0
RECONNECT = "reconnect needed"


class GraphError(Exception):
    """A Graph API error with its code/subcode (message is safe to show)."""

    def __init__(self, message: str, code: Optional[int] = None, subcode: Optional[int] = None,
                 status: Optional[int] = None):
        super().__init__(message)
        self.code, self.subcode, self.status = code, subcode, status


def graph_base() -> str:
    return f"https://graph.facebook.com/{settings.META_GRAPH_VERSION}"


def graph_call(client: httpx.Client, method: str, path: str, token: Optional[str] = None,
               params: Optional[dict] = None, data: Optional[dict] = None) -> dict:
    """Call the Graph API; raises GraphError with code/subcode on failure. Token goes in the OAuth header."""
    headers = {"Authorization": f"OAuth {token}"} if token else {}
    url = path if path.startswith("http") else f"{graph_base()}/{path.lstrip('/')}"
    try:
        resp = client.request(method, url, params=params, data=data, headers=headers, timeout=TIMEOUT)
    except httpx.HTTPError as exc:
        raise GraphError(f"network error: {type(exc).__name__}") from exc
    try:
        body = resp.json()
    except ValueError:
        body = {}
    if resp.status_code >= 400 or (isinstance(body, dict) and "error" in body):
        err = (body or {}).get("error") or {}
        msg = err.get("error_user_msg") or err.get("message") or f"HTTP {resp.status_code}"
        raise GraphError(str(msg)[:200], err.get("code"), err.get("error_subcode"), resp.status_code)
    return body


def _write_private(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(payload, fh)
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def token_path(credentials_ref: str) -> Path:
    return Path(settings.SECRETS_DIR) / Path(credentials_ref).name


def connect_instagram(short_lived_user_token: str, page_hint: Optional[str] = None, engine: Optional[Engine] = None,
                      client: Optional[httpx.Client] = None) -> Account:
    """Exchange the token, find the IG-linked Page, store its (non-expiring) Page token, upsert the account."""
    if not settings.META_APP_ID or not settings.META_APP_SECRET:
        raise AuthError("META_APP_ID and META_APP_SECRET must be set")
    own = client is None
    client = client or httpx.Client(timeout=TIMEOUT)
    try:
        try:
            exchanged = graph_call(client, "GET", "oauth/access_token", params={
                "grant_type": "fb_exchange_token", "client_id": settings.META_APP_ID,
                "client_secret": settings.META_APP_SECRET, "fb_exchange_token": short_lived_user_token.strip()})
        except GraphError as exc:
            raise AuthError(f"token exchange failed: {exc}") from exc
        long_token = exchanged.get("access_token")
        if not long_token:
            raise AuthError("token exchange returned no access token")
        try:
            pages = graph_call(client, "GET", "me/accounts", long_token, params={
                "fields": "id,name,access_token,instagram_business_account{id,username}", "limit": 100})
        except GraphError as exc:
            raise AuthError(f"could not list Facebook Pages: {exc}") from exc
    finally:
        if own:
            client.close()
    linked = [p for p in pages.get("data") or [] if p.get("instagram_business_account")]
    if not linked:
        raise AuthError("no Facebook Page with a linked Instagram professional account was found "
                        "(link the IG account to a Page and grant the Page permissions)")
    if page_hint:
        hint = page_hint.strip().lstrip("@").lower()
        linked = [p for p in linked if p.get("name", "").lower() == hint
                  or p["instagram_business_account"].get("username", "").lower() == hint]
        if not linked:
            raise AuthError(f"no linked Page matches '{page_hint}'")
    if len(linked) > 1:
        names = ", ".join(f"{p.get('name')} (@{p['instagram_business_account'].get('username')})" for p in linked)
        raise AuthError(f"several Pages have Instagram accounts; choose one with --page: {names}")
    page = linked[0]
    ig = page["instagram_business_account"]
    ig_user_id, ig_username = str(ig["id"]), ig.get("username") or str(ig["id"])
    ref = f"instagram_{ig_user_id}.json"
    _write_private(token_path(ref), {"page_id": page["id"], "page_name": page.get("name"), "ig_user_id": ig_user_id,
                                     "ig_username": ig_username, "page_token": page["access_token"],
                                     "saved_at": datetime.now(timezone.utc).isoformat()})
    with get_session(engine) as s:
        acct = s.scalar(select(Account).where(Account.platform == Platform.instagram, Account.channel_id == ig_user_id))
        if acct is None:
            acct = s.scalar(select(Account).where(Account.platform == Platform.instagram, Account.handle == ig_username))
        if acct is None:
            acct = Account(platform=Platform.instagram, handle=ig_username)
            s.add(acct)
        acct.handle, acct.channel_id, acct.display_name = ig_username, ig_user_id, page.get("name")
        acct.credentials_ref, acct.last_auth_at, acct.auth_error = ref, datetime.now(timezone.utc), None
        acct.is_active = True
        s.flush()
        warn_key = f"ig_auth_warned_{acct.id}"
        if get_setting(s, warn_key):
            set_setting(s, warn_key, "")
        s.expunge(acct)
    log.info("instagram connected: @%s via page %s", ig_username, page.get("name"))
    return acct


def load_token(account: Account) -> tuple[str, str]:
    """(ig_user_id, page_token) for an account; AuthError if missing or corrupt."""
    if not account.credentials_ref:
        raise AuthError("account has no stored token (run ig-connect)")
    path = token_path(account.credentials_ref)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return str(data["ig_user_id"]), str(data["page_token"])
    except (OSError, ValueError, KeyError) as exc:
        raise AuthError("token file missing or unreadable (run ig-connect)") from exc


def mark_reconnect(account_id: int, engine: Optional[Engine] = None) -> None:
    """Set auth_error and notify once (until the account is reconnected)."""
    key = f"ig_auth_warned_{account_id}"
    first = False
    with get_session(engine) as s:
        acct = s.get(Account, account_id)
        if acct is not None:
            acct.auth_error = RECONNECT
        if not get_setting(s, key):
            set_setting(s, key, datetime.now(timezone.utc).isoformat())
            first = True
        handle = acct.handle if acct is not None else f"#{account_id}"
    if first:
        try:
            notify("Instagram connection expired", f"Account @{handle} needs reconnecting: run ig-connect",
                   level="warning")
        except Exception as exc:  # noqa: BLE001
            log.warning("ig auth notify failed: %s", type(exc).__name__)


def debug_token(account: Account, engine: Optional[Engine] = None, client: Optional[httpx.Client] = None) -> dict:
    """{'is_valid', 'expires_at', 'scopes'}; marks the account and raises AuthError if invalid."""
    _ig, token = load_token(account)
    own = client is None
    client = client or httpx.Client(timeout=TIMEOUT)
    try:
        data = graph_call(client, "GET", "debug_token", params={
            "input_token": token, "access_token": f"{settings.META_APP_ID}|{settings.META_APP_SECRET}"}).get("data", {})
    except GraphError as exc:
        raise AuthError(f"token check failed: {exc}") from exc
    finally:
        if own:
            client.close()
    info = {"is_valid": bool(data.get("is_valid")), "expires_at": data.get("expires_at"),
            "scopes": list(data.get("scopes") or [])}
    if not info["is_valid"]:
        mark_reconnect(account.id, engine)
        raise AuthError(RECONNECT)
    return info
