"""Shared helpers: time, JSON coercion, money rounding, id formatting."""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Any


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def business_now(tz_name: str = "Asia/Kolkata") -> datetime:
    """Current wall-clock time in the pharmacy's timezone (naive)."""
    try:
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo(tz_name)).replace(tzinfo=None)
    except Exception:
        return datetime.now(timezone.utc).replace(tzinfo=None)


def business_date(tz_name: str = "Asia/Kolkata") -> date:
    """Today's date in the pharmacy's configured timezone.

    The server is authoritative for the business date so a counter report never
    changes because of the client computer's timezone (important at midnight).
    """
    try:
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo(tz_name)).date()
    except Exception:
        return datetime.now(timezone.utc).date()


def to_local(moment: datetime | None, tz_name: str = "Asia/Kolkata") -> datetime | None:
    """A stored (naive UTC) timestamp as wall-clock time in the pharmacy's timezone."""
    if moment is None:
        return None
    try:
        from zoneinfo import ZoneInfo

        return moment.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(tz_name)).replace(tzinfo=None)
    except Exception:
        return moment


def today_start() -> datetime:
    now = utcnow()
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def to_decimal(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if value is None or value == "":
        return Decimal("0")
    return Decimal(str(value))


def money(value: Any) -> Decimal:
    """Round to 2 decimal places (banker's-safe half-up)."""
    return to_decimal(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _group_indian(int_str: str) -> str:
    """Group digits Indian-style: 1,500 / 12,500 / 1,00,000 / 12,34,567."""
    if len(int_str) <= 3:
        return int_str
    head, tail = int_str[:-3], int_str[-3:]
    parts: list[str] = []
    while len(head) > 2:
        parts.insert(0, head[-2:])
        head = head[:-2]
    if head:
        parts.insert(0, head)
    return ",".join(parts + [tail])


def format_inr(value: Any, symbol: bool = True) -> str:
    """Format a monetary value as Indian rupees, e.g. ₹1,500.00.

    Never emits negative zero. Negative amounts use the proper minus sign.
    """
    d = to_decimal(value)
    negative = d < 0
    d = abs(d).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    int_part, frac = f"{d:.2f}".split(".")
    text = _group_indian(int_part) + "." + frac
    sign = "\u2212" if (negative and d != 0) else ""
    return sign + ("\u20b9" if symbol else "") + text


def format_deduction(value: Any) -> str:
    """Format a cash deduction: ₹0.00 at zero, otherwise −₹X.XX."""
    d = to_decimal(value)
    if d == 0:
        return "\u20b90.00"
    return "\u2212" + format_inr(d)


def round_off_amount(total: Any) -> tuple[Decimal, Decimal]:
    """Return (rounded_total, round_off_delta) using nearest rupee.

    Business rule: invoice round-off is to the nearest
    whole rupee (0.50 rounds up), delta is signed and reported separately.
    """
    total_d = money(total)
    nearest = total_d.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return nearest, (nearest - total_d)


def format_id(prefix: str, number: int, width: int = 6) -> str:
    return f"{prefix}{number:0{width}d}"
