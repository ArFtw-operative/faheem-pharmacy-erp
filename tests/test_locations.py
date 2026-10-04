"""Rack / box locations: masters, assignment, history, as-of reconstruction, integrity, concurrency."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.models import AuditLog, Batch, ItemLocation, LocationEvent, Rack, RackBox
from app.services import inventory_service as inv
from app.services import location_service as loc
from app.services import sales_service, stock_ledger
from app.utils import utcnow


def product(db, name="DOLO 650 TAB", stock=0, category="PHARMA"):
    item = inv.create_item(db, name=name, pack_size="15S", base_unit="TABLET", pack_unit="STRIP", units_per_pack=15,
                           dosage_form="TABLET", category=category)
    if stock:
        inv.add_or_update_batch(db, item, batch_no="B" + name[:3], quantity=stock, unit="BASE", mrp=Decimal("30"),
                                expiry_date=date(2028, 5, 1))
    db.commit()
    return item


def boxes_on(db):
    loc.set_config(db, {"location_boxes_enabled": True})


def rack(db, code="R-A01", name="Antibiotics"):
    r = loc.create_rack(db, code=code, name=name)
    db.commit()
    return r


def open_rows(db, item_id):
    return list(db.scalars(select(ItemLocation).where(ItemLocation.item_id == item_id, ItemLocation.valid_to.is_(None))))


# ------------------------------------------------------------------ masters
def test_rack_codes_are_unique_and_normalised(db):
    r = rack(db, code=" r-a01 ")
    assert r.code == "R-A01"
    with pytest.raises(loc.LocationError, match="already exists"):
        loc.create_rack(db, code="R-A01", name="Other")
    with pytest.raises(loc.LocationError, match="use letters"):
        loc.create_rack(db, code="A/B")
    loc.create_rack(db, code="R-A02", name="Antibiotics")          # names need not be unique
    db.commit()


def test_rename_keeps_relationships_and_history_meaning(db):
    r = rack(db, name="General Medicines")
    item = product(db)
    loc.assign(db, [item.id], r.id)
    db.commit()
    before_rename = utcnow()
    loc.update_rack(db, r, name="General Tablets")
    db.commit()
    assert loc.current(db, [item.id])[item.id].rack_name == "General Tablets"      # by id, so the product follows
    event = db.scalar(select(LocationEvent))
    assert "General Medicines" in event.to_label                                   # history keeps the name of the time
    racks, _ = loc.names_at(db, before_rename)
    assert racks[r.id] == ("R-A01", "General Medicines")
    assert db.scalar(select(func.count()).select_from(AuditLog).where(AuditLog.entity_type == "rack")) == 2


def test_boxes_are_unique_per_rack_and_need_the_feature(db):
    a, b = rack(db), rack(db, code="R-B01", name="Syrups")
    with pytest.raises(loc.LocationError, match="turned off"):
        loc.create_box(db, a, code="B01")
    boxes_on(db)
    loc.create_box(db, a, code="B01", name="Upper")
    loc.create_box(db, b, code="B01")                                   # same code, another rack: fine
    with pytest.raises(loc.LocationError, match="already has box"):
        loc.create_box(db, a, code="b01")
    db.commit()


def test_a_box_of_another_rack_is_refused_by_service_and_database(db):
    boxes_on(db)
    a, b = rack(db), rack(db, code="R-B01")
    bx = loc.create_box(db, b, code="B01")
    item = product(db)
    db.commit()
    with pytest.raises(loc.LocationError, match="belongs to rack R-B01"):
        loc.assign(db, [item.id], a.id, bx.id)
    db.rollback()
    db.add(ItemLocation(item_id=item.id, rack_id=a.id, box_id=bx.id, valid_from=utcnow()))   # a manipulated write
    with pytest.raises(IntegrityError):
        db.flush()
    db.rollback()


def test_disabled_rack_or_box_and_unknown_ids_are_refused(db):
    boxes_on(db)
    r = rack(db)
    bx = loc.create_box(db, r, code="B01")
    item = product(db)
    loc.set_box_status(db, bx, False)
    with pytest.raises(loc.LocationError, match="disabled"):
        loc.assign(db, [item.id], r.id, bx.id)
    loc.set_rack_status(db, r, False)
    with pytest.raises(loc.LocationError, match="disabled"):
        loc.assign(db, [item.id], r.id)
    with pytest.raises(loc.LocationError, match="not found"):
        loc.assign(db, [item.id], 99999)


def test_box_required_setting(db):
    boxes_on(db)
    loc.set_config(db, {"location_require_box": True})
    r = rack(db)
    item = product(db)
    with pytest.raises(loc.LocationError, match="box is required"):
        loc.assign(db, [item.id], r.id)


# ------------------------------------------------------------------ assignment and history
def test_assign_move_box_change_and_unassign_make_history(db):
    boxes_on(db)
    a, c = rack(db), rack(db, code="R-C01", name="Fever")
    b1 = loc.create_box(db, a, code="B01")
    b4 = loc.create_box(db, c, code="B04")
    b5 = loc.create_box(db, c, code="B05")
    item = product(db, stock=30)
    for target in ((a.id, b1.id), (c.id, b4.id), (c.id, b5.id), (None, None)):
        out = loc.assign(db, [item.id], *target, reason="Stock rearrangement")
        assert out["processed"] == 1
        db.commit()
    kinds = [e["type"] for e in loc.item_history(db, item.id)]
    assert kinds == ["UNASSIGNED", "BOX_CHANGED", "MOVED", "ASSIGNED"]
    moved = loc.item_history(db, item.id)[2]
    assert moved["from"].startswith("R-A01") and moved["to"].startswith("R-C01") and moved["stock"] == 30
    assert not open_rows(db, item.id)
    assert db.scalar(select(func.count()).select_from(ItemLocation).where(ItemLocation.item_id == item.id)) == 3


def test_bulk_move_reports_requested_processed_skipped_failed(db):
    r = rack(db)
    items = [product(db, name=f"MED {k} TAB") for k in range(5)]
    loc.assign(db, [items[0].id], r.id)
    inv.delete_item(db, items[1])
    db.commit()
    out = loc.assign(db, [i.id for i in items] + [987654], r.id)
    assert out["requested"] == 6 and out["processed"] == 3
    assert [s["id"] for s in out["skipped"]] == [items[0].id]                  # already there: no write
    assert {f["id"] for f in out["failed"]} == {items[1].id, 987654}
    ops = {e.operation_id for e in db.scalars(select(LocationEvent).where(LocationEvent.item_id.in_([i.id for i in items[2:]])))}
    assert ops == {out["operation_id"]}                                         # one operation, one correlation id
    assert db.scalar(select(AuditLog).where(AuditLog.entity_type == "location_operation", AuditLog.entity_id == out["operation_id"]))


def test_atomic_operation_rolls_back_entirely(db):
    r = rack(db)
    good, gone = product(db, name="GOOD TAB"), product(db, name="GONE TAB")
    inv.delete_item(db, gone)
    db.commit()
    with pytest.raises(loc.LocationError):
        loc.assign(db, [good.id, gone.id], r.id, atomic=True)
    db.rollback()
    assert not open_rows(db, good.id) and not db.scalar(select(func.count()).select_from(LocationEvent))


def test_stale_screen_is_detected(db):
    a, b = rack(db), rack(db, code="R-B01")
    item = product(db)
    loc.assign(db, [item.id], a.id)
    db.commit()
    seen = {item.id: {"rack_id": a.id}}
    loc.assign(db, [item.id], b.id)                     # another user moves it
    db.commit()
    out = loc.assign(db, [item.id], a.id, expected=seen)   # the first user acts on what they saw
    assert out["processed"] == 0 and "another user" in out["failed"][0]["reason"]
    assert loc.current(db, [item.id])[item.id].rack_id == b.id


def test_one_open_location_per_product_is_enforced(db):
    a, b = rack(db), rack(db, code="R-B01")
    item = product(db)
    loc.assign(db, [item.id], a.id)
    db.commit()
    db.add(ItemLocation(item_id=item.id, rack_id=b.id, valid_from=utcnow()))      # a racing second writer
    with pytest.raises(IntegrityError):
        db.flush()
    db.rollback()


def test_selling_to_zero_keeps_the_location(db):
    r = rack(db)
    item = product(db, stock=15)
    loc.assign(db, [item.id], r.id)
    db.commit()
    sales_service.create_sale(db, lines=[{"item_id": item.id, "quantity": 15}])
    db.commit()
    assert inv.stock_on_hand(db, item.id) == 0
    assert loc.current(db, [item.id])[item.id].rack_code == "R-A01"
    inv.deactivate_item(db, item)
    inv.reactivate_item(db, item)
    db.commit()
    assert loc.current(db, [item.id])[item.id].rack_code == "R-A01"


def test_moving_never_changes_stock(db):
    a, b = rack(db), rack(db, code="R-B01")
    item = product(db, stock=100)
    movements = db.scalar(select(func.count()).select_from(stock_ledger.InventoryMovement))
    loc.assign(db, [item.id], a.id)
    loc.assign(db, [item.id], b.id)
    db.commit()
    assert inv.stock_on_hand(db, item.id) == 100
    assert db.scalar(select(func.count()).select_from(stock_ledger.InventoryMovement)) == movements
    assert not stock_ledger.reconcile(db)


# ------------------------------------------------------------------ disabling with contents
def test_rack_with_products_cannot_be_disabled_until_moved(db):
    a, b = rack(db), rack(db, code="R-B01")
    item = product(db, stock=20)
    loc.assign(db, [item.id], a.id)
    db.commit()
    with pytest.raises(loc.NotEmpty) as exc:
        loc.set_rack_status(db, a, False)
    assert exc.value.products == 1 and exc.value.units == 20
    out = loc.set_rack_status(db, a, False, move_to_rack=b.id)
    db.commit()
    assert out["moved"]["processed"] == 1 and not a.is_active
    assert loc.current(db, [item.id])[item.id].rack_id == b.id


def test_box_disable_moves_or_keeps_the_rack(db):
    boxes_on(db)
    r = rack(db)
    b1, b2 = loc.create_box(db, r, code="B01"), loc.create_box(db, r, code="B02")
    x, y = product(db, name="X TAB"), product(db, name="Y TAB")
    loc.assign(db, [x.id, y.id], r.id, b1.id)
    db.commit()
    with pytest.raises(loc.NotEmpty):
        loc.set_box_status(db, b1, False)
    loc.set_box_status(db, b1, False, then="clear")
    db.commit()
    here = loc.current(db, [x.id, y.id])
    assert here[x.id].rack_id == r.id and here[x.id].box_id is None
    loc.set_box_status(db, b1, True)
    loc.assign(db, [x.id], r.id, b2.id)
    loc.set_box_status(db, b2, False, then="move", move_to_box=b1.id)
    db.commit()
    assert loc.current(db, [x.id])[x.id].box_id == b1.id


# ------------------------------------------------------------------ the past and reconciliation
def test_rack_inventory_as_of_a_past_moment_uses_history(db):
    a, b = rack(db), rack(db, code="R-B01")
    item = product(db, stock=40)
    loc.assign(db, [item.id], a.id)
    db.commit()
    t1 = utcnow()
    batch = db.scalar(select(Batch))
    stock_ledger.post(db, batch, "SALE", 10, reason="sold")
    loc.assign(db, [item.id], b.id)
    db.commit()
    then = loc.inventory_rows(db, t1, rack_ids=[a.id])
    assert [(r["rack"], r["quantity"]) for r in then] == [("R-A01", 40)]
    assert loc.inventory_rows(db, t1, rack_ids=[b.id]) == []
    now_rows = loc.inventory_rows(db, utcnow() + timedelta(seconds=1), rack_ids=[b.id])
    assert [(r["rack"], r["quantity"]) for r in now_rows] == [("R-B01", 30)]


def test_racks_plus_unassigned_equals_total_stock(db):
    a, b = rack(db), rack(db, code="R-B01")
    items = [product(db, name=f"P{k} TAB", stock=10 * (k + 1)) for k in range(5)]
    loc.assign(db, [items[0].id, items[1].id], a.id)
    loc.assign(db, [items[2].id], b.id)
    db.commit()
    when = utcnow() + timedelta(seconds=1)
    rows = loc.inventory_rows(db, when)
    total = int(db.scalar(select(func.sum(Batch.quantity))))
    by = {}
    for r in rows:
        by[r["rack"]] = by.get(r["rack"], 0) + r["quantity"]
    assert by == {"R-A01": 30, "R-B01": 30, "Unassigned": 90}
    assert sum(by.values()) == total == 150


def test_timeline_counts_each_day_from_history(db):
    r = rack(db)
    item = product(db, stock=12)
    loc.assign(db, [item.id], r.id)
    db.commit()
    days = loc.timeline(db, r.id, days=3)
    assert days[0]["products"] == 1 and days[0]["units"] == 12
    assert days[2]["products"] == 0                                 # two days ago it was not there


# ------------------------------------------------------------------ suggestions
def test_suggestions_never_assign_and_say_why(db):
    r = rack(db)
    loc.set_category_defaults(db, r, ["FMCG"])
    soap = product(db, name="SOAP BAR", category="FMCG")
    pill = product(db, name="PILL TAB")
    db.commit()
    s = loc.suggest(db, [soap, pill])
    assert s[soap.id]["source"] == "CATEGORY" and s[soap.id]["rack"] == "R-A01" and pill.id not in s
    assert not open_rows(db, soap.id)                               # a suggestion is not an assignment
    loc.assign(db, [pill.id], r.id)
    loc.unassign(db, [pill.id])
    db.commit()
    assert loc.suggest(db, [pill])[pill.id]["source"] == "LAST"


def test_import_codes_resolve_exactly_and_suggest_lookalikes(db):
    rack(db, code="R-A01")
    r, b, problem, hint = loc.resolve_code(db, "R-AO1")
    assert r is None and problem == "Unknown rack R-AO1" and hint == "R-A01"
    r, b, problem, _ = loc.resolve_code(db, "r-a01")
    assert r.code == "R-A01" and not problem
    assert db.scalar(select(func.count()).select_from(Rack)) == 1    # nothing created


def test_find_by_code(db):
    boxes_on(db)
    r = rack(db, code="R-A03")
    bx = loc.create_box(db, r, code="B02")
    db.commit()
    assert loc.find_by_code(db, "r-a03") == (r, None)
    assert loc.find_by_code(db, "R-A03/B02") == (r, bx)
    assert loc.find_by_code(db, "R-A03 B02") == (r, bx)
    assert loc.find_by_code(db, "B02") == (r, bx)                 # unique box code
    assert loc.find_by_code(db, "dolo") == (None, None)


def test_snapshot_lists_an_emptied_product_once(db):
    r = rack(db)
    item = product(db, stock=15)
    loc.assign(db, [item.id], r.id)
    db.commit()
    sales_service.create_sale(db, lines=[{"item_id": item.id, "quantity": 15}])
    db.commit()
    rows = loc.inventory_rows(db, utcnow() + timedelta(seconds=1), rack_ids=[r.id], stock="all")
    assert [(x["item"], x["status"], x["quantity"]) for x in rows] == [("DOLO 650 TAB", "Out of stock", 0)]


def test_a_few_early_placements_do_not_become_a_category_habit(db):
    r = rack(db)
    items = [product(db, name=f"EARLY {k} TAB") for k in range(10)]
    loc.assign(db, [i.id for i in items[:3]], r.id)          # 3 of 10 placed: not a habit yet
    db.commit()
    assert loc.category_suggestions(db, ["PHARMA"]) == {}
    loc.assign(db, [i.id for i in items[3:6]], r.id)         # 6 of 10, all in one rack: a habit
    db.commit()
    assert loc.category_suggestions(db, ["PHARMA"])["PHARMA"]["source"] == "USUAL"
