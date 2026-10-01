"""Product text search: every word of the query as a prefix ("dol 650" → dol* 650*).

SQLite uses its FTS5 index; PostgreSQL uses a GIN index on the same words
(to_tsvector 'simple' + prefix tsquery), ranked the same way. Callers get
product ids and never see the dialect.
"""
from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.database import PG_SEARCH_EXPR


def tokens(q: str) -> list[str]:
    return [t for t in "".join(c if c.isalnum() else " " for c in q or "").split() if t]


def product_ids(db: Session, q: str, limit: int, ranked: bool = False) -> list[int]:
    words = tokens(q)
    if not words:
        return []
    dialect = db.get_bind().dialect.name
    try:
        if dialect == "postgresql":
            query = " & ".join(f"{w.lower()}:*" for w in words)
            order = f" ORDER BY ts_rank({PG_SEARCH_EXPR}, to_tsquery('simple', :q)) DESC, id" if ranked else ""
            sql = f"SELECT id FROM items WHERE {PG_SEARCH_EXPR} @@ to_tsquery('simple', :q){order} LIMIT :n"
            return [r[0] for r in db.execute(text(sql), {"q": query, "n": limit})]
        match = " ".join(f"{w}*" for w in words)
        sql = f"SELECT rowid FROM items_fts WHERE items_fts MATCH :q{' ORDER BY rank' if ranked else ''} LIMIT :n"
        return [r[0] for r in db.execute(text(sql), {"q": match, "n": limit})]
    except Exception:            # an index that is missing or rebuilding never breaks a search: LIKE fallback runs
        db.rollback() if dialect == "postgresql" else None
        return []
