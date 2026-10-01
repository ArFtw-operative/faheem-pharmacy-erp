"""Optional AI reader for supplier invoice layouts the mapper cannot read.

Off by default. When enabled, and only when :mod:`column_mapper` could not
find the core columns (product, qty, rate, MRP, batch, expiry), it asks a
Claude model what each column *means*. Only the header row and a few sample
rows are sent — never the whole file — and the answer is a column → field
proposal, not data: every proposed field must still pass the mapper's content
checks, and the draft notes that its layout came from the AI, so a person
confirms it during review like any other line.

Configuration (database settings, via ``scripts/manage.py setting set``):

    invoice_ai_enabled   1 to allow it (default 0)
    invoice_ai_key       Anthropic API key (or the ANTHROPIC_API_KEY environment variable)
    invoice_ai_model     model id (default claude-opus-5-5)
"""
from __future__ import annotations

import json
import logging
import os
import re

from sqlalchemy.orm import Session

log = logging.getLogger(__name__)
API_URL = "https://api.anthropic.com/v1/messages"
DEFAULT_MODEL = "claude-opus-5-5"
SAMPLE_ROWS = 8

FIELD_HELP = {
    "name": "product / item description", "supplier_code": "supplier's product code", "batch": "batch or lot number",
    "expiry": "expiry month-year", "quantity": "billed quantity (packs)", "free": "free / bonus quantity",
    "rate": "purchase rate per pack", "mrp": "maximum retail price per pack", "amount": "line value",
    "discount": "item discount %", "discount_amount": "item discount amount", "scheme": "scheme discount %",
    "gst": "GST rate %", "hsn": "HSN code", "pack": "pack size text (10S, 200ML)", "manufacturer": "manufacturer / company",
    "barcode": "barcode", "invoice_no": "invoice number (same on every line of one invoice)",
    "invoice_date": "invoice date", "net_total": "invoice net amount payable", "round_off": "round off",
    "freight": "freight", "adjust": "adjustment", "credit": "credit note amount", "debit": "debit note amount",
    "bill_discount": "whole-bill discount %", "supplier_name": "supplier name", "customer_name": "billed-to name",
}


def settings(db: Session) -> dict:
    from app.services import settings_service

    get = lambda k, d="": settings_service.get_setting(db, k, d) or d
    return {"enabled": str(get("invoice_ai_enabled", "0")).strip() in ("1", "true", "yes", "on"),
            "key": get("invoice_ai_key") or os.environ.get("ANTHROPIC_API_KEY", ""),
            "model": get("invoice_ai_model", DEFAULT_MODEL)}


def advisor(db: Session):
    """A callable (headers, rows) → {header: field} when the AI reader is enabled, else None."""
    cfg = settings(db)
    if not cfg["enabled"] or not cfg["key"]:
        return None
    return lambda headers, rows: propose(headers, rows, key=cfg["key"], model=cfg["model"])


def _prompt(headers: list[str], rows: list[list[object]]) -> str:
    sample = [[str(c if c is not None else "")[:40] for c in r] for r in rows[:SAMPLE_ROWS]]
    fields = "\n".join(f"- {k}: {v}" for k, v in FIELD_HELP.items())
    return (
        "You map the columns of an Indian pharmacy distributor's purchase invoice export to fields.\n"
        f"Allowed fields:\n{fields}\n- ignore: anything else (totals of taxes, codes we do not use, remarks)\n\n"
        "Rules: use each field at most once; never map a column whose values contradict the field; prefer "
        "item-level columns over invoice-level summaries for line fields; old/previous prices are ignore.\n"
        f"Headers: {json.dumps(headers)}\nSample rows: {json.dumps(sample)}\n\n"
        "Answer with only a JSON object mapping each header you can identify to a field name."
    )


def propose(headers: list[str], rows: list[list[object]], *, key: str, model: str = DEFAULT_MODEL,
            timeout: float = 25.0) -> dict[str, str]:
    import httpx

    try:
        res = httpx.post(API_URL, timeout=timeout, headers={
            "x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={"model": model, "max_tokens": 1500, "messages": [{"role": "user", "content": _prompt(headers, rows)}]})
        res.raise_for_status()
        text = "".join(b.get("text", "") for b in res.json().get("content", []) if b.get("type") == "text")
    except Exception as exc:     # the importer carries on without it
        log.warning("AI invoice reader unavailable: %s", exc)
        return {}
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return {}
    try:
        data = json.loads(m.group(0))
    except ValueError:
        return {}
    allowed = set(FIELD_HELP) | {"ignore"}
    out: dict[str, str] = {}
    for header, fld in data.items():
        if not (isinstance(header, str) and header in headers and fld in allowed):
            continue
        if fld == "ignore" or fld not in out.values():
            out[header] = fld
    return out
