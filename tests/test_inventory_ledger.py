"""Inventory engine: base units, packaging, batches, FEFO, ledger, reversals.

Covers the ERP acceptance scenarios: loose tablets from strips, full-strip
sales, multi-batch FEFO with per-batch MRP, free goods, returns, cancellations,
conflicts, concurrency and ledger/projection reconciliation.
"""
from __future__ import annotations

import threading
from datetime import date
from decimal import Decimal

import pytest

from app.database import SessionLocal
from app.models import Batch, InventoryMovement, Item, Sale
from app.services import inventory_service as inv
from app.services import purchase_service, refund_service, sales_service, stock_ledger, units

FUTURE = date(2099, 2, 1)


def make_dolo(db, upp=15, loose=True, name="DOLO 650MG TAB"):
    return inv.create_item(db, name=name, pack_size=f"{upp}S", base_unit="TABLET", pack_unit="STRIP",
                           units_per_pack=upp, loose_sale=loose, dosage_form="TABLET")


def receive(db, item, packs, batch="DOBS4401", expiry=FUTURE, mrp="32.10", free=0, rate="24.00"):
    return inv.add_or_update_batch(
        db, item, batch_no=batch, expiry_date=expiry, quantity=packs, free=free, unit="PACK",
        movement_type="PURCHASE_RECEIPT", mrp=mrp, purchase_rate=rate, reference_type="PURCHASE",
    )


def sell(db, item, qty, **line):
    sale = sales_service.create_sale(db, lines=[{"item_id": item.id, "quantity": qty, **line}], payment_mode="UPI")
    db.commit()
    return sale


def stock(db, item) -> int:
    db.expire_all()
    return db.get(Item, item.id).total_stock


def assert_reconciled(db):
    assert stock_ledger.reconcile(db) == []


# --------------------------------------------------------------------------- units
@pytest.mark.parametrize("raw,kind,upp,outer,confident", [
    ("10", "COUNT", 10, None, True),
    ("10S", "COUNT", 10, None, True),
    ("15'S", "COUNT", 15, None, True),
    ("20 S", "COUNT", 20, None, True),
    ("1", "SINGLE", 1, None, True),
    ("1X3", "COUNT", 3, 1, True),
    ("2X10S", "NESTED", 10, 2, False),
    ("3X6S", "NESTED", 6, 3, False),
    ("20X10", "NESTED", 10, 20, False),
    ("5X1X1", "NESTED", 1, 5, False),
    ("KIT", "KIT", 1, None, True),
])
def test_pack_parser_shapes(raw, kind, upp, outer, confident):
    info = units.parse_pack(raw)
    assert (info.kind, info.units_per_pack, info.outer_count, info.confident) == (kind, upp, outer, confident)
    assert info.raw == raw  # the raw text is never lost


@pytest.mark.parametrize("raw,qty,unit", [("5ML", "5", "ML"), ("200ML", "200", "ML"), ("30GM", "30", "G")])
def test_pack_parser_content_is_metadata_not_stock(raw, qty, unit):
    info = units.parse_pack(raw)
    assert info.kind == "CONTENT" and info.units_per_pack == 1
    assert (info.content_qty, info.content_unit) == (Decimal(qty), unit)


def test_nested_content_pack_needs_review():
    info = units.parse_pack("5X5ML")
    assert info.outer_count == 5 and not info.confident


def test_conversion_display_and_shorthand():
    assert units.to_base(5, 15, "PACK") == 75
    assert units.to_base(3, 15, "BASE") == 3
    assert units.describe_stock(666, 15, "TABLET", "STRIP") == "44 strips + 6 tablets"
    assert units.describe_stock(48, 1, "BOTTLE", "BOTTLE") == "48 bottles"
    assert units.parse_qty_expression("3", 15) == 3
    assert units.parse_qty_expression("1s", 15) == 15
    assert units.parse_qty_expression("2s+3", 15) == 33
    with pytest.raises(units.UnitError):
        units.parse_qty_expression("2.5", 15)


def test_pricing_full_strip_equals_printed_mrp():
    assert units.display_unit_price("32.10", 15) == Decimal("2.14")
    assert units.line_amount("32.10", 15, 3) == Decimal("6.42")
    assert units.line_amount("32.10", 15, 15) == Decimal("32.10")
    assert units.line_amount("10.00", 3, 3) == Decimal("10.00")  # no per-unit rounding drift


def test_expiry_is_month_level():
    assert not units.is_expired(date(2026, 9, 1), date(2026, 9, 29))  # sellable through September
    assert units.is_expired(date(2026, 8, 1), date(2026, 9, 1))


# --------------------------------------------------------------------------- acceptance: Dolo
def test_dolo_acceptance_loose_and_full_strips(db):
    """Spec §57: 1 strip of 20 → sell 15 → receive 2 strips → sell 2."""
    dolo = make_dolo(db, upp=20)
    receive(db, dolo, 1, mrp="40.00")
    db.commit()
    assert stock(db, dolo) == 20

    sell(db, dolo, 15)
    assert stock(db, dolo) == 5

    receive(db, dolo, 2, mrp="40.00")  # same batch
    db.commit()
    assert stock(db, dolo) == 45
    assert inv.describe_stock(dolo, 45) == "2 strips + 5 tablets"
    assert db.query(Batch).filter_by(item_id=dolo.id).count() == 1

    sale = sell(db, dolo, 2)
    assert stock(db, dolo) == 43
    assert inv.describe_stock(dolo, 43) == "2 strips + 3 tablets"
    assert sale.items[0].quantity == 2 and sale.items[0].line_total == Decimal("4.00")
    assert_reconciled(db)


def test_qty_one_is_one_tablet_never_a_strip(db):
    dolo = make_dolo(db)
    receive(db, dolo, 1)
    db.commit()
    sale = sell(db, dolo, 1)
    assert stock(db, dolo) == 14
    line = sale.items[0]
    assert (line.mrp, line.line_total, line.pack_mrp, line.units_per_pack) == (
        Decimal("2.14"), Decimal("2.14"), Decimal("32.10"), 15)
    sell(db, dolo, 14)
    assert stock(db, dolo) == 0


def test_strip_shorthand_on_the_server(db):
    dolo = make_dolo(db)
    receive(db, dolo, 3)
    db.commit()
    sale = sell(db, dolo, "2s+3")
    assert sale.items[0].quantity == 33 and stock(db, dolo) == 12


def test_non_loose_product_sells_whole_units(db):
    syrup = inv.create_item(db, name="CALCIJOINT SYRUP 200ML", pack_size="200ML", base_unit="BOTTLE",
                            pack_unit="BOTTLE", units_per_pack=1, loose_sale=False)
    receive(db, syrup, 10, batch="AYLH26003", mrp="145.00")
    db.commit()
    sale = sell(db, syrup, 1)
    assert sale.items[0].line_total == Decimal("145.00") and stock(db, syrup) == 9


def test_multi_unit_pack_sells_single_units(db):
    """A box of 5 pens: 2 boxes inward = +10 pens; one pen sells at box MRP ÷ 5."""
    pens = inv.create_item(db, name="INSULIN PEN 5S", base_unit="PIECE", pack_unit="BOX", units_per_pack=5)
    assert pens.loose_sale is True  # derived from the conversion, never switched by hand
    receive(db, pens, 2, batch="P1", mrp="500")
    db.commit()
    assert stock(db, pens) == 10
    assert sell(db, pens, 3).items[0].line_total == Decimal("300.00")


# --------------------------------------------------------------------------- batches
def test_new_batch_goes_under_the_same_product(db):
    dolo = make_dolo(db)
    receive(db, dolo, 1, batch="A1")
    receive(db, dolo, 1, batch="B2", mrp="32.55")
    db.commit()
    assert db.query(Item).count() == 1
    assert sorted(b.batch_no for b in db.query(Batch).all()) == ["A1", "B2"]
    assert stock(db, dolo) == 30


def test_batch_text_is_kept_exactly_and_matched_normalised(db):
    dolo = make_dolo(db)
    b1 = receive(db, dolo, 1, batch="211000000")
    b2 = receive(db, dolo, 1, batch="211 000 000")
    db.commit()
    assert b1.id == b2.id and b1.batch_no == "211000000" and b1.quantity == 30


def test_same_batch_text_on_different_products_stays_separate(db):
    a, b = make_dolo(db, name="A TAB"), make_dolo(db, name="B TAB")
    ba, bb = receive(db, a, 1, batch="X1"), receive(db, b, 1, batch="X1")
    db.commit()
    assert ba.id != bb.id and stock(db, a) == 15 and stock(db, b) == 15


def test_conflicting_expiry_is_not_merged(db):
    dolo = make_dolo(db)
    receive(db, dolo, 1, batch="C1", expiry=date(2099, 2, 1))
    db.commit()
    with pytest.raises(stock_ledger.BatchConflict, match="expiry"):
        receive(db, dolo, 1, batch="C1", expiry=date(2099, 5, 1))
    db.rollback()
    assert stock(db, dolo) == 15


def test_conflicting_mrp_is_not_overwritten(db):
    dolo = make_dolo(db)
    receive(db, dolo, 1, batch="M1", mrp="32.10")
    db.commit()
    with pytest.raises(stock_ledger.BatchConflict, match="MRP"):
        receive(db, dolo, 1, batch="M1", mrp="33.00")
    db.rollback()
    assert db.query(Batch).one().mrp == Decimal("32.10")


# --------------------------------------------------------------------------- FEFO
def test_fefo_splits_across_batches_with_their_own_mrp(db):
    """Spec §58: 5 in A (unit ₹2.00) + 40 in B (unit ₹2.10); sell 8 → 5 from A, 3 from B."""
    dolo = make_dolo(db, upp=10)
    a = receive(db, dolo, 1, batch="A", expiry=date(2099, 1, 1), mrp="20.00")
    b = receive(db, dolo, 4, batch="B", expiry=date(2099, 5, 1), mrp="21.00")
    db.commit()
    inv.record_adjustment(db, batch=a, quantity=5, category="COUNT", reason="setup")
    db.commit()
    sale = sell(db, dolo, 8)
    rows = sorted(sale.items, key=lambda r: r.expiry_date)
    assert [(r.batch_no, r.quantity, r.line_total) for r in rows] == [
        ("A", 5, Decimal("10.00")), ("B", 3, Decimal("6.30"))]
    assert {r.line_no for r in rows} == {1}
    assert sale.subtotal == Decimal("16.30")
    db.expire_all()
    assert (db.get(Batch, a.id).quantity, db.get(Batch, b.id).quantity) == (0, 37)
    assert_reconciled(db)


def test_expired_batch_is_never_sold(db):
    dolo = make_dolo(db)
    old = receive(db, dolo, 1, batch="OLD", expiry=date(2020, 1, 1))
    receive(db, dolo, 1, batch="NEW", expiry=FUTURE)
    db.commit()
    sale = sell(db, dolo, 3)
    assert sale.items[0].batch_no == "NEW"
    with pytest.raises(sales_service.SaleError, match="expired"):
        sell(db, dolo, 1, batch_id=old.id)
    db.rollback()
    with pytest.raises(sales_service.SaleError, match="non-expired"):
        sell(db, dolo, 13)  # 12 sellable, 15 expired
    db.rollback()


def test_manual_batch_override_is_audited(db):
    from app.models import AuditLog

    dolo = make_dolo(db)
    receive(db, dolo, 1, batch="FIRST", expiry=date(2099, 1, 1))
    later = receive(db, dolo, 1, batch="LATER", expiry=date(2099, 9, 1))
    db.commit()
    sale = sell(db, dolo, 2, batch_id=later.id)
    assert sale.items[0].batch_no == "LATER"
    assert db.query(AuditLog).filter(AuditLog.details.like("Manual batch override%")).count() == 1


def test_client_rate_is_ignored(db):
    dolo = make_dolo(db)
    receive(db, dolo, 1)
    db.commit()
    sale = sell(db, dolo, 3, rate="0.01")
    assert sale.subtotal == Decimal("6.42")


def test_batch_without_mrp_blocks_the_sale(db):
    dolo = make_dolo(db)
    receive(db, dolo, 1, mrp="0")
    db.commit()
    with pytest.raises(sales_service.SaleError, match="no MRP"):
        sell(db, dolo, 1)
    db.rollback()
    assert stock(db, dolo) == 15


# --------------------------------------------------------------------------- purchases, returns, reversals
def test_free_goods_increase_physical_stock(db):
    dolo = make_dolo(db, upp=10)
    receive(db, dolo, 8, free=2, mrp="20.00")
    db.commit()
    assert stock(db, dolo) == 100
    kinds = {m.movement_type: m.quantity for m in db.query(InventoryMovement).all()}
    assert kinds == {"PURCHASE_RECEIPT": 80, "FREE_STOCK": 20}


def test_purchase_movement_keeps_document_quantity(db):
    dolo = make_dolo(db)
    receive(db, dolo, 5)
    db.commit()
    m = db.query(InventoryMovement).one()
    assert (m.txn_quantity, m.txn_unit, m.units_per_pack, m.quantity) == (5, "PACK", 15, 75)


def test_purchase_return_decreases_the_right_batch(db):
    dolo = make_dolo(db)
    a = receive(db, dolo, 2, batch="A")
    b = receive(db, dolo, 2, batch="B", mrp="32.55")
    db.commit()
    purchase_service.create_return(db, item_id=dolo.id, batch_id=b.id, quantity=15, value="24", reason="damaged")
    db.commit()
    db.expire_all()
    assert (db.get(Batch, a.id).quantity, db.get(Batch, b.id).quantity) == (30, 15)
    assert db.query(InventoryMovement).filter_by(movement_type="PURCHASE_RETURN").one().quantity == -15


def test_sale_return_restores_the_sold_batch_and_links_it(db):
    dolo = make_dolo(db)
    batch = receive(db, dolo, 1)
    db.commit()
    sale = sell(db, dolo, 5)
    refund_service.create_return(db, sale, lines=[{"sale_item_id": sale.items[0].id, "quantity": 2}],
                                 refund_method="UPI", reason_code="OTHER", reason_note="unused")
    db.commit()
    assert db.get(Batch, batch.id).quantity == 12
    ret = db.query(InventoryMovement).filter_by(movement_type="SALE_RETURN").one()
    orig = db.query(InventoryMovement).filter_by(movement_type="SALE").one()
    assert ret.reversal_of_id == orig.id and ret.quantity == 2
    assert_reconciled(db)


def test_cancelled_bill_creates_reversals_not_deletions(db):
    dolo = make_dolo(db, upp=10)
    receive(db, dolo, 1, batch="A", expiry=date(2099, 1, 1), mrp="20")
    receive(db, dolo, 1, batch="B", expiry=date(2099, 3, 1), mrp="20")
    db.commit()
    sale = sell(db, dolo, 14)
    sales_service.void_sale(db, sale, reason="wrong item")
    db.commit()
    assert stock(db, dolo) == 20
    sales = db.query(InventoryMovement).filter_by(movement_type="SALE").all()
    cancels = db.query(InventoryMovement).filter_by(movement_type="SALE_CANCEL").all()
    assert len(sales) == 2 and len(cancels) == 2
    assert {c.reversal_of_id for c in cancels} == {s.id for s in sales}
    assert_reconciled(db)


def test_adjustments_are_ledgered_with_reasons(db):
    dolo = make_dolo(db)
    batch = receive(db, dolo, 1)
    db.commit()
    inv.set_batch_quantity(db, batch, 12, reason="Physical count difference")
    inv.set_batch_quantity(db, batch, 13, reason="Found one")
    db.commit()
    rows = db.query(InventoryMovement).order_by(InventoryMovement.id).all()[1:]
    assert [(r.movement_type, r.quantity, r.balance_after) for r in rows] == [
        ("ADJUSTMENT_OUT", -3, 12), ("ADJUSTMENT_IN", 1, 13)]
    assert "Physical count difference" in rows[0].reason
    assert_reconciled(db)


def test_stock_never_goes_negative(db):
    dolo = make_dolo(db)
    batch = receive(db, dolo, 1)
    db.commit()
    with pytest.raises(inv.InventoryError, match="Insufficient stock"):
        inv.decrement_stock(db, batch, 16)
    db.rollback()
    assert stock(db, dolo) == 15


def test_idempotent_sale_posts_stock_once(db):
    dolo = make_dolo(db)
    receive(db, dolo, 1)
    db.commit()
    for _ in range(2):
        sales_service.create_sale(db, lines=[{"item_id": dolo.id, "quantity": 4}], client_request_id="req-1")
        db.commit()
    assert stock(db, dolo) == 11 and db.query(Sale).count() == 1


def test_packaging_cannot_change_while_stock_exists(db):
    dolo = make_dolo(db)
    receive(db, dolo, 1)
    db.commit()
    with pytest.raises(inv.InventoryError, match="Bring its stock to zero"):
        inv.update_item(db, dolo, units_per_pack=10)
    db.rollback()
    inv.update_item(db, dolo, loose_sale="no")  # the loose flag itself does not recount stock
    db.commit()
    assert db.get(Item, dolo.id).units_per_pack == 15


def test_two_counters_selling_the_last_tablet(db):
    """Only one of two simultaneous sales of the final unit may succeed."""
    dolo = make_dolo(db)
    batch = receive(db, dolo, 1)
    db.commit()
    inv.decrement_stock(db, batch, 14, reason="setup")
    db.commit()
    item_id = dolo.id  # never touch the test's session from the worker threads
    barrier = threading.Barrier(2)
    results: list[str] = []
    errors: list[str] = []

    def counter(tag):
        session = SessionLocal()
        try:
            barrier.wait()
            sales_service.create_sale(session, lines=[{"item_id": item_id, "quantity": 1}], client_request_id=tag)
            session.commit()
            results.append("ok")
        except Exception as exc:  # noqa: BLE001
            session.rollback()
            results.append("fail")
            errors.append(repr(exc))
        finally:
            session.close()

    threads = [threading.Thread(target=counter, args=(t,)) for t in ("c1", "c2")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == ["fail", "ok"], errors
    assert stock(db, dolo) == 0
    assert_reconciled(db)


def test_projection_can_be_rebuilt_from_the_ledger(db):
    dolo = make_dolo(db)
    batch = receive(db, dolo, 2)
    sell(db, dolo, 4)
    db.execute(Batch.__table__.update().where(Batch.id == batch.id).values(quantity=999))
    db.commit()
    assert stock_ledger.reconcile(db) == [
        {"batch_id": batch.id, "item_id": dolo.id, "projection": 999, "ledger": 26}]
    assert stock_ledger.rebuild_projection(db) == 1
    db.commit()
    assert stock(db, dolo) == 26 and stock_ledger.reconcile(db) == []


# --------------------------------------------------------------------------- opening import & APIs
def test_opening_import_counts_packs_and_loose_units(db):
    sheet = ("Product Name,Pack,Base Unit,Pack Unit,Units Per Pack,Loose Sale,Batch,Expiry,Qty,Loose Qty,MRP\n"
             "DOLO 650MG TAB 15'S,15S,Tablet,Strip,15,Y,DOBS4401,02/2099,44,6,32.10\n"
             "CALCIJOINT SYRUP,200ML,Bottle,Bottle,,,AY1,11/2099,48,,145\n"
             "ENJOY 1X3,1X3,,,,,E1,01/2099,10,,60\n")
    result = inv.import_items(db, "opening.csv", sheet.encode())
    db.commit()
    assert result["errors"] == []
    dolo = db.query(Item).filter(Item.name.like("DOLO%")).one()
    assert (dolo.units_per_pack, dolo.loose_sale, dolo.base_unit, dolo.total_stock) == (15, True, "TABLET", 666)
    syrup = db.query(Item).filter(Item.name.like("CALCI%")).one()
    assert (syrup.units_per_pack, syrup.loose_sale, syrup.total_stock) == (1, False, 48)
    # 1X3 without an explicit loose flag stays a single retail pack
    enjoy = db.query(Item).filter(Item.name.like("ENJOY%")).one()
    assert (enjoy.units_per_pack, enjoy.total_stock) == (1, 10)
    assert {m.movement_type for m in db.query(InventoryMovement)} == {"OPENING_STOCK"}
    assert_reconciled(db)


def test_pos_search_payload_has_packaging_and_no_cost(client, db):
    from tests.conftest import login

    dolo = make_dolo(db)
    receive(db, dolo, 44)
    db.commit()
    login(client)
    item = client.get("/api/items/search", params={"q": "DOLO"}).json()["items"][0]
    assert (item["units_per_pack"], item["loose_sale"], item["stock"]) == (15, True, 660)
    assert item["stock_label"] == "44 strips"
    assert item["batches"][0]["unit_mrp"] == "2.14"
    flat = str(item)
    assert "purchase_rate" not in flat and "cost" not in flat and "selling_rate" not in flat


def test_inventory_reset_zeroes_stock_through_the_ledger(db):
    from app.services import inventory_reset

    dolo = make_dolo(db)
    receive(db, dolo, 2)
    sell(db, dolo, 4)
    result = inventory_reset.reset(db, mode="catalog", reason="fresh import")
    db.commit()
    assert result["units"] == 26 and result["binned"] == 1
    assert stock(db, dolo) == 0 and db.get(Item, dolo.id).deleted_at is not None
    last = db.query(InventoryMovement).order_by(InventoryMovement.id.desc()).first()
    assert (last.movement_type, last.quantity, last.reason) == ("ADJUSTMENT_OUT", -26, "fresh import")
    assert db.query(Sale).count() == 1  # history untouched
    assert_reconciled(db)


def test_data_reset_clears_business_data_and_keeps_setup(db):
    from app.models import Customer, User
    from app.services import data_reset

    dolo = make_dolo(db)
    receive(db, dolo, 2)
    sell(db, dolo, 3)
    db.add(Customer(customer_id="CUST-1", name="Test"))
    db.commit()
    users = db.query(User).count()
    data_reset.wipe(db)
    db.commit()
    counts = data_reset.counts(db)
    assert counts.pop("audit_logs") == 1  # only the reset itself
    assert set(counts.values()) == {0}
    assert db.query(User).count() == users
