"""Packaging parser: every required format, malformed input, and property checks."""
from __future__ import annotations

from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app.services import packaging_parser as pp


def lv(level):
    return (level.quantity, level.unit) if level else None


@pytest.mark.parametrize("raw,desc,purchase,retail,base,content", [
    ("10", "PARACIP TAB", (1, "STRIP"), (1, "STRIP"), (10, "TABLET"), None),
    ("10S", "X TAB", (1, "STRIP"), (1, "STRIP"), (10, "TABLET"), None),
    ("10s", "X CAP", (1, "STRIP"), (1, "STRIP"), (10, "CAPSULE"), None),
    ("10T", "", (1, "STRIP"), (1, "STRIP"), (10, "TABLET"), None),
    ("1X10", "X TAB", (1, "STRIP"), (1, "STRIP"), (10, "TABLET"), None),
    ("1x10s", "X CAP", (1, "STRIP"), (1, "STRIP"), (10, "CAPSULE"), None),
    ("10X10", "X TAB", (1, "BOX"), (10, "STRIP"), (100, "TABLET"), None),
    ("20X10", "PARACIP-500 TAB", (1, "BOX"), (20, "STRIP"), (200, "TABLET"), None),
    ("10X1X10", "MULTIPREX CAP", (1, "BOX"), (10, "STRIP"), (100, "CAPSULE"), None),
    ("10X1X15", "X TAB", (1, "BOX"), (10, "STRIP"), (150, "TABLET"), None),
    ("10X1X4", "CALCIJOINT D3 SG CAP", (1, "BOX"), (10, "STRIP"), (40, "CAPSULE"), None),
    ("5X2X15", "X TAB", (1, "BOX"), (10, "STRIP"), (150, "TABLET"), None),
    ("1X14", "X TAB", (1, "STRIP"), (1, "STRIP"), (14, "TABLET"), None),
    ("1X5", "X TAB", (1, "STRIP"), (1, "STRIP"), (5, "TABLET"), None),
    ("1X4", "X CAP", (1, "STRIP"), (1, "STRIP"), (4, "CAPSULE"), None),
    ("60ML", "OKACET SYRUP", (1, "BOTTLE"), (1, "BOTTLE"), (1, "BOTTLE"), ("60", "ML")),
    ("100ML", "X SYP", (1, "BOTTLE"), (1, "BOTTLE"), (1, "BOTTLE"), ("100", "ML")),
    ("1X100ML", "X SUSP", (1, "BOTTLE"), (1, "BOTTLE"), (1, "BOTTLE"), ("100", "ML")),
    ("500ML", "NS IV FLUID", (1, "BOTTLE"), (1, "BOTTLE"), (1, "BOTTLE"), ("500", "ML")),
    ("15ML", "X EYE DROP", (1, "BOTTLE"), (1, "BOTTLE"), (1, "BOTTLE"), ("15", "ML")),
    ("5GM", "X CREAM", (1, "TUBE"), (1, "TUBE"), (1, "TUBE"), ("5", "GRAM")),
    ("20GM", "OMNIGEL", (1, "TUBE"), (1, "TUBE"), (1, "TUBE"), ("20", "GRAM")),
    ("75GMS", "X SOAP", (1, "PIECE"), (1, "PIECE"), (1, "PIECE"), ("75", "GRAM")),
    ("1GM", "X SACHET", (1, "SACHET"), (1, "SACHET"), (1, "SACHET"), ("1", "GRAM")),
    ("1PCS", "X MASK", (1, "PIECE"), (1, "PIECE"), (1, "PIECE"), None),
    ("VIAL", "X INJ", (1, "VIAL"), (1, "VIAL"), (1, "VIAL"), None),
    ("VAIL", "X INJ", (1, "VIAL"), (1, "VIAL"), (1, "VIAL"), None),
    ("1", "X GLOVES", (1, "PIECE"), (1, "PIECE"), (1, "PIECE"), None),
    ("1X100", "ACCUSURE CANNULA", (1, "BOX"), (100, "PIECE"), (100, "PIECE"), None),
])
def test_required_formats(raw, desc, purchase, retail, base, content):
    p = pp.parse(raw, description=desc)
    assert p.confidence in (pp.HIGH, pp.MEDIUM), (raw, p.issues)
    assert (lv(p.purchase), lv(p.retail), lv(p.base)) == (purchase, retail, base)
    assert (p.content and (format(p.content[0].normalize(), "f"), p.content[1])) == content or (content is None and p.content is None)
    assert p.raw == raw


def test_multilevel_hierarchy_matches_the_specification():
    p = pp.parse("10X1X10", description="MULTIPREX CAP")
    assert [(l.quantity, l.unit) for l in p.levels] == [(10, "INNER_BOX"), (1, "STRIP"), (10, "CAPSULE")]
    assert p.base_per_outer == 100 and p.confidence == pp.HIGH
    # the same text is tablets for a tablet product
    assert pp.parse("10X1X10", description="X TABLET").base.unit == "TABLET"


@pytest.mark.parametrize("a,b", [("10x1x10", "10 X 1 X 10"), ("10x1x10", "10*1*10"), ("10x1x10", "10x1x10S"), ("10X1X10", "10×1×10")])
def test_equivalent_spellings_are_the_same_package(a, b):
    assert pp.same_package(a, b)
    assert pp.pack_key(a) == pp.pack_key(b)


def test_different_packages_are_not_the_same():
    assert not pp.same_package("10x1x10", "10x10")
    assert not pp.same_package("60ML", "100ML")


@pytest.mark.parametrize("raw,desc", [("10ML57", "EXOTIC EAR DROP"), ("5ml+", "MOXIFORD E DROP"), ("", "X"), (None, ""),
                                       ("ABC", "X"), ("10X1", "BEGROEASE-50 TABLET"), ("1", "X TAB"), ("18 NO", "I V CANULA"),
                                       ("10X10", "X SYRUP"), ("500MG", "X TAB")])
def test_ambiguous_or_malformed_go_to_review(raw, desc):
    p = pp.parse(raw, description=desc)
    assert p.confidence in (pp.LOW, pp.UNRESOLVED), (raw, p.confidence)
    if raw:
        assert p.issues


def test_unknown_form_with_weight_does_not_invent_a_container():
    p = pp.parse("20GM", description="")
    assert p.confidence == pp.LOW and p.base.unit == ""


@pytest.mark.parametrize("raw,form,problem", [
    ("10X10", "SYRUP", "unusual"), ("60ML", "TABLET", "not measured in mL"), ("0X10", "TABLET", "zero"),
])
def test_validation_flags_impossible_combinations(raw, form, problem):
    p = pp.parse(raw, master_form=form)
    assert p.confidence in (pp.LOW, pp.UNRESOLVED)
    assert any(problem in i for i in p.issues)


def test_large_multiplier_is_flagged():
    p = pp.parse("100X100X10", description="X TAB")
    assert p.confidence == pp.LOW and any("unexpectedly large" in i for i in p.issues)


def test_master_form_wins_over_description():
    p = pp.parse("10X10", master_form="CAPSULE", description="SOMETHING TAB")
    assert p.base.unit == "CAPSULE" and p.form_source == "PRODUCT_MASTER"


def test_strength_markers_and_suffixes_do_not_change_the_pack():
    assert pp.parse("15S", description="DOLO 650 MG TAB").base.quantity == 15


@settings(max_examples=400, deadline=None)
@given(st.text(max_size=24))
def test_parser_never_raises_on_random_text(text):
    p = pp.parse(text, description=text)
    assert p.confidence in pp.RANK
    for level in (p.purchase, p.retail, p.base):
        assert level is None or level.quantity > 0
    if p.retail_to_base is not None:
        assert p.retail_to_base > 0


@settings(max_examples=300, deadline=None)
@given(st.integers(1, 60), st.integers(1, 20), st.integers(1, 30), st.sampled_from(["TAB", "CAP", ""]))
def test_hierarchy_conversions_are_exact(outer, mid, inner, word):
    p = pp.parse(f"{outer}X{mid}X{inner}", description=f"X {word}")
    if p.base is not None:
        assert p.base.quantity == outer * mid * inner
        assert p.retail.quantity * p.retail_to_base == p.base.quantity
        assert p.base.quantity > 0


@settings(max_examples=200, deadline=None)
@given(st.decimals(min_value=Decimal("0.5"), max_value=Decimal("5000"), places=1), st.sampled_from(["ML", "GM", "GMS"]))
def test_content_is_metadata_never_stock(qty, unit):
    p = pp.parse(f"{qty}{unit}", description="X SYRUP" if unit == "ML" else "X CREAM")
    if p.base is not None:
        assert p.base.quantity == 1
        assert p.content[0] == qty
