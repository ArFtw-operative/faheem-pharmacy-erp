"""The regression corpus runs in the build: zero wrong auto-accepts, measured straight-through.

A change to the purchase system that lets one wrong line through fails the build. The
straight-through floors guard against regressions; the real numbers are printed by
``python scripts/purchase_corpus.py`` and reported in CORPUS_REPORT.md.
"""
from __future__ import annotations

import pytest

from app.services import ocr
from tests import purchase_corpus as corpus


@pytest.fixture
def results(db):
    out = corpus.run(db)
    db.commit()
    return out


def test_corpus_has_zero_wrong_auto_accepts(results):
    wrong = [(r.invoice, w) for r in results for w in r.wrong]
    assert wrong == []


def test_recurring_structured_invoices_pass_straight_through(results):
    s = corpus.summarize(results)
    assert s["STRUCTURED"]["known_straight_through"] >= 98.0


def test_recurring_text_pdf_invoices_pass_straight_through(results):
    assert corpus.summarize(results)["PDF_TEXT"]["known_straight_through"] >= 95.0


@pytest.mark.skipif(not ocr.available(), reason="Tesseract is not installed")
def test_recurring_clear_scans_pass_straight_through(results):
    assert corpus.summarize(results)["OCR"]["known_straight_through"] >= 90.0


def test_traps_never_pass(results):
    a2 = next(r for r in results if r.invoice == "NR03897")
    assert a2.review + a2.blocked >= 4            # strength, release marker, unreadable pack, unknown product
