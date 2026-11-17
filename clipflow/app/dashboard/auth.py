"""HTTP Basic auth for the dashboard (localhost-only when no password is set)."""
import secrets
from typing import Optional

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from app.config import settings

LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost"}
_basic = HTTPBasic(auto_error=False)


def _is_local(request: Request) -> bool:
    host = request.client.host if request.client else ""
    extra = getattr(request.app.state, "extra_local_hosts", set())
    return host in LOCAL_HOSTS or host in extra


def require_auth(request: Request, creds: Optional[HTTPBasicCredentials] = Depends(_basic)) -> str:
    """Dependency: authenticate the request; returns the username."""
    password = settings.DASHBOARD_PASSWORD
    if not password:
        request.state.no_password = True
        if _is_local(request):
            return "local"
        raise HTTPException(status.HTTP_403_FORBIDDEN,
                            "Dashboard has no password; only localhost access is allowed.")
    request.state.no_password = False
    ok = creds is not None and secrets.compare_digest(
        creds.username.encode(), settings.DASHBOARD_USER.encode()
    ) & secrets.compare_digest(creds.password.encode(), password.encode())
    if not ok:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid credentials",
                            headers={"WWW-Authenticate": 'Basic realm="ClipFlow"'})
    return creds.username
