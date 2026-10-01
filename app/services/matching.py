"""Exact product matching for invoice review.

The review queue deliberately does **not** derive a "confidence" from fuzzy
similarity against the item master. That catalogue is not a trusted reference,
and fuzzy scoring was both slow (a master search per line) and misleading (a
high score against bad data is not evidence).

A parsed line is matched only on an exact identifier:

1. barcode / article id (exact), then
2. exact product name (case-insensitive, whitespace-collapsed).

Anything else is treated as a new product for a human to name in the review
queue. No thresholds and no weights.
"""
from __future__ import annotations

import re

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Item


def normalize_name(name: str) -> str:
    text = (name or "").strip().lower()
    return re.sub(r"\s+", " ", text)


def find_exact_match(db: Session, name: str, barcode: str = "") -> Item | None:
    """Return an existing *available* item only on an exact identifier match.

    Items in the recycle bin are ignored so an import/push never quietly adds
    stock to a deleted item; the caller creates a fresh item instead.
    """
    code = (barcode or "").strip()
    if code:
        item = db.scalar(
            select(Item).where(Item.barcode == code, Item.deleted_at.is_(None)).limit(1)
        )
        if item is None:
            item = db.scalar(
                select(Item).where(Item.article_id == code, Item.deleted_at.is_(None)).limit(1)
            )
        if item is not None:
            return item
    key = normalize_name(name)
    if not key:
        return None
    return db.scalar(
        select(Item).where(func.lower(Item.name) == key, Item.deleted_at.is_(None)).limit(1)
    )


def find_exact_matches(db: Session, names: list[str]) -> dict[str, Item]:
    """Batch exact-name lookup. Returns ``{normalized_name: Item}``.

    Recycle-bin items are excluded (see :func:`find_exact_match`).
    """
    keys = {normalize_name(n) for n in names if normalize_name(n)}
    if not keys:
        return {}
    rows = db.scalars(
        select(Item).where(func.lower(Item.name).in_(keys), Item.deleted_at.is_(None))
    )
    return {normalize_name(item.name): item for item in rows}
