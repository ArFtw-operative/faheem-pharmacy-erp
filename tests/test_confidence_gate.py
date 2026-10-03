"""Confidence gate: per-field confidence, decision states, configurable thresholds."""
from __future__ import annotations

import json

from app.models import ImportMetric
from app.routers.purchases import _line_view
from app.services import confidence_gate as gate
from app.services import settings_service
from tests.conftest import login
from tests.test_purchasing import csv_bytes, dolo, draft, supplier


def test_matched_resolved_line_is_auto_accepted(db):
    dolo(db)
    p = draft(db, "D650,DOLO 650MG TAB,15S,DB1,May-2028,10,2,24.00,33.60,240.00")
    v = gate.assess(p.items[0])
    assert v["state"] == gate.AUTO_ACCEPT and v["fields"]["product"] >= 0.97
    assert v["provenance"]["product"] == "PRODUCT_MASTER" and v["provenance"]["batch"] == "SOURCE_FILE"


def test_one_weak_field_does_not_make_the_row_manual(db):
    dolo(db)
    p = draft(db, "D650,DOLO 650MG TAB,15S,,May-2028,10,2,24.00,33.60,240.00")   # no batch printed
    v = gate.assess(p.items[0])
    assert v["state"] == gate.WARNING and v["weakest"] == "batch"
    assert v["fields"]["quantity"] == 1.0 and v["fields"]["packaging"] >= 0.97


def test_unresolved_pack_goes_to_review_with_quantity_still_known(db):
    p = draft(db, ",MYSTERY BRAND,10ML57,B1,May-2028,2,1,10,20,20")
    v = gate.assess(p.items[0])
    assert v["state"] == gate.REVIEW
    assert v["fields"]["quantity"] == 1.0 and v["fields"]["packaging"] == 0.5
    assert _line_view(p.items[0])["stock"]["stock"] == "3 purchase packs"


def test_damaged_batch_blocks(db):
    dolo(db)
    p = draft(db, "D650,DOLO 650MG TAB,15S,2.61E+09,May-2028,10,2,24.00,33.60,240.00")
    assert gate.assess(p.items[0])["state"] == gate.BLOCK


def test_thresholds_are_configuration(db):
    dolo(db)
    p = draft(db, "D650,DOLO 650MG TAB,15S,DB1,May-2028,10,2,24.00,33.60,240.00")
    settings_service.set_setting(db, "purchase_gate_thresholds", json.dumps({"auto": 0.99}))
    limits = gate.thresholds(db)
    assert limits["auto"] == 0.99 and limits["warn"] == 0.90
    assert gate.assess(p.items[0], limits)["state"] == gate.WARNING     # exact name 0.98 < 0.99


def test_document_summary_counts_gate_states_and_import_records_metrics(client, db):
    login(client)
    dolo(db)
    sup = supplier(db)
    db.commit()
    content = csv_bytes("D650,DOLO 650MG TAB,15S,DB1,May-2028,10,2,24.00,33.60,240.00", ",UNKNOWN,10ML57,X,May-2028,1,,1,2,1")
    r = client.post("/api/erp/purchases/import", files={"file": ("g.csv", content, "text/csv")},
                    data={"supplier_id": str(sup.id), "invoice_no": "G-1"})
    assert r.status_code == 200, r.text
    g = r.json()["summary"]["gate"]
    assert g["AUTO_ACCEPT"] == 1 and g["REVIEW"] == 1
    m = db.query(ImportMetric).one()
    assert (m.lines, m.auto_accepted, m.review, m.route) == (2, 1, 1, "STRUCTURED")
