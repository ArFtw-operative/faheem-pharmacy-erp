"""Split payments, per-item discount %, editing and voiding invoices."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.models import Batch
from app.services import customer_service, invoice_kit, inventory_service as inv
from app.services import refund_service, reports_service, sales_service
from tests.conftest import login


def _stock(db, name="Split Med", qty=50, rate="100"):
    item = inv.create_item(db, name=name)
    batch = inv.add_or_update_batch(
        db, item, batch_no="SP1", expiry_date=date(2030, 1, 1), quantity=qty,
        purchase_rate="60", selling_rate=rate, mrp=rate,
    )
    db.commit()
    return item, batch


def _line(item, batch, qty=5, **extra):
    return {"item_id": item.id, "batch_id": batch.id, "quantity": qty, "rate": "100", **extra}


# --------------------------------------------------------------------------- split
def test_split_must_add_up_to_the_bill(db):
    item, batch = _stock(db)
    with pytest.raises(sales_service.SaleError, match="add up to 450.00 but the bill is 500.00"):
        sales_service.create_sale(
            db, lines=[_line(item, batch)],
            payments=[{"mode": "CASH", "amount": "250"}, {"mode": "CARD", "amount": "200"}],
        )
    db.rollback()
    assert db.get(Batch, batch.id).quantity == 50  # nothing was taken from stock


def test_cash_short_of_the_cash_part_is_rejected(db):
    item, batch = _stock(db)
    with pytest.raises(sales_service.SaleError, match="Insufficient cash"):
        sales_service.create_sale(
            db, lines=[_line(item, batch)],
            payments=[{"mode": "CASH", "amount": "300"}, {"mode": "CARD", "amount": "200"}],
            cash_received="250",
        )


def test_reports_count_each_part_under_its_method(db):
    item, batch = _stock(db)
    sales_service.create_sale(db, lines=[_line(item, batch)],
                              payments=[{"mode": "CASH", "amount": "300"}, {"mode": "UPI", "amount": "200"}])
    sales_service.create_sale(db, lines=[_line(item, batch, qty=1)], payment_mode="CARD")
    db.commit()
    start, end = reports_service.period_range("today")
    modes = {m["mode"]: m["total"] for m in reports_service.sales_summary(db, start, end)["by_payment_mode"]}
    assert modes == {"CASH": Decimal("300.00"), "UPI": Decimal("200.00"), "CARD": Decimal("100.00")}


def test_invoice_view_lists_every_payment(db):
    item, batch = _stock(db)
    sale = sales_service.create_sale(db, lines=[_line(item, batch)],
                                     payments=[{"mode": "CASH", "amount": "300"}, {"mode": "CARD", "amount": "200"}])
    db.commit()
    data = invoice_kit.build_invoice_view(db, sale)
    assert [(p["method"], p["amountPaise"]) for p in data["payments"]] == [("Cash", 30000), ("Card", 20000)]
    assert data["paymentText"].startswith("Cash + Card")


# --------------------------------------------------------------------------- discount %
def test_per_item_discount_percent(db):
    item, batch = _stock(db)
    other, other_batch = _stock(db, name="Undiscounted Med")
    sale = sales_service.create_sale(
        db, lines=[_line(item, batch, qty=2, discount_pct=10), _line(other, other_batch, qty=1)],
        payment_mode="UPI",
    )
    db.commit()
    first, second = sorted(sale.items, key=lambda i: i.product_name)
    assert second.product_name == "Undiscounted Med" and second.discount == Decimal("0")
    assert first.discount == Decimal("20.00") and first.line_total == Decimal("180.00")
    assert sale.total == Decimal("280.00")
    # the invoice shows the 10% on its own line, not spread over the other one
    data = invoice_kit.build_invoice_view(db, sale)
    by_name = {i["name"]: i for i in data["items"]}
    assert by_name["Split Med"]["discountPaise"] == 2000 and by_name["Undiscounted Med"]["discountPaise"] == 0


def test_discount_percent_out_of_range(db):
    item, batch = _stock(db)
    with pytest.raises(sales_service.SaleError, match="maximum allowed is 20%"):  # store limit, not just 100%
        sales_service.create_sale(db, lines=[_line(item, batch, discount_pct=120)], payment_mode="CASH")


# --------------------------------------------------------------------------- edit
def test_returned_or_void_invoices_cannot_be_edited(db):
    item, batch = _stock(db)
    sale = sales_service.create_sale(db, lines=[_line(item, batch, qty=2)], payment_mode="CASH")
    db.commit()
    line = sale.items[0]
    refund_service.create_return(db, sale, lines=[{"sale_item_id": line.id, "quantity": 1}],
                                 refund_method="CASH", reason_code="DAMAGED")
    db.commit()
    assert "returned" in sales_service.editable_problem(db, sale)
    with pytest.raises(sales_service.SaleError):
        sales_service.void_sale(db, sale)

    other = sales_service.create_sale(db, lines=[_line(item, batch, qty=1)], payment_mode="CASH")
    sales_service.void_sale(db, other, reason="test")
    db.commit()
    assert sales_service.editable_problem(db, other) == "This invoice is void"


def test_history_search_by_customer_and_filters(client, db):
    item, batch = _stock(db)
    asha = customer_service.create_customer(db, name="Asha Rao", mobile="98765 43210")
    db.commit()
    s1 = sales_service.create_sale(db, lines=[_line(item, batch, qty=1)], customer_id=asha.id,
                                   payments=[{"mode": "CASH", "amount": "50"}, {"mode": "UPI", "amount": "50"}])
    s2 = sales_service.create_sale(db, lines=[_line(item, batch, qty=1)], payment_mode="CARD")
    s3 = sales_service.create_sale(db, lines=[_line(item, batch, qty=1)], payment_mode="CASH")
    sales_service.void_sale(db, s3, reason="t")
    db.commit()
    find = lambda **f: [s.invoice_no for s in sales_service.search_sales(db, **f)[0]]
    assert find(q="asha") == [s1.invoice_no]
    assert find(q="9876543210") == [s1.invoice_no]
    assert find(payment="UPI") == [s1.invoice_no]
    assert set(find(payment="CASH")) == {s1.invoice_no, s3.invoice_no}
    assert find(status="void") == [s3.invoice_no]
    summary = sales_service.sales_summary(db)
    assert summary["bills"] == 2 and summary["void"] == 1 and summary["net"] == Decimal("200.00")
    assert summary["by_mode"]["CARD"] == Decimal("100.00")

    login(client)
    found = [r["invoice_no"] for r in client.get("/api/erp/sales", params={"q": "asha"}).json()["sales"]]
    assert s1.invoice_no in found and s2.invoice_no not in found
    assert client.get("/api/erp/sales", params={"start": "2026-01-01", "end": "2030-01-01"}).status_code == 200
    csv = client.get("/sales/export/csv", params={"payment": "UPI"}).text
    assert s1.invoice_no in csv and s2.invoice_no not in csv


def test_history_page_with_payment_filter_renders(client, db):
    item, batch = _stock(db)
    sales_service.create_sale(db, lines=[_line(item, batch, qty=1)], payment_mode="CARD")
    sales_service.create_sale(db, lines=[_line(item, batch, qty=1)],
                              payments=[{"mode": "CASH", "amount": "50"}, {"mode": "UPI", "amount": "50"}])
    db.commit()
    login(client)
    for mode in ("", "CASH", "UPI", "CARD", "SPLIT"):
        for status in ("", "paid", "returned", "void"):
            resp = client.get("/api/erp/sales", params={"payment": mode, "status": status, "q": "x"})
            assert resp.status_code == 200, (mode, status)
    assert sales_service.sales_summary(db, payment="CARD")["by_mode"]["CARD"] == Decimal("100.00")
    assert sales_service.sales_summary(db, payment="UPI")["bills"] == 1
