"""Udhaar (customer store credit) and the Counter Report."""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.models import CounterDayClose, Sale, UdhaarEntry, UdhaarPayment, UdhaarReminder, WhatsAppMessage
from app.services import business_time, counter_service, customer_service, refund_service, sales_service, udhaar_service
from app.services import inventory_service as inv
from tests.conftest import login


def _setup(db, mrp="100"):
    item = inv.create_item(db, name="CALPOL 500 TAB", pack_size="1")
    inv.add_or_update_batch(db, item, batch_no="C1", quantity=100, unit="PACK", movement_type="OPENING_STOCK", mrp=mrp)
    cust = customer_service.create_customer(db, name="Ravi Kumar", mobile="9876500011")
    db.commit()
    return item, cust


def _terms(db, days=10, remind=9):
    today = business_time.current_business_date(db)
    return {"due_date": (today + timedelta(days=days)).isoformat(), "reminder_date": (today + timedelta(days=remind)).isoformat()}


def _udhaar_sale(db, item, cust, qty=20, **kw):
    return sales_service.create_sale(db, lines=[{"item_id": item.id, "quantity": qty}], customer_id=cust.id,
                                     payment_mode="UDHAAR", udhaar=kw.pop("udhaar", _terms(db)), **kw)


# ----------------------------------------------------------------------------- rules at the counter
def test_udhaar_needs_customer_mobile_and_dates(db):
    item, cust = _setup(db)
    with pytest.raises(sales_service.SaleError, match="needs a customer"):
        sales_service.create_sale(db, lines=[{"item_id": item.id, "quantity": 1}], payment_mode="UDHAAR", udhaar=_terms(db))
    db.rollback()
    with pytest.raises(sales_service.SaleError, match="due date and the reminder date"):
        sales_service.create_sale(db, lines=[{"item_id": item.id, "quantity": 1}], customer_id=cust.id, payment_mode="UDHAAR")
    db.rollback()
    with pytest.raises(sales_service.SaleError, match="reminder date must be on or before"):
        _udhaar_sale(db, item, cust, udhaar=_terms(db, days=3, remind=5))
    db.rollback()
    with pytest.raises(sales_service.SaleError, match="cannot be before today"):
        _udhaar_sale(db, item, cust, udhaar=_terms(db, days=-1, remind=-2))
    db.rollback()
    nomobile = customer_service.create_customer(db, name="No Phone")
    db.commit()
    with pytest.raises(sales_service.SaleError, match="no mobile"):
        _udhaar_sale(db, item, nomobile)
    db.rollback()
    # cash / UPI bills need none of it
    sale = sales_service.create_sale(db, lines=[{"item_id": item.id, "quantity": 1}], payment_mode="UPI")
    db.commit()
    assert udhaar_service.entry_for_sale(db, sale.id) is None


def test_udhaar_sale_writes_the_ledger_and_partial_payments_reduce_it(db):
    item, cust = _setup(db)
    sale = _udhaar_sale(db, item, cust)                 # 20 × ₹100 = ₹2,000
    db.commit()
    e = udhaar_service.entry_for_sale(db, sale.id)
    assert (e.amount, e.paid, e.status) == (Decimal("2000.00"), Decimal("0.00"), "OPEN")
    assert sale.payment_mode == "UDHAAR" and sales_service.payment_label(sale) == "Udhaar"
    assert sale.tendered_amount is None
    today = business_time.current_business_date(db)
    assert udhaar_service.status(e, today) == "Upcoming"
    udhaar_service.receive_payment(db, cust, amount="500", mode="CASH")
    db.commit()
    assert (e.paid, e.amount - e.paid) == (Decimal("500.00"), Decimal("1500.00"))
    assert udhaar_service.status(e, today) == "Partially Paid"
    assert db.get(Sale, sale.id).total == Decimal("2000.00")             # the sale itself never changes
    with pytest.raises(udhaar_service.UdhaarError, match="Only ₹1500.00"):
        udhaar_service.receive_payment(db, cust, amount="1600", mode="UPI")
    udhaar_service.receive_payment(db, cust, amount="1500", mode="UPI", reference="UTR1")
    db.commit()
    assert e.status == "PAID" and udhaar_service.status(e, today) == "Paid"
    assert [(p.mode, p.amount) for p in db.query(UdhaarPayment).order_by(UdhaarPayment.id)] == [("CASH", Decimal("500.00")), ("UPI", Decimal("1500.00"))]
    with pytest.raises(udhaar_service.UdhaarError, match="owes nothing"):
        udhaar_service.receive_payment(db, cust, amount="1", mode="CASH")


def test_statuses_by_date_and_oldest_dues_paid_first(db):
    item, cust = _setup(db)
    a = _udhaar_sale(db, item, cust, qty=3)
    b = _udhaar_sale(db, item, cust, qty=5)
    db.commit()
    ea, eb = udhaar_service.entry_for_sale(db, a.id), udhaar_service.entry_for_sale(db, b.id)
    today = business_time.current_business_date(db)
    ea.due_date, eb.due_date = today - timedelta(days=4), today              # as if billed earlier
    db.commit()
    assert udhaar_service.status(ea, today) == "Overdue" and udhaar_service.status(eb, today) == "Due Today"
    row = udhaar_service.row(ea, today, "Asia/Kolkata")
    assert row["days_overdue"] == 4 and row["balance"] == "300.00"
    udhaar_service.receive_payment(db, cust, amount="400", mode="CASH")      # oldest due first: 300 on A, 100 on B
    db.commit()
    assert (ea.paid, eb.paid, ea.status) == (Decimal("300.00"), Decimal("100.00"), "PAID")
    assert udhaar_service.outstanding(db, cust.id) == Decimal("400.00")


def test_limit_split_payment_void_and_edit_rules(db):
    item, cust = _setup(db)
    udhaar_service.set_terms(db, cust, limit="1000", days=7)
    db.commit()
    with pytest.raises(sales_service.SaleError, match="Udhaar limit"):
        _udhaar_sale(db, item, cust, qty=11)
    db.rollback()
    # ₹500 now in cash, ₹300 on Udhaar
    split = sales_service.create_sale(db, lines=[{"item_id": item.id, "quantity": 8}], customer_id=cust.id, payment_mode="SPLIT",
                                      payments=[{"mode": "CASH", "amount": 500}, {"mode": "UDHAAR", "amount": 300}],
                                      cash_received=500, udhaar=_terms(db))
    db.commit()
    e = udhaar_service.entry_for_sale(db, split.id)
    assert e.amount == Decimal("300.00") and split.payment_mode == "SPLIT" and split.change_amount == Decimal("0.00")
    due, reminder = udhaar_service.proposed_dates(db, cust)
    assert (due - business_time.current_business_date(db)).days == 7 and reminder == due - timedelta(days=1)
    # edit before any repayment: allowed, the entry follows the bill
    sales_service.amend_sale(db, split, lines=[{"item_id": item.id, "quantity": 8}], customer_id=cust.id, payment_mode="UDHAAR",
                             payments=[{"mode": "UDHAAR", "amount": 800}], udhaar={"due_date": e.due_date.isoformat(), "reminder_date": e.reminder_date.isoformat()})
    db.commit()
    assert e.amount == Decimal("800.00")
    udhaar_service.receive_payment(db, cust, amount="100", mode="CASH")
    db.commit()
    assert "Udhaar payments were received" in sales_service.editable_problem(db, split)
    with pytest.raises(sales_service.SaleError, match="already received"):
        sales_service.void_sale(db, split, reason="mistake")
    db.rollback()
    other = _udhaar_sale(db, item, cust, qty=1)
    db.commit()
    sales_service.void_sale(db, other, reason="wrong customer")
    db.commit()
    assert udhaar_service.entry_for_sale(db, other.id).status == "CANCELLED"
    assert udhaar_service.outstanding(db, cust.id) == Decimal("700.00")


def test_return_on_an_udhaar_bill_reduces_what_is_owed(db):
    item, cust = _setup(db)
    sale = _udhaar_sale(db, item, cust, qty=10)
    db.commit()
    line = sale.items[0]
    for override in (False, True):                       # not even a manager override pays cash out for unpaid goods
        with pytest.raises(refund_service.RefundError, match="refund by Udhaar"):
            refund_service.create_return(db, sale, lines=[{"sale_item_id": line.id, "quantity": 2}], refund_method="CASH",
                                         allow_override=override)
        db.rollback()
    ret = refund_service.create_return(db, sale, lines=[{"sale_item_id": line.id, "quantity": 2}], refund_method="UDHAAR")
    db.commit()
    e = udhaar_service.entry_for_sale(db, sale.id)
    assert ret.total_refund == Decimal("200.00") and e.paid == Decimal("200.00") and e.amount == Decimal("1000.00")
    assert db.query(UdhaarPayment).one().mode == "RETURN"


def test_opening_balance_terms_and_reminders(db, monkeypatch):
    from app.services.whatsapp import service as wa
    from tests.test_whatsapp import FakeGateway

    item, cust = _setup(db)
    t = _terms(db)
    e = udhaar_service.add_opening(db, cust, amount="750", due_date=t["due_date"], reminder_date=t["reminder_date"], note="old notebook")
    db.commit()
    assert e.kind == "OPENING" and udhaar_service.label(e) == "Opening balance"
    rem = udhaar_service.send_reminder(db, e, channel="CALL", note="spoke to wife")
    db.commit()
    assert rem.channel == "CALL" and rem.balance == Decimal("750.00")
    fake = FakeGateway()
    fake.not_on, fake.unknown = set(), set()
    wa._provider_override = fake
    wa._cache.update(at=0.0, conn=None)
    try:
        r2 = udhaar_service.send_reminder(db, e)
        db.commit()
        msg = db.get(WhatsAppMessage, r2.whatsapp_message_id)
        assert msg.kind == "UDHAAR_REMINDER" and "₹750.00" in msg.message_text and msg.sale_id is None
        wa.process_due(db)
        db.refresh(msg)
        assert msg.status == "SENT" and fake.sent[0][0] == "919876500011" and fake.sent[0][3] == "text/plain"
        # automatic reminders: off by default, then one per reminder date
        from app.services.settings_service import set_setting
        sale = _udhaar_sale(db, item, cust, qty=1)
        db.commit()
        entry = udhaar_service.entry_for_sale(db, sale.id)
        entry.reminder_date = business_time.current_business_date(db)
        db.commit()
        assert udhaar_service.send_due_reminders(db) == 0
        set_setting(db, "udhaar_auto_reminders", "true")
        db.commit()
        assert udhaar_service.send_due_reminders(db) == 1
        assert udhaar_service.send_due_reminders(db) == 0                      # not again for the same date
        assert db.query(UdhaarReminder).filter(UdhaarReminder.automatic.is_(True)).count() == 1
    finally:
        wa._provider_override = None
    led = udhaar_service.ledger(db, cust)
    assert led["lines"][-1]["balance"] == "850.00" and len(led["reminders"]) == 3
    assert "UDHAAR STATEMENT" in udhaar_service.statement_text(db, cust)


# ----------------------------------------------------------------------------- API
def test_udhaar_through_the_api(client, db):
    item, cust = _setup(db)
    login(client)
    s = client.get(f"/api/erp/udhaar/customers/{cust.id}").json()
    assert s["outstanding"] == "0.00" and s["due_date"] > s["reminder_date"]
    body = {"lines": [{"item_id": item.id, "quantity": 5}], "customer_id": cust.id, "payment_mode": "UDHAAR",
            "payments": [{"mode": "UDHAAR", "amount": 500}]}
    r = client.post("/api/sales", json=body)
    assert r.status_code == 400 and "due date" in r.json()["detail"]
    r = client.post("/api/sales", json={**body, "udhaar": {"due_date": s["due_date"], "reminder_date": s["reminder_date"]}})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["udhaar"]["amount"] == "500.00" and d["paid"] == "0.00" and d["payment_label"] == "Udhaar"
    lst = client.get("/api/erp/udhaar").json()
    assert lst["totals"]["outstanding"] == "500.00" and lst["rows"][0]["status"] == "Upcoming" and lst["rows"][0]["invoice"] == d["invoice_no"]
    assert client.get("/api/erp/udhaar", params={"q": "9876500011"}).json()["rows"]
    r = client.post(f"/api/erp/udhaar/customers/{cust.id}/receive", json={"amount": "200", "mode": "UPI", "entry_id": lst["rows"][0]["id"]})
    assert r.status_code == 200 and r.json()["outstanding"] == "300.00"
    assert client.get("/api/erp/udhaar").json()["rows"][0]["status"] == "Partially Paid"
    led = client.get(f"/api/erp/udhaar/customers/{cust.id}/ledger").json()
    assert [l["balance"] for l in led["lines"]] == ["500.00", "300.00"]
    assert client.get(f"/api/erp/udhaar/customers/{cust.id}/statement?print=1").status_code == 200
    r = client.post(f"/api/erp/udhaar/{lst['rows'][0]['id']}/remind", json={"channel": "CALL", "note": "called"})
    assert r.status_code == 200
    detail = client.get(f"/api/erp/sales/{d['sale_id']}").json()
    assert detail["udhaar"]["balance"] == "300.00" and detail["payment"] == "Udhaar"
    inv_view = client.get(f"/api/sales/{d['sale_id']}/invoice-view").json()
    assert inv_view["totals"]["duePaise"] == 50000 and any("Udhaar" in n for n in inv_view["notes"])
    assert client.put(f"/api/erp/udhaar/customers/{cust.id}/terms", json={"limit": "5000", "days": 30}).json()["days"] == 30


# ----------------------------------------------------------------------------- Counter Report
def test_counter_report_by_payment_mode_with_drill_down(db):
    item, cust = _setup(db)
    sales_service.create_sale(db, lines=[{"item_id": item.id, "quantity": 2}], payment_mode="CASH", cash_received=500)
    sales_service.create_sale(db, lines=[{"item_id": item.id, "quantity": 3}], payment_mode="UPI")
    sales_service.create_sale(db, lines=[{"item_id": item.id, "quantity": 1, "discount_pct": 10}], payment_mode="CARD")
    ud = _udhaar_sale(db, item, cust, qty=4)
    void = sales_service.create_sale(db, lines=[{"item_id": item.id, "quantity": 9}], payment_mode="CASH")
    db.commit()
    sales_service.void_sale(db, void, reason="test")
    cash = db.query(Sale).filter(Sale.payment_mode == "CASH", Sale.payment_status != "CANCELLED").one()
    refund_service.create_return(db, cash, lines=[{"sale_item_id": cash.items[0].id, "quantity": 1}], refund_method="CASH")
    udhaar_service.receive_payment(db, cust, amount="150", mode="CASH")
    db.commit()
    today = business_time.current_business_date(db)
    f = counter_service.compute(db, today)
    by = {m["mode"]: m for m in f["modes"]}
    assert f["bills"] == 4 and f["void"] == {"bills": 1, "total": "900.00"}
    assert (by["CASH"]["sales"], by["UPI"]["sales"], by["CARD"]["sales"], by["UDHAAR"]["sales"]) == ("200.00", "300.00", "90.00", "400.00")
    assert f["gross"] == "1000.00" and f["discounts"] == "10.00" and f["sales_total"] == "990.00"
    assert f["refunds"] == "100.00" and f["net"] == "890.00"
    assert by["CASH"]["received"] == "250.00"                 # 200 sales + 150 Udhaar collected − 100 refunded
    assert f["cash_in_hand"] == "250.00" and by["UDHAAR"]["received"] == "0.00"
    assert f["received"] == "640.00"                           # 250 cash + 300 UPI + 90 card
    assert f["udhaar_given"] == "400.00" and f["udhaar_collected"] == "150.00" and f["udhaar_outstanding"] == "250.00"
    rows = counter_service.documents(db, today, "UDHAAR")["rows"]
    assert [r["invoice_no"] for r in rows] == [ud.invoice_no] and rows[0]["amount"] == "400.00"
    assert len(counter_service.documents(db, today, "VOID")["rows"]) == 1
    assert counter_service.documents(db, today, "COLLECTIONS")["rows"][0]["amount"] == "150.00"
    assert counter_service.documents(db, today, "RETURNS")["rows"][0]["amount"] == "100.00"
    rep = counter_service.report(db, today)
    assert rep["state"] == "OPEN" and db.query(CounterDayClose).count() == 0
    with pytest.raises(ValueError):
        counter_service.close_day(db, today)                   # the day is not over


def test_a_finished_day_is_frozen_and_later_edits_show_as_changes(db, monkeypatch):
    item, cust = _setup(db)
    sale = sales_service.create_sale(db, lines=[{"item_id": item.id, "quantity": 2}], payment_mode="CASH")
    db.commit()
    day = business_time.current_business_date(db)
    monkeypatch.setattr(business_time, "current_business_date", lambda _db: day + timedelta(days=1))   # midnight passed
    rep = counter_service.report(db, day)
    assert rep["state"] == "CLOSED" and rep["intact"] and rep["changes"] == [] and rep["figures"]["sales_total"] == "200.00"
    sales_service.void_sale(db, sale, reason="found later")
    db.commit()
    again = counter_service.report(db, day)
    assert again["figures"]["sales_total"] == "200.00"           # the closed figures never change
    assert {"field": "sales_total", "closed": "200.00", "now": "0.00"} in again["changes"]
    assert db.query(CounterDayClose).count() == 1
    counter_service._checked = None
    assert counter_service.close_finished_days(db) == 0          # already closed


def test_counter_report_api(client, db):
    item, cust = _setup(db)
    sales_service.create_sale(db, lines=[{"item_id": item.id, "quantity": 2}], payment_mode="CASH")
    db.commit()
    login(client)
    r = client.get("/api/erp/counter").json()
    assert r["state"] == "OPEN" and r["figures"]["modes"][0]["sales"] == "200.00"
    assert client.get("/api/erp/counter/documents", params={"mode": "CASH"}).json()["rows"][0]["total"] == "200.00"
    future = (date.today() + timedelta(days=5)).isoformat()
    assert client.get("/api/erp/counter", params={"day": future}).status_code == 400
