"""The pharmacy's business clock (timezone from the ``timezone`` setting)."""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy.orm import Session

from app.services.settings_service import get_setting
from app.utils import business_date, business_now


def timezone_name(db: Session) -> str:
    return get_setting(db, "timezone", "Asia/Kolkata")


def current_business_date(db: Session) -> date:
    return business_date(timezone_name(db))


def now(db: Session) -> datetime:
    """Naive wall-clock time in the pharmacy timezone."""
    return business_now(timezone_name(db))
