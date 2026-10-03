"""GS1 / GTIN parsing and its use on purchase lines."""
from __future__ import annotations

from datetime import date

from app.services import barcode_service as bc
from app.services import inventory_service as inv
from app.services import purchasing
from tests.test_purchasing import supplier


def test_bracketed_element_string():
    g = bc.parse("(01)08901030865275(17)280500(10)AB-12/X(21)SN99")
    assert (g.gtin, g.batch, g.expiry, g.serial) == ("08901030865275", "AB-12/X", date(2028, 5, 31), "SN99")


def test_raw_element_string_with_group_separators():
    g = bc.parse("01089010308652751728053110LOT001\x1d21SER1")
    assert (g.gtin, g.expiry, g.batch, g.serial) == ("08901030865275", date(2028, 5, 31), "LOT001", "SER1")


def test_day_zero_is_the_last_day_of_the_month():
    assert bc.parse("(17)270200(10)B1").expiry == date(2027, 2, 28)


def test_plain_gtin_and_check_digit():
    assert bc.parse("8901030865275").gtin == "08901030865275"
    assert bc.parse("8901030865276") is None          # wrong check digit
    assert bc.parse("HELLO") is None


def test_gs1_on_a_line_matches_the_product_and_fills_blank_batch_and_expiry(db):
    item = inv.create_item(db, name="ANY NAME TAB", pack_size="10S", base_unit="TABLET", pack_unit="STRIP",
                           units_per_pack=10, dosage_form="TABLET", barcode="8901030865275")
    sup = supplier(db)
    content = "Product,Pack,Batch,Expiry,Qty,Rate,MRP,Barcode\nSUPPLIER CALLS IT SOMETHING,10S,,,2,10,20,(01)08901030865275(17)280500(10)LOT9\n"
    p = purchasing.create_from_file(db, "g.csv", content.encode(), supplier_id=sup.id, invoice_no="G-1")
    line = p.items[0]
    assert line.item_id == item.id and line.match_method == "GS1"
    assert line.batch_no == "LOT9" and line.expiry_date == date(2028, 5, 1)
    assert line.raw["batch"] == ""                        # the supplier's own (blank) value is kept
