"""Opt-in tests for private supplied files; invoice contents never enter Git."""
import os
from pathlib import Path
import pytest
from app.models import InventoryMovement
from app.services import purchase_import, purchasing
from tests.test_purchasing import supplier

CORPUS = os.environ.get("PHARMACY_SUPPLIER_CORPUS_DIR", "")
pytestmark = pytest.mark.skipif(not CORPUS, reason="Private supplier corpus not configured")
FILES = [
    ("new_modern_pharma_items_all_21_pages.csv",350,1),
    ("SAMICO_PT2.csv",25,1),
    ("GULAB_PT1.csv",430,10),
    ("Faheem_Pharmacy_NR03897_SAMICO_204_Import.csv",208,1),
    ("Mohammadia_Gen_Store_Extracted_Table_No_NA.pdf",180,1),
]


@pytest.mark.parametrize("filename,rows,count",FILES)
def test_supplied_files_stage_every_row_without_stock(db, filename, rows, count):
    path = Path(CORPUS)/filename
    data = path.read_bytes()
    docs = purchasing.import_file(db,filename,data,supplier_id=supplier(db).id)
    assert len(docs) == count
    assert sum(len(d.items) for d in docs) == rows
    assert db.query(InventoryMovement).count() == 0
    if filename == "SAMICO_PT2.csv":
        invalid = [l for d in docs for l in d.items if "quantity_invalid" in {i["code"] for i in l.issues}]
        assert len(invalid) == 3
        assert all(not l.receipt_decision["resolved"] for l in invalid)
    if filename.endswith(".pdf"):
        assert docs[0].invoice_no == "0000000027_C1"
        assert docs[0].charges["_expected_lines"] == "180"
        assert docs[0].charges["_expected_qty"] == "372.00"
        assert not docs[0].charges.get("_extraction_issues")
