"""Common template rendering context and navigation."""
from __future__ import annotations

from fastapi import Request
from sqlalchemy.orm import Session

from app.config import APP_NAME, APP_VERSION
from app.deps import templates
from app.models import User
from app.permissions import has_permission
from app.services import settings_service

def render(
    request: Request,
    name: str,
    db: Session,
    context: dict | None = None,
    *,
    user: User | None = None,
    status_code: int = 200,
):
    user = user if user is not None else getattr(request.state, "user", None)
    # Server-rendered initial clock value so the topbar never flashes a
    # placeholder (and never shifts) between page navigations.
    from app.utils import business_now

    tz = settings_service.get_setting(db, "timezone", "Asia/Kolkata")
    now_local = business_now(tz)
    path = request.url.path


    ctx = {
        "app_name": APP_NAME,
        "app_version": APP_VERSION,
        "user": user,
        "profile": settings_service.get_profile(db),
        "current_path": path,
        "business_tz": tz,
        "ist_time": now_local.strftime("%I:%M:%S"),
        "ist_period": now_local.strftime("%p"),
    }
    if context:
        ctx.update(context)
    ctx["can"] = lambda code: has_permission(user, code)
    return templates.TemplateResponse(request, name, ctx, status_code=status_code)
