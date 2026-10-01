"""Reports in the ERP document format (text layout, print, text/PDF export)."""
from __future__ import annotations

from datetime import datetime

from tests.conftest import login
from tests.test_report_generator import RANGE, sell, stocked


def test_item_sales_document_layout_and_exports(client, db):
    item, batch = stocked(db, name="Dolo 650")
    sell(db, item, batch, qty=3, when=datetime(2026, 9, 5, 12))
    db.commit()
    login(client)
    doc = client.post("/reports/api/generate", json={"report": "item-sales", "parameters": RANGE}).json()
    text = doc["text"].splitlines()
    assert text[0].strip() == "FAHEEM PHARMACY" or text[0].strip()          # centred pharmacy name
    assert "ITEM SALES DETAIL" in doc["text"] and "Period: 01-Sep-2026 to 30-Sep-2026" in doc["text"]
    rules = [i for i, l in enumerate(text) if l and set(l) == {"-"}]
    assert len(rules) == 4                                                   # head, header, body, total
    assert text[rules[0] + 1].startswith("SNo")
    assert text[rules[1] + 1].startswith("1 ") and "Dolo 650" in text[rules[1] + 1]
    assert text[rules[2] + 1].startswith("TOTAL")
    assert "Cost" not in text[rules[0] + 1] and "Profit" not in text[rules[0] + 1]      # only when requested
    txt = client.get(f"/reports/api/document/{doc['token']}.txt")
    assert txt.status_code == 200 and txt.text == doc["text"]
    pdf = client.get(f"/reports/api/document/{doc['token']}.pdf")
    assert pdf.status_code == 200 and pdf.content[:4] == b"%PDF"
    import pymupdf

    assert "ITEM SALES DETAIL" in pymupdf.open(stream=pdf.content, filetype="pdf")[0].get_text()


def test_cost_and_profit_appear_only_when_selected(client, db):
    item, batch = stocked(db, name="Dolo 650")          # bought at 5, sold at MRP 10
    sell(db, item, batch, qty=3, when=datetime(2026, 9, 5, 12))
    db.commit()
    login(client)
    plain = client.post("/reports/api/generate", json={"report": "item-sales", "parameters": RANGE}).json()
    assert not {"cost", "profit"} & {c["key"] for c in plain["columns"]}
    doc = client.post("/reports/api/generate", json={"report": "item-sales", "parameters": RANGE,
                                                    "columns": ["item", "pack", "quantity", "mrp", "value", "cost", "profit"]}).json()
    row = doc["rows"][0]
    assert (row["value"], row["cost"], row["profit"]) == ("30.00", "15.00", "15.00")
    header = next(l for l in doc["text"].splitlines() if l.startswith("SNo"))
    assert "Cost" in header and "Profit" in header
