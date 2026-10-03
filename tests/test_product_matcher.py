"""Product matching cascade: normalised names, attribute rules, explainable scores."""
from __future__ import annotations

from app.services import inventory_service as inv
from app.services import product_matcher as pm
from app.services import purchasing
from tests.conftest import login
from tests.test_purchasing import draft, supplier


def tab(db, name, pack="10S", form="TABLET"):
    return inv.create_item(db, name=name, pack_size=pack, base_unit="TABLET", pack_unit="STRIP", units_per_pack=10, dosage_form=form)


def test_superficial_differences_match_without_a_person(db):
    item = tab(db, "ROSUBEST-10 TABLET")
    p = draft(db, ",ROSUBEST 10 TAB,10S,B1,May-2028,1,,50,90,50")
    assert p.items[0].item_id == item.id and p.items[0].match_method == "NORMALIZED_NAME"
    assert p.items[0].status == "READY"


def test_gel_spellings_resolve_to_the_same_product(db):
    gel = inv.create_item(db, name="OMNIGEL 20 GM", pack_size="20GM", base_unit="TUBE", pack_unit="TUBE", units_per_pack=1, dosage_form="GEL")
    p = draft(db, ",OMNIGEL 20G,20GM,B1,May-2028,3,,80,120,240", ",OMNI GEL 20 GMS,20 GM,B2,May-2028,1,,80,120,80")
    assert [l.item_id for l in p.items] == [gel.id, gel.id]
    from app.routers.purchases import _line_view
    view = _line_view(p.items[0])["stock"]
    assert (view["stock"], view["equivalent"]) == ("3 tubes", "3 × 20 g")


def test_different_strength_never_matches(db):
    tab(db, "DRUG 10 mg TAB")
    p = draft(db, ",DRUG 5 mg TAB,10S,B1,May-2028,1,,50,90,50")
    assert p.items[0].item_id is None
    sug = purchasing.suggestions(db, p.items[0])
    assert all("different strength" in s["note"] for s in sug)


def test_formulation_marker_is_never_dropped(db):
    tab(db, "GLYCOMET 500 TAB")
    p = draft(db, ",GLYCOMET SR 500 TAB,10S,B1,May-2028,1,,50,90,50")
    assert p.items[0].item_id is None


def test_conflicting_form_does_not_match(db):
    tab(db, "CROCIN 120")
    p = draft(db, ",CROCIN 120 SYP,60ML,B1,May-2028,1,,50,90,50")
    assert p.items[0].item_id is None


def test_two_candidates_with_the_same_key_are_left_to_a_person(db):
    tab(db, "ALPHA-5 TAB")
    tab(db, "ALPHA 5 TABLET")
    p = draft(db, ",ALPHA 5 TABS,10S,B1,May-2028,1,,50,90,50")
    assert p.items[0].item_id is None


def test_explain_gives_signals_and_caps_strength_mismatch(db):
    right = tab(db, "ROSUBEST 10 TAB")
    wrong = tab(db, "ROSUBEST 20 TAB")
    sup = supplier(db)
    p = draft(db, ",ROSUBEST 10 TABLET,10S,B1,May-2028,1,,50,90,50", sup=sup)
    line = p.items[0]
    good = pm.explain(db, line, right, alias=True)
    bad = pm.explain(db, line, wrong)
    assert good["signals"]["strength"] == 1.0 and good["signals"]["dosageForm"] == 1.0 and good["score"] > 0.9
    assert "supplier alias found" in good["reasons"]
    assert bad["signals"]["strength"] == 0.0 and bad["capped"] and bad["score"] <= 0.6


def test_price_alone_never_rejects_a_candidate(db):
    item = tab(db, "ROSUBEST 10 TAB")
    item.mrp = 300
    p = draft(db, ",ROSUBEST 10 TABLET,10S,B1,May-2028,1,,50,90,50")
    out = pm.explain(db, p.items[0], item)
    assert out["signals"]["mrp"] < 0.85 and not out["capped"]


def test_match_inspector_api(client, db):
    login(client)
    tab(db, "ROSUBEST-10 TABLET")
    tab(db, "ROSUBEST-20 TABLET")
    p = draft(db, ",ROSUBEST 10 TAB,10S,B1,May-2028,1,,50,90,50")
    db.commit()
    r = client.get(f"/api/erp/purchases/{p.id}/lines/{p.items[0].id}/inspect")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["matched"]["method"] == "NORMALIZED_NAME" and d["matched"]["signals"]["strength"] == 1.0
    assert d["packaging"]["confidence"] == "HIGH" and d["gate"]["state"] in ("AUTO_ACCEPT", "AUTO_ACCEPT_WITH_WARNING")
    assert all(c["signals"]["strength"] == 0.0 for c in d["candidates"])
