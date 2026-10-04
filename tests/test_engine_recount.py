"""Drafts made before an update are counted again once by the new engine when they are listed or
opened; a person's corrections stay; a current draft is not touched again."""
from __future__ import annotations

from sqlalchemy import func, select

from app.models import AuditLog, Purchase
from app.services import purchase_automation as auto
from app.services import purchasing
from tests.conftest import login
from tests.test_receipt_proposer import tab
from tests.test_purchasing import draft


def old_draft(db):
    """A draft as 1.8.0 left it: no engine stamp, its line not counted, one correction by a person."""
    tab(db)
    p = draft(db, ",EXAMPLE 10MG TAB,10S,B1,May-2028,2,,30,50,60")
    line = p.items[0]
    line.receipt_decision = {"resolved": False}
    line.status = "NEEDS_REVIEW"
    line.corrections = {"batch": {"value": "B1-CHECKED", "by": "abdul"}}
    p.charges = {k: v for k, v in (p.charges or {}).items() if k != "_engine"}
    db.commit()
    return p


def recounts(db, p):
    return db.scalar(select(func.count()).select_from(AuditLog).where(
        AuditLog.entity_type == "purchase", AuditLog.details.like("Counted again by engine%")))


def test_opening_an_old_draft_counts_it_again_once(client, db):
    login(client)
    p = old_draft(db)
    doc = client.get(f"/api/erp/purchases/{p.id}").json()
    line = doc["lines"][0]
    assert line["receipt"]["resolved"] is True and line["status"] in ("READY", "CORRECTED", "NEEDS_REVIEW")
    db.expire_all()
    p = db.get(Purchase, p.id)
    assert p.charges["_engine"] == auto.ENGINE_VERSION
    assert p.items[0].corrections["batch"]["value"] == "B1-CHECKED"           # the person's correction stays
    assert recounts(db, p) == 1
    client.get(f"/api/erp/purchases/{p.id}")
    assert recounts(db, p) == 1                                               # once, not on every open


def test_the_purchase_list_counts_old_drafts_too(client, db):
    login(client)
    p = old_draft(db)
    client.get("/api/erp/purchases")
    db.expire_all()
    p = db.get(Purchase, p.id)
    assert p.charges["_engine"] == auto.ENGINE_VERSION and p.items[0].receipt_decision["resolved"] is True


def test_posted_purchases_are_never_recounted(db):
    tab(db)
    p = draft(db, ",EXAMPLE 10MG TAB,10S,B1,May-2028,2,,30,50,60")
    purchasing.post(db, p, accept_warnings=True)
    p.charges = {k: v for k, v in (p.charges or {}).items() if k != "_engine"}
    db.commit()
    assert auto.refresh_if_stale(db, p) is False
