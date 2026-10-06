"""Change counters for multi-counter work (two PCs on the same ERP).

Every committed change to a table bumps its area's counter in ``sync_versions`` in the same
transaction (ORM inserts / updates / deletes, and bulk ``update()`` / ``delete()`` statements). The
browser polls :func:`versions` every few seconds and refreshes the screens whose area changed, so one
counter sees the other's purchases, stock and sales without reloading the page. Nothing is pushed or
locked: the database stays the single source of truth; this only says *what* to re-read.
"""
from __future__ import annotations

from sqlalchemy import event, select, update
from sqlalchemy.orm import Session

from app.models import SyncVersion
from app.utils import utcnow

AREAS = ("inventory", "purchases", "sales", "customers", "locations", "masters")
TABLE_AREAS = {
    "items": ("inventory",), "batches": ("inventory",), "inventory_movements": ("inventory",),
    "stock_adjustments": ("inventory",), "product_packagings": ("inventory",), "item_uoms": ("inventory",),
    "purchases": ("purchases",), "purchase_items": ("purchases",), "purchase_returns": ("purchases", "inventory"),
    "suppliers": ("purchases",), "supplier_product_maps": ("purchases",),
    "sales": ("sales",), "sale_items": ("sales",), "sale_payments": ("sales",), "sale_returns": ("sales",),
    "sale_return_items": ("sales",), "parked_sales": ("sales",), "manual_bills": ("sales",), "manual_bill_items": ("sales",),
    "udhaar_entries": ("sales", "customers"), "udhaar_payments": ("sales", "customers"), "udhaar_reminders": ("customers",),
    "customers": ("customers",), "customer_followups": ("customers",),
    "racks": ("locations",), "rack_boxes": ("locations",), "item_locations": ("locations", "inventory"),
    "location_events": ("locations",),
    "categories": ("masters", "inventory"), "item_forms": ("masters",),
}


def _mark(session: Session, tables) -> None:
    areas = session.info.setdefault("sync_areas", set())
    for t in tables:
        areas.update(TABLE_AREAS.get(t, ()))


def _after_flush(session, flush_context):
    _mark(session, {getattr(o, "__tablename__", "") for o in (*session.new, *session.dirty, *session.deleted)
                    if not isinstance(o, SyncVersion)})


def _orm_execute(state):
    if state.is_update or state.is_delete or state.is_insert:
        table = getattr(state.statement, "table", None)
        if table is not None and table.name != "sync_versions":
            _mark(state.session, {table.name})


def _before_commit(session):
    areas = session.info.pop("sync_areas", None)
    if not areas:
        return
    now = utcnow()
    for area in sorted(areas):                      # a fixed order: two counters never wait on each other in a cycle
        done = session.execute(update(SyncVersion).where(SyncVersion.area == area)
                               .values(version=SyncVersion.version + 1, changed_at=now)
                               .execution_options(synchronize_session=False)).rowcount
        if not done:                                # (a database created before the table was seeded)
            session.add(SyncVersion(area=area, version=1, changed_at=now))
    session.info.pop("sync_areas", None)            # (the statements above marked nothing new)


def _after_rollback(session):
    session.info.pop("sync_areas", None)


def install(session_class) -> None:
    """Attach the hooks to the application's session factory (idempotent)."""
    if getattr(session_class, "_faheem_sync", False):
        return
    event.listen(session_class, "after_flush", _after_flush)
    event.listen(session_class, "do_orm_execute", _orm_execute)
    event.listen(session_class, "before_commit", _before_commit)
    event.listen(session_class, "after_soft_rollback", lambda s, prev: _after_rollback(s))
    session_class._faheem_sync = True


def versions(db: Session) -> dict[str, int]:
    rows = dict(db.execute(select(SyncVersion.area, SyncVersion.version)).all())
    return {a: int(rows.get(a, 0)) for a in AREAS}
