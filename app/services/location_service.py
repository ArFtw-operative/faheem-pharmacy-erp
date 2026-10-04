"""Rack / box locations: the masters, where each product is, every move, and the past.

Model (DECISIONS.md D25-D30):

* ``racks`` and ``rack_boxes`` are masters referred to by id. A box belongs to one rack;
  the database itself refuses a (rack, box) pair that does not belong together.
* ``item_locations`` is dated: a move closes the open row and opens a new one. At most one
  open row per product (a partial unique index); ``batch_id`` is the extension point for
  stock kept in several places.
* ``location_events`` is append-only and copies codes / names at the time, so history keeps
  its meaning after a rename. A bulk operation shares one ``operation_id``.
* Location never changes stock. "Where was it on 30 Sep" is answered from the dated rows and
  the stock ledger (``inventory_movements``), the same source as every stock report: nothing
  is copied into snapshot tables, nothing is reconstructed from today's state.

Every write goes through this module; routers and screens never touch the tables.
"""
from __future__ import annotations

import difflib
import re
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.orm import Session

from app import audit
from app.models import AuditLog, Batch, Category, InventoryMovement, Item, ItemLocation, LocationEvent, Rack, RackBox, User
from app.services import business_time, settings_service, units
from app.utils import utcnow

CODE_RE = re.compile(r"^[A-Z0-9][A-Z0-9 ._-]{0,29}$")
SETTINGS = {
    # key: (default, label) — the defaults are what a pharmacy gets without touching anything
    "location_boxes_enabled": ("0", "Box / bin locations inside racks"),
    "location_require_box": ("0", "A box is required when boxes are on"),
    "location_require_rack_on_receipt": ("0", "Received stock must have a rack before a purchase posts"),
    "pos_show_location": ("1", "Show rack and box in POS search and the bill"),
    "pos_search_by_location": ("1", "POS search finds products by rack / box code"),
}


class LocationError(Exception):
    pass


class NotEmpty(LocationError):
    """A rack or box still holds products: the caller must say where they go."""

    def __init__(self, message: str, products: int, units_: int):
        super().__init__(message)
        self.products, self.units = products, units_


# --------------------------------------------------------------------------- settings
def config(db: Session) -> dict[str, bool]:
    return {k: settings_service.get_setting(db, k, d) == "1" for k, (d, _) in SETTINGS.items()}


def set_config(db: Session, values: dict[str, Any], *, user: User | None = None, ip_address: str = "") -> dict[str, bool]:
    for key, value in values.items():
        if key not in SETTINGS:
            raise LocationError(f"Unknown setting {key}")
        settings_service.set_setting(db, key, "1" if value in (True, "1", 1, "on", "true") else "0", user=user, ip_address=ip_address)
    return config(db)


# --------------------------------------------------------------------------- labels
def normalize_code(value: Any) -> str:
    return " ".join(str(value or "").strip().upper().split())


def _check_code(code: str, what: str) -> str:
    code = normalize_code(code)
    if not code:
        raise LocationError(f"{what} code is required")
    if not CODE_RE.match(code):
        raise LocationError(f"{what} code {code!r}: use letters, digits, space, - _ . (up to 30)")
    return code


def label(rack: Rack | None, box: RackBox | None = None, *, names: bool = True) -> str:
    if rack is None:
        return ""
    text = f"{rack.code} {rack.name}".strip() if names and rack.name and rack.name != rack.code else rack.code
    if box is not None:
        text += f" / {box.code}" + (f" {box.name}" if names and box.name and box.name != box.code else "")
    return text


@dataclass
class Loc:
    rack_id: int
    rack_code: str
    rack_name: str
    rack_active: bool
    box_id: int | None = None
    box_code: str = ""
    box_name: str = ""
    since: datetime | None = None

    @property
    def short(self) -> str:
        return self.rack_code + (f" / {self.box_code}" if self.box_code else "")

    def as_dict(self) -> dict:
        return {"rack_id": self.rack_id, "rack": self.rack_code, "rack_name": self.rack_name, "rack_active": self.rack_active,
                "box_id": self.box_id, "box": self.box_code, "box_name": self.box_name, "label": self.short,
                "since": self.since.isoformat() if self.since else ""}


# --------------------------------------------------------------------------- racks
def _audit(db, action, entity_type, entity_id, user, before, after, details, ip):
    audit.record(db, action=action, entity_type=entity_type, entity_id=entity_id, user=user,
                 before=before, after=after, details=details, ip_address=ip)


def get_rack(db: Session, rack_id: Any) -> Rack:
    try:
        rack = db.get(Rack, int(rack_id))
    except (TypeError, ValueError):
        rack = None
    if rack is None:
        raise LocationError("Rack not found")
    return rack


def get_box(db: Session, box_id: Any) -> RackBox:
    try:
        box = db.get(RackBox, int(box_id))
    except (TypeError, ValueError):
        box = None
    if box is None:
        raise LocationError("Box not found")
    return box


def create_rack(db: Session, *, code: str, name: str = "", description: str = "", sort_order: Any = None,
                user: User | None = None, ip_address: str = "") -> Rack:
    code = _check_code(code, "Rack")
    if db.scalar(select(Rack.id).where(Rack.code == code)):
        raise LocationError(f"Rack code {code} already exists")
    order = int(sort_order) if str(sort_order or "").strip().lstrip("-").isdigit() else \
        int(db.scalar(select(func.coalesce(func.max(Rack.sort_order), 0))) or 0) + 10
    rack = Rack(code=code, name=" ".join(str(name or "").split())[:80], description=str(description or "")[:2000],
                sort_order=order, created_by=user.id if user else None, updated_by=user.id if user else None)
    db.add(rack)
    db.flush()
    _audit(db, audit.A_CREATE, "rack", rack.id, user, None, audit.snapshot(rack), f"Rack {code} created", ip_address)
    return rack


def update_rack(db: Session, rack: Rack, *, user: User | None = None, ip_address: str = "", **fields) -> Rack:
    before = audit.snapshot(rack)
    if "code" in fields:
        code = _check_code(fields["code"], "Rack")
        if code != rack.code and db.scalar(select(Rack.id).where(Rack.code == code, Rack.id != rack.id)):
            raise LocationError(f"Rack code {code} already exists")
        rack.code = code
    if "name" in fields:
        rack.name = " ".join(str(fields["name"] or "").split())[:80]
    if "description" in fields:
        rack.description = str(fields["description"] or "")[:2000]
    if "sort_order" in fields and str(fields["sort_order"]).strip().lstrip("-").isdigit():
        rack.sort_order = int(fields["sort_order"])
    if "zone" in fields:
        rack.zone = str(fields["zone"] or "")[:40]
    rack.updated_by = user.id if user else None
    rack.updated_at = utcnow()
    db.flush()
    after = audit.snapshot(rack)
    changed = {k for k in after if before.get(k) != after.get(k)} - {"updated_at", "updated_by"}
    if changed:
        _audit(db, audit.A_UPDATE, "rack", rack.id, user, before, after,
               f"Rack {rack.code}: {', '.join(sorted(changed))} changed", ip_address)
    return rack


def _contents(db: Session, *, rack_id: int, box_id: int | None = None) -> tuple[list[int], int]:
    """Products (not in the recycle bin) currently in a rack or box, and their stock."""
    cond = [ItemLocation.valid_to.is_(None), ItemLocation.batch_id.is_(None), ItemLocation.rack_id == rack_id,
            Item.deleted_at.is_(None)]
    if box_id is not None:
        cond.append(ItemLocation.box_id == box_id)
    ids = list(db.scalars(select(ItemLocation.item_id).join(Item, Item.id == ItemLocation.item_id).where(*cond)))
    stock = int(db.scalar(select(func.coalesce(func.sum(Batch.quantity), 0)).where(Batch.item_id.in_(ids), Batch.quantity > 0)) or 0) if ids else 0
    return ids, stock


def set_rack_status(db: Session, rack: Rack, active: bool, *, move_to_rack: Any = None, move_to_box: Any = None,
                    user: User | None = None, ip_address: str = "") -> dict:
    """Disable: only an empty rack, or after moving everything in it (one transaction). Enable: always."""
    moved = None
    if not active and rack.is_active:
        ids, stock = _contents(db, rack_id=rack.id)
        if ids and not move_to_rack:
            raise NotEmpty(f"Rack {rack.code} holds {len(ids)} product(s), {stock} unit(s). Move them to another rack first.",
                           len(ids), stock)
        if ids:
            if int(move_to_rack) == rack.id:
                raise LocationError("Choose a different rack to move the products to")
            moved = assign(db, ids, move_to_rack, move_to_box, source="RACK", reason=f"Rack {rack.code} disabled",
                           user=user, ip_address=ip_address, atomic=True)
    if rack.is_active != bool(active):
        before = {"is_active": rack.is_active}
        rack.is_active = bool(active)
        rack.updated_by = user.id if user else None
        rack.updated_at = utcnow()
        db.flush()
        _audit(db, audit.A_UPDATE, "rack", rack.id, user, before, {"is_active": rack.is_active},
               f"Rack {rack.code} {'enabled' if active else 'disabled'}", ip_address)
    return {"rack": rack_row(db, rack), "moved": moved}


# --------------------------------------------------------------------------- boxes
def create_box(db: Session, rack: Rack, *, code: str, name: str = "", description: str = "",
               user: User | None = None, ip_address: str = "") -> RackBox:
    if not config(db)["location_boxes_enabled"]:
        raise LocationError("Box locations are turned off (Racks → Settings)")
    code = _check_code(code, "Box")
    if db.scalar(select(RackBox.id).where(RackBox.rack_id == rack.id, RackBox.code == code)):
        raise LocationError(f"Rack {rack.code} already has box {code}")
    order = int(db.scalar(select(func.coalesce(func.max(RackBox.sort_order), 0)).where(RackBox.rack_id == rack.id)) or 0) + 10
    box = RackBox(rack_id=rack.id, code=code, name=" ".join(str(name or "").split())[:80], description=str(description or "")[:2000],
                  sort_order=order, created_by=user.id if user else None, updated_by=user.id if user else None)
    db.add(box)
    db.flush()
    _audit(db, audit.A_CREATE, "rack_box", box.id, user, None, audit.snapshot(box), f"Box {rack.code} / {code} created", ip_address)
    return box


def update_box(db: Session, box: RackBox, *, user: User | None = None, ip_address: str = "", **fields) -> RackBox:
    before = audit.snapshot(box)
    if "code" in fields:
        code = _check_code(fields["code"], "Box")
        if code != box.code and db.scalar(select(RackBox.id).where(RackBox.rack_id == box.rack_id, RackBox.code == code, RackBox.id != box.id)):
            raise LocationError(f"This rack already has box {code}")
        box.code = code
    if "name" in fields:
        box.name = " ".join(str(fields["name"] or "").split())[:80]
    if "description" in fields:
        box.description = str(fields["description"] or "")[:2000]
    box.updated_by = user.id if user else None
    box.updated_at = utcnow()
    db.flush()
    after = audit.snapshot(box)
    if {k for k in after if before.get(k) != after.get(k)} - {"updated_at", "updated_by"}:
        _audit(db, audit.A_UPDATE, "rack_box", box.id, user, before, after, f"Box {box.rack.code} / {box.code} changed", ip_address)
    return box


def set_box_status(db: Session, box: RackBox, active: bool, *, then: str = "", move_to_box: Any = None,
                   user: User | None = None, ip_address: str = "") -> dict:
    """Disable a box holding products only with ``then``: ``move`` (to another box) or ``clear`` (keep the rack)."""
    moved = None
    if not active and box.is_active:
        ids, stock = _contents(db, rack_id=box.rack_id, box_id=box.id)
        if ids and then not in ("move", "clear"):
            raise NotEmpty(f"Box {box.rack.code} / {box.code} holds {len(ids)} product(s), {stock} unit(s). "
                           "Move them to another box, or keep them in the rack without a box.", len(ids), stock)
        if ids and then == "move":
            target = get_box(db, move_to_box)
            if target.id == box.id:
                raise LocationError("Choose a different box")
            moved = assign(db, ids, target.rack_id, target.id, source="RACK", reason=f"Box {box.code} disabled",
                           user=user, ip_address=ip_address, atomic=True)
        elif ids:
            moved = assign(db, ids, box.rack_id, None, source="RACK", reason=f"Box {box.code} disabled",
                           user=user, ip_address=ip_address, atomic=True, allow_no_box=True)
    if box.is_active != bool(active):
        if active and not box.rack.is_active:
            raise LocationError(f"Enable rack {box.rack.code} first")
        before = {"is_active": box.is_active}
        box.is_active = bool(active)
        box.updated_by = user.id if user else None
        box.updated_at = utcnow()
        db.flush()
        _audit(db, audit.A_UPDATE, "rack_box", box.id, user, before, {"is_active": box.is_active},
               f"Box {box.rack.code} / {box.code} {'enabled' if active else 'disabled'}", ip_address)
    return {"moved": moved}


# --------------------------------------------------------------------------- where things are now
def current(db: Session, item_ids: Iterable[int]) -> dict[int, Loc]:
    """Current product-level location of each product (one indexed query)."""
    ids = list({int(i) for i in item_ids})
    if not ids:
        return {}
    rows = db.execute(
        select(ItemLocation.item_id, Rack.id, Rack.code, Rack.name, Rack.is_active, RackBox.id, RackBox.code, RackBox.name,
               ItemLocation.valid_from)
        .join(Rack, Rack.id == ItemLocation.rack_id)
        .outerjoin(RackBox, RackBox.id == ItemLocation.box_id)
        .where(ItemLocation.item_id.in_(ids), ItemLocation.valid_to.is_(None), ItemLocation.batch_id.is_(None)))
    return {r[0]: Loc(r[1], r[2], r[3] or "", bool(r[4]), r[5], r[6] or "", r[7] or "", r[8]) for r in rows}


def _stock(db: Session, item_ids: list[int]) -> dict[int, int]:
    if not item_ids:
        return {}
    return {i: int(q or 0) for i, q in db.execute(
        select(Batch.item_id, func.sum(Batch.quantity)).where(Batch.item_id.in_(item_ids), Batch.quantity > 0).group_by(Batch.item_id))}


def _target(db: Session, rack_id: Any, box_id: Any, *, allow_no_box: bool = False) -> tuple[Rack, RackBox | None]:
    rack = get_rack(db, rack_id)
    if not rack.is_active:
        raise LocationError(f"Rack {rack.code} is disabled: enable it or choose another rack")
    cfg = config(db)
    box = None
    if box_id not in (None, "", 0, "0"):
        if not cfg["location_boxes_enabled"]:
            raise LocationError("Box locations are turned off (Racks → Settings)")
        box = get_box(db, box_id)
        if box.rack_id != rack.id:
            raise LocationError(f"Box {box.code} belongs to rack {box.rack.code}, not {rack.code}")
        if not box.is_active:
            raise LocationError(f"Box {rack.code} / {box.code} is disabled")
    elif cfg["location_boxes_enabled"] and cfg["location_require_box"] and not allow_no_box:
        raise LocationError("Choose a box: a box is required (Racks → Settings)")
    return rack, box


def _parse_expected(expected: Any) -> dict[int, tuple[int | None, int | None]]:
    """What the screen showed: ``{item_id: {"rack_id": r, "box_id": b}}`` (None = unassigned)."""
    out = {}
    for key, value in (expected or {}).items():
        try:
            out[int(key)] = (None, None) if not value else (int(value["rack_id"]) if value.get("rack_id") else None,
                                                           int(value["box_id"]) if value.get("box_id") else None)
        except (TypeError, ValueError, KeyError, AttributeError):
            raise LocationError("Malformed expected locations")
    return out


def _ids(item_ids: Any) -> list[int]:
    if not isinstance(item_ids, (list, tuple, set)) or not item_ids:
        raise LocationError("Select products first")
    try:
        ids = list(dict.fromkeys(int(i) for i in item_ids))
    except (TypeError, ValueError):
        raise LocationError("Product ids must be numbers")
    if len(ids) > 10000:
        raise LocationError("Move at most 10,000 products at once")
    return ids


def _change(db: Session, *, item: Item, open_row: ItemLocation | None, old: Loc | None, rack: Rack | None,
            box: RackBox | None, now: datetime, op: str, source: str, reason: str, reference: str,
            user: User | None, stock: int | None) -> str | None:
    """Close the open row (guarded) and open the new one. Returns a failure reason or None."""
    if open_row is not None:
        closed = db.execute(update(ItemLocation).where(ItemLocation.id == open_row.id, ItemLocation.valid_to.is_(None))
                            .values(valid_to=now).execution_options(synchronize_session=False)).rowcount
        if closed != 1:
            return "moved by another user a moment ago — reload and try again"
    kind = ("UNASSIGNED" if rack is None else "ASSIGNED" if old is None
            else "BOX_CHANGED" if old.rack_id == rack.id else "MOVED")
    from_label = (f"{old.rack_code} {old.rack_name}".strip() + (f" / {old.box_code} {old.box_name}".rstrip() if old.box_code else "")) if old else ""
    event = LocationEvent(
        operation_id=op, event_type=kind, source=source, item_id=item.id,
        from_rack_id=old.rack_id if old else None, from_box_id=old.box_id if old else None,
        to_rack_id=rack.id if rack else None, to_box_id=box.id if box else None,
        from_label=from_label[:200], to_label=label(rack, box)[:200], stock_snapshot=stock,
        reason=str(reason or "")[:2000], reference=str(reference or "")[:60],
        user_id=user.id if user else None, username=user.username if user else "system", created_at=now)
    db.add(event)
    db.flush()
    if rack is not None:
        db.add(ItemLocation(item_id=item.id, rack_id=rack.id, box_id=box.id if box else None, valid_from=now,
                            created_by=user.id if user else None, event_id=event.id))
    return None


def assign(db: Session, item_ids: Any, rack_id: Any, box_id: Any = None, *, reason: str = "", source: str = "MANUAL",
           reference: str = "", expected: Any = None, atomic: bool = False, allow_no_box: bool = False,
           user: User | None = None, ip_address: str = "") -> dict:
    """Put products in a rack (and box). ``rack_id`` None = unassign.

    Every product is validated on the server: a product in the recycle bin, or one another user
    moved since the screen loaded (``expected``), is reported, never silently changed. With
    ``atomic`` any problem fails the whole operation (the caller rolls back); otherwise the
    valid products change and each other one comes back with its reason. A product already
    there is skipped without a write.
    """
    ids = _ids(item_ids)
    rack, box = (None, None) if rack_id in (None, "", 0) else _target(db, rack_id, box_id, allow_no_box=allow_no_box)
    want = _parse_expected(expected)
    items = {i.id: i for i in db.scalars(select(Item).where(Item.id.in_(ids)))}
    lock = select(ItemLocation).where(ItemLocation.item_id.in_(ids), ItemLocation.valid_to.is_(None), ItemLocation.batch_id.is_(None))
    if db.get_bind().dialect.name == "postgresql":
        lock = lock.with_for_update()            # two users moving the same product: the second waits, then re-checks
    open_rows = {r.item_id: r for r in db.scalars(lock)}
    now_loc = current(db, ids)
    stock = _stock(db, ids)
    now = utcnow()
    op = str(uuid.uuid4())
    processed, skipped, failed = [], [], []
    for item_id in ids:
        item = items.get(item_id)
        if item is None:
            failed.append({"id": item_id, "name": "", "reason": "product not found"})
            continue
        if item.deleted_at is not None:
            failed.append({"id": item_id, "name": item.name, "reason": "in the recycle bin — restore it first"})
            continue
        old = now_loc.get(item_id)
        have = (old.rack_id, old.box_id) if old else (None, None)
        if item_id in want and want[item_id] != have:
            failed.append({"id": item_id, "name": item.name,
                           "reason": f"changed by another user since you looked (now {old.short if old else 'unassigned'})"})
            continue
        if have == ((rack.id if rack else None), (box.id if box else None)):
            skipped.append({"id": item_id, "name": item.name, "reason": f"already {'in ' + old.short if old else 'unassigned'}"})
            continue
        problem = _change(db, item=item, open_row=open_rows.get(item_id), old=old, rack=rack, box=box, now=now, op=op,
                          source=source, reason=reason, reference=reference, user=user, stock=stock.get(item_id, 0))
        if problem:
            failed.append({"id": item_id, "name": item.name, "reason": problem})
        else:
            processed.append(item_id)
    if atomic and failed:
        raise LocationError(f"{len(failed)} product(s) could not move: " + "; ".join(f"{f['name'] or f['id']}: {f['reason']}" for f in failed[:5]))
    db.flush()
    target = label(rack, box) if rack else "Unassigned"
    result = {"operation_id": op, "requested": len(ids), "processed": len(processed), "processed_ids": processed,
              "skipped": skipped, "failed": failed, "target": target,
              "target_short": (rack.code + (f" / {box.code}" if box else "")) if rack else "Unassigned"}
    if processed or failed:
        _audit(db, audit.A_UPDATE, "location_operation", op, user, None,
               {"source": source, "target": target, "requested": len(ids), "processed": len(processed),
                "skipped": len(skipped), "failed": failed[:100], "reference": reference},
               f"{'Unassigned' if rack is None else 'Moved to ' + target}: {len(processed)} of {len(ids)} product(s)"
               + (f", {len(failed)} failed" if failed else ""), ip_address)
        db.flush()
    return result


def unassign(db: Session, item_ids: Any, **kwargs) -> dict:
    return assign(db, item_ids, None, None, **kwargs)


# --------------------------------------------------------------------------- suggestions
def suggest(db: Session, items: list[Item]) -> dict[int, dict]:
    """Where received stock should go — a suggestion, never applied by itself.

    CURRENT: the product already has a location · CATEGORY: the rack set for its category ·
    USUAL: most of its category's products are in one rack · LAST: where it was before.
    """
    out: dict[int, dict] = {}
    if not items:
        return out
    ids = [i.id for i in items]
    now = current(db, ids)
    for it in items:
        if it.id in now:
            out[it.id] = {**now[it.id].as_dict(), "source": "CURRENT"}
    rest = [it for it in items if it.id not in out]
    if not rest:
        return out
    by_cat = category_suggestions(db, {it.category for it in rest})
    last = {}
    for item_id, rid, bid in db.execute(
            select(ItemLocation.item_id, ItemLocation.rack_id, ItemLocation.box_id)
            .where(ItemLocation.item_id.in_([it.id for it in rest]), ItemLocation.batch_id.is_(None))
            .order_by(ItemLocation.valid_from)):
        last[item_id] = (rid, bid)
    racks = {r.id: r for r in db.scalars(select(Rack).where(Rack.is_active.is_(True)))}
    boxes = {b.id: b for b in db.scalars(select(RackBox).where(RackBox.is_active.is_(True)))}
    for it in rest:
        rid, bid = last.get(it.id, (None, None))
        if rid in racks:
            out[it.id] = _suggestion(racks[rid], boxes.get(bid), "LAST")
        elif it.category in by_cat:
            out[it.id] = by_cat[it.category]
    return out


def _suggestion(rack: Rack, box: RackBox | None, source: str) -> dict:
    return {"rack_id": rack.id, "rack": rack.code, "rack_name": rack.name, "rack_active": True,
            "box_id": box.id if box else None, "box": box.code if box else "", "box_name": box.name if box else "",
            "label": rack.code + (f" / {box.code}" if box else ""), "source": source}


def category_suggestions(db: Session, categories: Iterable[str]) -> dict[str, dict]:
    """Category → suggested rack: the rack set for the category (CATEGORY), else the rack most of
    its products are already in, when that is a clear habit (USUAL: 3+ products, 60%+)."""
    cats = {c for c in categories if c}
    if not cats:
        return {}
    racks = {r.id: r for r in db.scalars(select(Rack).where(Rack.is_active.is_(True)))}
    out = {c.code: _suggestion(racks[c.default_rack_id], None, "CATEGORY")
           for c in db.scalars(select(Category).where(Category.code.in_(cats), Category.default_rack_id.is_not(None)))
           if c.default_rack_id in racks}
    per_cat: dict[str, list] = defaultdict(list)
    for cat, rid, n in db.execute(
            select(Item.category, ItemLocation.rack_id, func.count()).join(ItemLocation, ItemLocation.item_id == Item.id)
            .where(Item.category.in_(cats - set(out)), ItemLocation.valid_to.is_(None), ItemLocation.batch_id.is_(None),
                   Item.deleted_at.is_(None))
            .group_by(Item.category, ItemLocation.rack_id)):
        per_cat[cat].append((n, rid))
    sizes = dict(db.execute(select(Item.category, func.count()).where(Item.category.in_(set(per_cat)), Item.deleted_at.is_(None))
                            .group_by(Item.category)).all()) if per_cat else {}
    for cat, pairs in per_cat.items():
        located = sum(n for n, _ in pairs)
        n, rid = max(pairs)
        # a habit only once the category is mostly organised (half its products have a rack) and one rack
        # clearly dominates; a few early placements never speak for a whole category
        if located >= 3 and located * 2 >= sizes.get(cat, 0) and n / located >= 0.6 and rid in racks:
            out[cat] = _suggestion(racks[rid], None, "USUAL")
    return out


def set_category_defaults(db: Session, rack: Rack, categories: list[str], *, user: User | None = None, ip_address: str = "") -> list[str]:
    wanted = {str(c).strip().upper() for c in categories or [] if str(c).strip()}
    known = set(db.scalars(select(Category.code)))
    if wanted - known:
        raise LocationError("Unknown category: " + ", ".join(sorted(wanted - known)))
    before = sorted(db.scalars(select(Category.code).where(Category.default_rack_id == rack.id)))
    for cat in db.scalars(select(Category).where(or_(Category.default_rack_id == rack.id, Category.code.in_(wanted)))):
        cat.default_rack_id = rack.id if cat.code in wanted else None
    db.flush()
    if before != sorted(wanted):
        _audit(db, audit.A_UPDATE, "rack", rack.id, user, {"categories": before}, {"categories": sorted(wanted)},
               f"Rack {rack.code}: preferred for {', '.join(sorted(wanted)) or 'no category'}", ip_address)
    return sorted(wanted)


# --------------------------------------------------------------------------- lists and statistics
def _expiry_cutoff(db: Session) -> date:
    return date.today() + timedelta(days=settings_service.get_int(db, "expiry_threshold_days", 90))


def rack_row(db: Session, rack: Rack, stats: dict | None = None) -> dict:
    s = stats if stats is not None else rack_stats(db, [rack.id]).get(rack.id, {})
    return {"id": rack.id, "code": rack.code, "name": rack.name, "description": rack.description, "active": bool(rack.is_active),
            "sort_order": rack.sort_order, "boxes": s.get("boxes", 0), "products": s.get("products", 0), "batches": s.get("batches", 0),
            "units": s.get("units", 0), "expiring": s.get("expiring", 0), "out_of_stock": s.get("out_of_stock", 0),
            "mrp_value": str(s.get("mrp_value", 0)), "updated_at": rack.updated_at.isoformat() if rack.updated_at else "",
            "categories": sorted(db.scalars(select(Category.code).where(Category.default_rack_id == rack.id)))}


def rack_stats(db: Session, rack_ids: list[int] | None = None, *, box_level: bool = False) -> dict:
    """Per rack (or per box): products, batches in stock, units, expiring soon, out of stock, MRP value."""
    key = ItemLocation.box_id if box_level else ItemLocation.rack_id
    q = (select(key, ItemLocation.item_id).join(Item, Item.id == ItemLocation.item_id)
         .where(ItemLocation.valid_to.is_(None), ItemLocation.batch_id.is_(None), Item.deleted_at.is_(None)))
    if rack_ids is not None:
        q = q.where(ItemLocation.rack_id.in_(rack_ids))
    where: dict[int, int] = {}
    members: dict[Any, list[int]] = defaultdict(list)
    for k, item_id in db.execute(q):
        members[k].append(item_id)
        where[item_id] = k
    out: dict[Any, dict] = {k: {"products": len(v), "batches": 0, "units": 0, "expiring": 0, "out_of_stock": 0, "mrp_value": 0}
                            for k, v in members.items()}
    cutoff = _expiry_cutoff(db)
    stocked: set[int] = set()
    if where:
        for item_id, qty, expiry, unit_mrp in db.execute(
                select(Batch.item_id, Batch.quantity, Batch.expiry_date, Batch.unit_mrp)
                .where(Batch.item_id.in_(list(where)), Batch.quantity > 0)):
            s = out[where[item_id]]
            s["batches"] += 1
            s["units"] += qty
            s["mrp_value"] = round(float(s["mrp_value"]) + float(unit_mrp or 0) * qty, 2)
            stocked.add(item_id)
            if expiry and expiry <= cutoff:
                s["expiring"] += 1
    for item_id, k in where.items():
        if item_id not in stocked:
            out[k]["out_of_stock"] += 1
    if not box_level:
        for rid, n in db.execute(select(RackBox.rack_id, func.count()).where(RackBox.is_active.is_(True)).group_by(RackBox.rack_id)):
            out.setdefault(rid, {"products": 0, "batches": 0, "units": 0, "expiring": 0, "out_of_stock": 0, "mrp_value": 0})["boxes"] = n
    return out


def list_racks(db: Session, *, q: str = "", status: str = "") -> list[dict]:
    stmt = select(Rack)
    if q.strip():
        like = f"%{q.strip()}%"
        stmt = stmt.where(or_(Rack.code.ilike(like), Rack.name.ilike(like), Rack.description.ilike(like)))
    if status == "active":
        stmt = stmt.where(Rack.is_active.is_(True))
    elif status == "inactive":
        stmt = stmt.where(Rack.is_active.is_(False))
    racks = list(db.scalars(stmt.order_by(Rack.sort_order, Rack.code)))
    stats = rack_stats(db, [r.id for r in racks])
    return [rack_row(db, r, stats.get(r.id, {})) for r in racks]


def list_boxes(db: Session, rack: Rack) -> list[dict]:
    stats = rack_stats(db, [rack.id], box_level=True)
    return [{"id": b.id, "rack_id": rack.id, "code": b.code, "name": b.name, "description": b.description, "active": bool(b.is_active),
             **{k: stats.get(b.id, {}).get(k, 0) for k in ("products", "batches", "units", "expiring", "out_of_stock")}}
            for b in rack.boxes]


def metrics(db: Session) -> dict:
    """The few numbers worth a glance (each opens the matching list)."""
    tz = ZoneInfo(business_time.timezone_name(db))
    today = datetime.now(tz).date()
    start = datetime.combine(today, time.min, tz).astimezone(timezone.utc).replace(tzinfo=None)
    live = select(Item.id).where(Item.deleted_at.is_(None))
    located = select(ItemLocation.item_id).where(ItemLocation.valid_to.is_(None), ItemLocation.batch_id.is_(None))
    return {
        "racks": db.scalar(select(func.count()).select_from(Rack)) or 0,
        "active_racks": db.scalar(select(func.count()).select_from(Rack).where(Rack.is_active.is_(True))) or 0,
        "unassigned": db.scalar(select(func.count()).select_from(Item).where(Item.deleted_at.is_(None), Item.id.not_in(located))) or 0,
        "without_box": db.scalar(select(func.count()).select_from(ItemLocation).where(
            ItemLocation.valid_to.is_(None), ItemLocation.batch_id.is_(None), ItemLocation.box_id.is_(None), ItemLocation.item_id.in_(live))) or 0,
        "moved_today": db.scalar(select(func.count(func.distinct(LocationEvent.item_id))).where(LocationEvent.created_at >= start)) or 0,
        "boxes_enabled": config(db)["location_boxes_enabled"],
    }


def location_condition(value: str, db: Session):
    """SQL condition on ``Item`` for an inventory Location filter value.

    "" · assigned · unassigned · nobox · rack:<id> · box:<id>
    """
    open_loc = and_(ItemLocation.valid_to.is_(None), ItemLocation.batch_id.is_(None))
    if value == "assigned":
        return Item.id.in_(select(ItemLocation.item_id).where(open_loc))
    if value == "unassigned":
        return Item.id.not_in(select(ItemLocation.item_id).where(open_loc))
    if value == "nobox":
        return Item.id.in_(select(ItemLocation.item_id).where(open_loc, ItemLocation.box_id.is_(None)))
    kind, _, ident = value.partition(":")
    if kind in ("rack", "box") and ident.isdigit():
        col = ItemLocation.rack_id if kind == "rack" else ItemLocation.box_id
        return Item.id.in_(select(ItemLocation.item_id).where(open_loc, col == int(ident)))
    return None


def find_by_code(db: Session, term: str) -> tuple[Rack | None, RackBox | None]:
    """A typed location code: ``R-A03`` (rack), ``R-A03/B02`` or ``R-A03 B02`` (box), or a box code
    that exists in exactly one rack. Used by POS and inventory search; never fuzzy."""
    text = normalize_code(term)
    if not text or len(text) > 61:
        return None, None
    rack = db.scalar(select(Rack).where(Rack.code == text))
    if rack:
        return rack, None
    for sep in ("/", " "):
        if sep in text:
            left, right = (normalize_code(x) for x in text.rsplit(sep, 1))
            rack = db.scalar(select(Rack).where(Rack.code == left))
            if rack:
                box = db.scalar(select(RackBox).where(RackBox.rack_id == rack.id, RackBox.code == right))
                if box:
                    return rack, box
    boxes = list(db.scalars(select(RackBox).where(RackBox.code == text).limit(2)))
    if len(boxes) == 1:
        return boxes[0].rack, boxes[0]
    return None, None


def items_at(db: Session, rack: Rack, box: RackBox | None = None, limit: int = 300) -> list[int]:
    cond = [ItemLocation.valid_to.is_(None), ItemLocation.batch_id.is_(None), ItemLocation.rack_id == rack.id]
    if box is not None:
        cond.append(ItemLocation.box_id == box.id)
    return list(db.scalars(select(ItemLocation.item_id).join(Item, Item.id == ItemLocation.item_id)
                           .where(*cond, Item.deleted_at.is_(None)).order_by(Item.name).limit(limit)))


# --------------------------------------------------------------------------- history
def _event_dict(e: LocationEvent) -> dict:
    return {"id": e.id, "operation_id": e.operation_id, "type": e.event_type, "source": e.source, "item_id": e.item_id,
            "from": e.from_label, "to": e.to_label, "from_rack_id": e.from_rack_id, "to_rack_id": e.to_rack_id,
            "stock": e.stock_snapshot, "reason": e.reason, "reference": e.reference, "user": e.username,
            "at": e.created_at.isoformat() if e.created_at else ""}


def item_history(db: Session, item_id: int, limit: int = 200) -> list[dict]:
    return [_event_dict(e) for e in db.scalars(select(LocationEvent).where(LocationEvent.item_id == item_id)
                                               .order_by(LocationEvent.created_at.desc(), LocationEvent.id.desc()).limit(limit))]


def rack_history(db: Session, rack_id: int, *, offset: int = 0, limit: int = 200) -> dict:
    cond = or_(LocationEvent.from_rack_id == rack_id, LocationEvent.to_rack_id == rack_id)
    total = db.scalar(select(func.count()).select_from(LocationEvent).where(cond)) or 0
    names = dict(db.execute(select(Item.id, Item.name).where(Item.id.in_(
        select(LocationEvent.item_id).where(cond).order_by(LocationEvent.created_at.desc()).offset(offset).limit(limit)))).all())
    rows = []
    for e in db.scalars(select(LocationEvent).where(cond).order_by(LocationEvent.created_at.desc(), LocationEvent.id.desc())
                        .offset(offset).limit(limit)):
        rows.append({**_event_dict(e), "product": names.get(e.item_id, ""),
                     "direction": "in" if e.to_rack_id == rack_id and e.from_rack_id != rack_id else
                     "out" if e.from_rack_id == rack_id and e.to_rack_id != rack_id else "within"})
    audits = [{"at": a.timestamp.isoformat(), "user": a.username, "details": a.details}
              for a in db.scalars(select(AuditLog).where(AuditLog.entity_type == "rack", AuditLog.entity_id == str(rack_id))
                                  .order_by(AuditLog.id.desc()).limit(50))]
    return {"total": total, "rows": rows, "changes": audits}


# --------------------------------------------------------------------------- the past (as of a moment)
def end_of_day(db: Session, day: date) -> datetime:
    """The UTC instant a business day ends (local pharmacy time zone)."""
    tz = ZoneInfo(business_time.timezone_name(db))
    return datetime.combine(day + timedelta(days=1), time.min, tz).astimezone(timezone.utc).replace(tzinfo=None)


def locations_at(db: Session, when: datetime, *, rack_ids: list[int] | None = None, item_ids: list[int] | None = None) -> dict[int, tuple[int, int | None]]:
    """Product → (rack, box) as it was at ``when`` (UTC), from the dated rows."""
    q = select(ItemLocation.item_id, ItemLocation.rack_id, ItemLocation.box_id).where(
        ItemLocation.batch_id.is_(None), ItemLocation.valid_from <= when,
        or_(ItemLocation.valid_to.is_(None), ItemLocation.valid_to > when))
    if rack_ids is not None:
        q = q.where(ItemLocation.rack_id.in_(rack_ids))
    if item_ids is not None:
        q = q.where(ItemLocation.item_id.in_(item_ids))
    return {i: (r, b) for i, r, b in db.execute(q)}


def names_at(db: Session, when: datetime) -> tuple[dict[int, tuple[str, str]], dict[int, tuple[str, str]]]:
    """Rack and box (code, name) as they were at ``when``: today's values, rolled back through the
    audited changes made after it (each change stores the before state)."""
    racks = {r.id: (r.code, r.name) for r in db.scalars(select(Rack))}
    boxes = {b.id: (b.code, b.name) for b in db.scalars(select(RackBox))}
    for entity, table in (("rack", racks), ("rack_box", boxes)):
        seen: set[int] = set()
        for a in db.scalars(select(AuditLog).where(AuditLog.entity_type == entity, AuditLog.timestamp > when,
                                                   AuditLog.action == audit.A_UPDATE).order_by(AuditLog.id)):
            try:
                ident = int(a.entity_id)
            except ValueError:
                continue
            if ident in seen or ident not in table or not isinstance(a.before, dict) or "code" not in a.before:
                continue
            seen.add(ident)                       # the first change after the moment holds the state at that moment
            table[ident] = (a.before.get("code") or table[ident][0], a.before.get("name") or "")
    return racks, boxes


def batch_stock_at(db: Session, when: datetime, item_ids: list[int] | None = None) -> dict[int, int]:
    """Batch → quantity at ``when``: the ledger sum, exactly as the stock reports compute it."""
    q = select(InventoryMovement.batch_id, func.sum(InventoryMovement.quantity)).where(InventoryMovement.created_at < when)
    if item_ids is not None:
        q = q.where(InventoryMovement.item_id.in_(item_ids))
    return {b: int(s or 0) for b, s in db.execute(q.group_by(InventoryMovement.batch_id))}


def inventory_rows(db: Session, when: datetime, *, rack_ids: list[int] | None = None, box_id: int | None = None,
                   assigned: str = "", item_id: int | None = None, category: str = "", batch: str = "",
                   expiry_before: date | None = None, expired_only: bool = False, stock: str = "positive",
                   item_status: str = "") -> list[dict]:
    """Rack inventory at ``when``: one row per batch (or per located product with no stock when
    ``stock`` is zero/all). Unassigned stock is reported as such, so the parts add up to the total."""
    loc = locations_at(db, when)
    racks, boxes = names_at(db, when)
    # a rack / box / "in a rack" report reads only the products that were there: never the whole catalogue
    scope = None
    if rack_ids or box_id or assigned == "assigned":
        scope = [i for i, (r, b) in loc.items() if (not rack_ids or r in rack_ids) and (not box_id or b == box_id)]
    if item_id:
        scope = [i for i in (scope if scope is not None else [item_id]) if i == item_id]
    qty = {}
    if scope is None:
        qty = batch_stock_at(db, when)
    else:
        for start in range(0, len(scope), 5000):
            qty.update(batch_stock_at(db, when, scope[start:start + 5000]))
    q = select(Batch, Item).join(Item, Item.id == Batch.item_id).where(Batch.created_at < when)
    if scope is not None:
        q = q.where(Item.id.in_(scope or [-1]))
    if item_id:
        q = q.where(Item.id == item_id)
    if category:
        q = q.where(Item.category == category)
    rows: list[dict] = []
    tz = ZoneInfo(business_time.timezone_name(db))
    today = (when - timedelta(microseconds=1)).replace(tzinfo=timezone.utc).astimezone(tz).date()

    def keep(item: Item) -> bool:
        where = loc.get(item.id)
        if assigned == "assigned" and not where:
            return False
        if assigned == "unassigned" and where:
            return False
        if rack_ids and (not where or where[0] not in rack_ids):
            return False
        if box_id and (not where or where[1] != box_id):
            return False
        if item_status == "active" and not item.is_active:
            return False
        if item_status == "disabled" and item.is_active:
            return False
        return True

    def base(item: Item) -> dict:
        where = loc.get(item.id)
        r = racks.get(where[0], ("?", "")) if where else ("", "")
        b = boxes.get(where[1], ("", "")) if where and where[1] else ("", "")
        return {"item_id": item.id, "rack": r[0] or "Unassigned", "rack_name": r[1], "box": b[0], "box_name": b[1],
                "item": item.name, "code": item.article_id, "category": item.category, "unit": units.unit_label(item.base_unit, 2),
                "product_status": "Active" if item.is_active else "Disabled", "rack_id": where[0] if where else None}

    for b, item in db.execute(q.order_by(Item.name, Batch.expiry_date)):
        if not keep(item):
            continue
        if batch and batch.upper() not in (b.batch_no or "").upper():
            continue
        quantity = qty.get(b.id, 0)
        if quantity <= 0 or stock == "zero":          # an empty batch is not a line; a product with nothing is (below)
            continue
        expired = units.is_expired(b.expiry_date, today)
        if expired_only and not expired:
            continue
        if expiry_before and (not b.expiry_date or b.expiry_date > expiry_before):
            continue
        rows.append({**base(item), "batch": b.batch_no, "expiry": b.expiry_date.isoformat() if b.expiry_date else "",
                     "quantity": quantity, "status": "Expired" if expired else "Sellable",
                     "mrp_value": round(float(b.unit_mrp or 0) * quantity, 2)})
    if stock in ("zero", "all") and not batch and not expired_only and not expiry_before:
        # located (or filtered) products holding nothing at that moment: shelf labels with no stock behind them
        per_item = {i: int(t or 0) for i, t in db.execute(
            select(InventoryMovement.item_id, func.sum(InventoryMovement.quantity))
            .where(InventoryMovement.created_at < when).group_by(InventoryMovement.item_id))}
        zq = select(Item).where(Item.created_at < when, or_(Item.deleted_at.is_(None), Item.deleted_at > when))
        if item_id:
            zq = zq.where(Item.id == item_id)
        if category:
            zq = zq.where(Item.category == category)
        for item in db.scalars(zq.order_by(Item.name)):
            if per_item.get(item.id, 0) > 0 or not keep(item):
                continue
            rows.append({**base(item), "batch": "", "expiry": "", "quantity": 0, "status": "Out of stock", "mrp_value": 0})
    return rows


def timeline(db: Session, rack_id: int, *, days: int = 30, end: date | None = None, box_id: int | None = None) -> list[dict]:
    """Rack contents at the close of each day (newest first), rebuilt from the dated rows and the ledger."""
    days = max(1, min(int(days or 30), 92))
    tz = ZoneInfo(business_time.timezone_name(db))
    last = end or datetime.now(tz).date()
    out = []
    for n in range(days):
        day = last - timedelta(days=n)
        when = end_of_day(db, day)
        q = select(ItemLocation.item_id).where(ItemLocation.batch_id.is_(None), ItemLocation.rack_id == rack_id,
                                               ItemLocation.valid_from < when,
                                               or_(ItemLocation.valid_to.is_(None), ItemLocation.valid_to >= when))
        if box_id:
            q = q.where(ItemLocation.box_id == box_id)
        ids = list(db.scalars(q))
        units_ = batches = 0
        if ids:
            for _, s in db.execute(select(InventoryMovement.batch_id, func.sum(InventoryMovement.quantity))
                                   .where(InventoryMovement.item_id.in_(ids), InventoryMovement.created_at < when)
                                   .group_by(InventoryMovement.batch_id)):
                if (s or 0) > 0:
                    units_ += int(s)
                    batches += 1
        out.append({"date": day.isoformat(), "as_of": when.isoformat(), "products": len(ids), "batches": batches, "units": units_})
    return out


# --------------------------------------------------------------------------- import support
def _fold(code: str) -> str:
    """Look-alike folding for suggestions only (O→0, I/L→1, S→5, no separators)."""
    return re.sub(r"[^A-Z0-9]", "", normalize_code(code)).translate(str.maketrans("OILS", "0115"))


def resolve_code(db: Session, rack_code: str, box_code: str = "") -> tuple[Rack | None, RackBox | None, str, str]:
    """An imported rack (and box) code → (rack, box, problem, suggestion). Exact matches only;
    an unknown code is never created, it comes back with the closest existing code."""
    code = normalize_code(rack_code)
    if not code:
        return None, None, "", ""
    rack = db.scalar(select(Rack).where(Rack.code == code))
    if rack is None:
        racks = list(db.scalars(select(Rack).where(Rack.is_active.is_(True))))
        folded = {_fold(r.code): r.code for r in racks}
        hit = folded.get(_fold(code)) or next(iter(difflib.get_close_matches(code, [r.code for r in racks], n=1, cutoff=0.6)), "")
        return None, None, f"Unknown rack {code}", hit
    if not rack.is_active:
        return None, None, f"Rack {code} is disabled", ""
    box_code = normalize_code(box_code)
    if not box_code:
        return rack, None, "", ""
    box = db.scalar(select(RackBox).where(RackBox.rack_id == rack.id, RackBox.code == box_code))
    if box is None or not box.is_active:
        codes = [b.code for b in rack.boxes if b.is_active]
        hit = next(iter(difflib.get_close_matches(box_code, codes, n=1, cutoff=0.5)), "")
        return rack, None, f"Rack {code} has no box {box_code}", f"{code} / {hit}" if hit else ""
    return rack, box, "", ""
