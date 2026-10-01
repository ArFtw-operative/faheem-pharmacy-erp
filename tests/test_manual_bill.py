"""Manual bills: typed lines, own number series, no stock ledger, cost unknown."""
from __future__ import annotations

from decimal import Decimal

import pytest

from app.models import InventoryMovement, Sale
from app.services import financials, refund_service, sales_service
from app.services import inventory_service as inv
from tests.conftest import login

LINES = [{"name": "Crepe bandage 10cm", "quantity": 2, "rate": "85"},
         {"name": "Hot water bag", "quantity": 1, "rate": "240", "discount_pct": 10}]


def test_manual_bill_has_its_own_series_and_never_touches_stock(db):
    item = inv.create_item(db, name="DOLO 650MG TAB", pack_size="15S")
    inv.add_or_update_batch(db, item, batch_no="B1", quantity=2, unit="PACK", movement_type="OPENING_STOCK", mrp="30")
    before = db.query(InventoryMovement).count()
    sale = sales_service.create_sale(db, lines=LINES, invoice_type="MANUAL", payment_mode="CASH", cash_received="500")
    db.commit()
    assert sale.invoice_no.startswith("MB-") and sale.invoice_type == "MANUAL"
    assert db.query(InventoryMovement).count() == before
    assert [l.item_id for l in sale.items] == [None, None]
    assert sale.subtotal == Decimal("386.00") and sale.total == Decimal("386.00")
    assert sale.change_amount == Decimal("114.00")
    stock = sales_service.create_sale(db, lines=[{"item_id": item.id, "quantity": 1}])
    assert stock.invoice_no.startswith("INV-")          # stock bills keep their own series


def test_manual_bill_rules(db):
    with pytest.raises(sales_service.SaleError, match="maximum allowed"):
        sales_service.create_sale(db, lines=[{"name": "X", "quantity": 1, "rate": "100", "discount_pct": 50}], invoice_type="MANUAL")
    with pytest.raises(sales_service.SaleError, match="whole number"):
        sales_service.create_sale(db, lines=[{"name": "X", "quantity": "1.5", "rate": "10"}], invoice_type="MANUAL")
    with pytest.raises(sales_service.SaleError, match="rate"):
        sales_service.create_sale(db, lines=[{"name": "X", "quantity": 1, "rate": "0"}], invoice_type="MANUAL")
    item = inv.create_item(db, name="PAN 40", pack_size="15S")
    with pytest.raises(sales_service.SaleError, match="batch"):
        sales_service.create_sale(db, lines=[{"item_id": item.id, "batch_id": 1, "name": "PAN 40", "quantity": 1, "rate": "5"}], invoice_type="MANUAL")


def test_manual_bill_takes_any_inventory_item_without_stock_and_typed_items(db):
    from app.services import report_generator as reports

    out_of_stock = inv.create_item(db, name="AZEE 500 TAB", pack_size="3S")              # no batch at all
    dolo = inv.create_item(db, name="DOLO 650 TAB", pack_size="15S", base_unit="TABLET", pack_unit="STRIP", units_per_pack=15)
    inv.add_or_update_batch(db, dolo, batch_no="D1", quantity=1, unit="PACK", movement_type="OPENING_STOCK", mrp="30")
    db.commit()
    moves = db.query(InventoryMovement).count()
    sale = sales_service.create_sale(db, invoice_type="MANUAL", round_off_mode="NONE", lines=[
        {"item_id": out_of_stock.id, "name": "AZEE 500 TAB", "quantity": 2, "rate": "98.50", "discount_pct": 5},
        {"item_id": dolo.id, "name": "DOLO 650 TAB", "quantity": 4, "rate": "30"},          # more than the 1 strip in stock
        {"name": "Crepe bandage 10cm", "quantity": 1, "rate": "85"}])
    db.commit()
    assert db.query(InventoryMovement).count() == moves                                  # stock neither checked nor changed
    assert [l.item_id for l in sale.items] == [out_of_stock.id, dolo.id, None]
    assert sale.items[0].pack_size == "3S" and sale.items[0].discount == Decimal("9.85")
    assert sale.total == Decimal("187.15") + Decimal("120") + Decimal("85")
    rep = reports.generate(db, "item-wise-sales", {"period": "this_month"}, ["item", "sold", "value"], user=None)
    by = {r["item"]: r for r in rep["rows"]}
    assert by["DOLO 650 TAB · manual bill"]["sold"] == 4 and by["Crepe bandage 10cm · manual bill"]["sold"] == 1
    bills = reports.generate(db, "bill-register", {"period": "this_month"}, user=None)
    assert any(sale.invoice_no in str(r.values()) for r in bills["rows"])                 # in the bill register too


def test_manual_bill_cost_is_unknown_not_zero(db):
    sale = sales_service.create_sale(db, lines=LINES, invoice_type="MANUAL")
    db.commit()
    assert all(financials.effective_cost(l) is None for l in sale.items)


def test_manual_bill_return_does_not_touch_inventory(db):
    sale = sales_service.create_sale(db, lines=LINES, invoice_type="MANUAL")
    db.commit()
    line = sale.items[0]
    ret = refund_service.create_return(db, sale, lines=[{"sale_item_id": line.id, "quantity": 1}], reason_code="DAMAGED")
    db.commit()
    assert ret.total_refund > 0 and db.query(InventoryMovement).count() == 0


def test_api_manual_bill(client, db):
    login(client)
    r = client.post("/api/sales", json={"invoice_type": "MANUAL", "lines": LINES, "payment_mode": "SPLIT",
                                        "payments": [{"mode": "CASH", "amount": 186}, {"mode": "UPI", "amount": 200}]})
    assert r.status_code == 200, r.text
    sale = db.get(Sale, r.json()["sale_id"])
    assert sale.invoice_type == "MANUAL" and len(sale.payments) == 2
    assert client.get(f"/sales/{sale.id}/invoice").status_code == 200


def test_manual_lines_keep_batch_and_expiry_and_reopen_as_manual_for_editing(client, db):
    from datetime import date

    login(client)
    dolo = inv.create_item(db, name="DOLO 650 TAB", pack_size="15S")                      # no stock at all
    db.commit()
    sale = sales_service.create_sale(db, invoice_type="MANUAL", round_off_mode="NONE", lines=[
        {"item_id": dolo.id, "name": "DOLO 650 TAB", "quantity": 3, "rate": "30", "batch": " DB 44 ", "expiry": "05/28"},
        {"name": "Hot water bag", "quantity": 1, "rate": "240"}])
    db.commit()
    assert (sale.items[0].batch_no, sale.items[0].expiry_date) == ("DB 44", date(2028, 5, 1))
    with pytest.raises(sales_service.SaleError, match="MM/YY"):
        sales_service.create_sale(db, invoice_type="MANUAL", lines=[{"name": "X", "quantity": 1, "rate": "5", "expiry": "soon"}])
    db.rollback()
    payload = client.get(f"/api/erp/sales/{sale.id}/edit").json()
    assert payload["invoice_type"] == "MANUAL" and all(l["manual"] for l in payload["lines"])    # not a stock bill
    first = payload["lines"][0]
    assert (first["item_id"], first["batch"], first["expiry"]) == (dolo.id, "DB 44", "05/28")
    moves = db.query(InventoryMovement).count()
    r = client.put(f"/api/sales/{sale.id}", json={"payment_mode": "CASH", "round_off_mode": "NONE", "lines": [
        {"item_id": dolo.id, "name": "DOLO 650 TAB", "quantity": 5, "rate": "28", "batch": "DB45", "expiry": "06/2028"},
        {"name": "Hot water bag", "quantity": 1, "rate": "240"}]})
    assert r.status_code == 200, r.text
    db.expire_all()
    edited = db.get(Sale, sale.id)
    assert edited.invoice_no == sale.invoice_no and edited.total == Decimal("380.00")
    assert (edited.items[0].quantity, edited.items[0].batch_no, edited.items[0].expiry_date) == (5, "DB45", date(2028, 6, 1))
    assert db.query(InventoryMovement).count() == moves                                     # still never touches stock


def test_manual_line_code_is_kept_and_defaults_to_the_product_code(client, db):
    login(client)
    item = inv.create_item(db, name="PAN 40 TAB", pack_size="15S")
    db.commit()
    sale = sales_service.create_sale(db, invoice_type="MANUAL", lines=[
        {"item_id": item.id, "name": "PAN 40 TAB", "quantity": 1, "rate": "12"},                 # no code sent
        {"item_id": item.id, "name": "PAN 40 TAB", "code": "PAN-X1", "quantity": 1, "rate": "12"},
        {"name": "Crepe bandage", "code": " CB 10 ", "pack": "1 roll", "quantity": 1, "rate": "85"}])
    db.commit()
    assert [l.item_code for l in sale.items] == [item.article_id, "PAN-X1", "CB 10"]
    assert sale.items[2].pack_size == "1 roll"
    lines = client.get(f"/api/erp/sales/{sale.id}/edit").json()["lines"]
    assert [l["code"] for l in lines] == [item.article_id, "PAN-X1", "CB 10"] and lines[2]["pack"] == "1 roll"
