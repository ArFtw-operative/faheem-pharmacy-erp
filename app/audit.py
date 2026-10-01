"""Audit logging. Every data-changing module must call `record(...)`.

The audit table is append-only by convention; the API never exposes updates or
deletes on it.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any

from sqlalchemy import inspect
from sqlalchemy.orm import Session

from app.models import AuditLog, User
from app.models import (  # re-exported action constants
    A_CREATE,
    A_DELETE,
    A_EXPORT,
    A_LOGIN,
    A_LOGIN_FAILED,
    A_LOGOUT,
    A_PERMISSION_CHANGE,
    A_PRINT,
    A_SETTLE,
    A_SNOOZE,
    A_UPDATE,
)

__all__ = [
    "A_CREATE", "A_UPDATE", "A_DELETE", "A_LOGIN", "A_LOGOUT", "A_LOGIN_FAILED",
    "A_SETTLE", "A_SNOOZE", "A_EXPORT", "A_PRINT", "A_PERMISSION_CHANGE",
    "record", "snapshot",
]


def _coerce(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except Exception:  # pragma: no cover - defensive
            return str(value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def snapshot(obj: Any, exclude: set[str] | None = None) -> dict[str, Any]:
    """Return a JSON-safe dict of an ORM object's column values."""
    if obj is None:
        return {}
    exclude = exclude or set()
    mapper = inspect(obj).mapper
    data: dict[str, Any] = {}
    for column in mapper.columns:
        key = column.key
        if key in exclude:
            continue
        try:
            data[key] = _coerce(getattr(obj, key))
        except Exception:  # pragma: no cover - detached instances
            continue
    return data


def record(
    db: Session,
    *,
    action: str,
    entity_type: str,
    entity_id: Any = "",
    user: User | None = None,
    username: str | None = None,
    before: Any = None,
    after: Any = None,
    details: str = "",
    ip_address: str = "",
    commit: bool = False,
) -> AuditLog:
    """Append an audit entry.

    `before`/`after` may be ORM objects or plain dicts. The caller controls the
    transaction; pass commit=True only when the audit entry is the sole change.
    """
    before_data = snapshot(before) if before is not None and not isinstance(before, dict) else before
    after_data = snapshot(after) if after is not None and not isinstance(after, dict) else after

    entry = AuditLog(
        user_id=user.id if user else None,
        username=username or (user.username if user else "system"),
        action=action,
        entity_type=entity_type,
        entity_id=str(entity_id) if entity_id != "" else "",
        before=before_data,
        after=after_data,
        details=details,
        ip_address=ip_address,
    )
    db.add(entry)
    if commit:
        db.commit()
    return entry
