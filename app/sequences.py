"""Atomic, gap-tolerant human-readable ID generation.

Counters live in the `number_sequences` table and are incremented inside the
caller's transaction, so a rollback also rolls back the consumed number.
"""
from __future__ import annotations

import re
from datetime import datetime

from sqlalchemy.orm import Session

from app.models import NumberSequence
from app.utils import format_id


def next_number(db: Session, key: str) -> int:
    seq = db.get(NumberSequence, key, with_for_update=True)   # row lock on PostgreSQL: two tills never get one number
    if seq is None:
        seq = NumberSequence(key=key, next_value=1)
        db.add(seq)
        db.flush()
    value = seq.next_value
    seq.next_value = value + 1
    db.flush()
    return value


def article_prefix(name: str) -> str:
    """First 3 alphanumeric characters of the product name, uppercased.

    Shorter names are padded with 'X' so the Article ID is always 3 + 4 chars
    (e.g. "Augmentin 625" -> AUG0001).
    """
    cleaned = re.sub(r"[^A-Za-z0-9]", "", name or "").upper()
    if len(cleaned) >= 3:
        return cleaned[:3]
    return cleaned.ljust(3, "X") or "XXX"


def next_article_id_for(db: Session, name: str) -> str:
    """Article ID = 3-char product prefix + 4-digit per-prefix serial.

    Serial numbers are tracked per prefix so the 4-digit space is not exhausted
    globally (the catalogue has ~7,600 prefixes; the largest holds <5,000 items).
    """
    prefix = article_prefix(name)
    value = next_number(db, f"article:{prefix}")
    return f"{prefix}{value:04d}"


def next_article_id(db: Session, prefix: str = "ART") -> str:
    """Legacy global article sequence (kept for compatibility)."""
    return format_id(prefix, next_number(db, "article_id"), width=6)


def next_customer_id(db: Session, prefix: str = "CUST") -> str:
    return format_id(prefix, next_number(db, "customer_id"), width=6)


def next_employee_id(db: Session, prefix: str = "EMP") -> str:
    return format_id(prefix, next_number(db, "employee_id"), width=4)


def next_manual_bill_no(db: Session, when: datetime | None = None) -> str:
    """Manual bills have their own series so they never interleave with stock invoices."""
    when = when or datetime.now()
    day = when.strftime("%Y%m%d")
    return f"MB-{day}-{next_number(db, f'manual:{day}'):04d}"


def next_invoice_no(db: Session, when: datetime | None = None) -> str:
    when = when or datetime.now()
    day = when.strftime("%Y%m%d")
    seq = next_number(db, f"invoice:{day}")
    return f"INV-{day}-{seq:04d}"
