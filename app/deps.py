"""FastAPI dependencies: authentication, session lookup, permission guards."""
from __future__ import annotations

import os

from fastapi import Depends, HTTPException, Request, status
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.config import SESSION_COOKIE, STATIC_DIR, TEMPLATE_DIR
from app.database import get_db
from app.models import CUSTOMER_TYPE_LABELS, User
from app.permissions import has_permission
from app.security import read_session_token
from jinja2 import pass_context

from app.utils import format_deduction, format_inr, to_local

templates = Jinja2Templates(directory=str(TEMPLATE_DIR))
templates.env.filters["inr"] = format_inr
templates.env.filters["deduct"] = format_deduction
templates.env.filters["ctype"] = lambda v: CUSTOMER_TYPE_LABELS.get((v or "WALK_IN"), "Walk-In")


@pass_context
def _localtime(ctx, value, fmt: str = "%d %b %Y, %I:%M %p") -> str:
    """Render a stored UTC timestamp in the pharmacy's timezone."""
    local = to_local(value, ctx.get("business_tz") or "Asia/Kolkata")
    return local.strftime(fmt) if local else ""


templates.env.filters["localtime"] = _localtime


def static_url(path: str) -> str:
    """Cache-busted static asset URL based on the file's mtime and size.

    Prevents a browser from serving a stale stylesheet/script after a deploy
    (``/static/pos-pack.css?v=...``)."""
    rel = str(path or "").lstrip("/")
    if rel.startswith("static/"):
        rel = rel[len("static/"):]
    try:
        st = os.stat(STATIC_DIR / rel)
        version = f"{int(st.st_mtime)}-{st.st_size}"
    except OSError:
        version = "0"
    return f"/static/{rel}?v={version}"


templates.env.globals["static_url"] = static_url


def get_current_user(request: Request, db: Session = Depends(get_db)) -> User | None:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    data = read_session_token(token)
    if not data:
        return None
    user = db.get(User, data.get("uid"))
    if user is None or not user.is_active:
        return None
    from app.models import LoginSession

    sess = db.get(LoginSession, data.get("sid")) if data.get("sid") else None
    if sess is None or sess.logout_at is not None or sess.user_id != user.id:
        return None                    # logged out, or ended by a password reset
    request.state.user = user
    request.state.session_id = data.get("sid")
    return user


def require_login(user: User | None = Depends(get_current_user)) -> User:
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Login required")
    return user


def require_permission(code: str):
    def _guard(user: User = Depends(require_login)) -> User:
        if not has_permission(user, code):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Missing permission: {code}",
            )
        return user

    return _guard


def client_ip(request: Request) -> str:
    if request.client:
        return request.client.host
    return ""
