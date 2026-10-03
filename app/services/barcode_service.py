"""BarcodeService: GTIN and GS1 element strings as scanners hand them to the ERP (text).

Understands the application identifiers purchase lines need — (01) GTIN, (10) batch,
(17) expiry YYMMDD, (21) serial — written with brackets, as raw data with FNC1/GS
separators, or a bare GTIN/EAN. Expiry day 00 means the last day of that month (GS1 rule);
the ERP stores the month, like every printed expiry.
"""
from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date

GS = "\x1d"
_FIXED = {"01": 14, "02": 14, "11": 6, "13": 6, "15": 6, "17": 6, "20": 2}
_VARIABLE = {"10": 20, "21": 20, "22": 20, "240": 30, "241": 30, "30": 8, "37": 8}


@dataclass
class Gs1:
    gtin: str = ""
    batch: str = ""
    expiry: date | None = None
    serial: str = ""
    raw: str = ""


def gtin_valid(code: str) -> bool:
    """Mod-10 check digit of GTIN-8/12/13/14."""
    if not re.fullmatch(r"\d{8}|\d{12,14}", code or ""):
        return False
    digits = [int(c) for c in code]
    body, check = digits[:-1], digits[-1]
    total = sum(d * (3 if i % 2 == 0 else 1) for i, d in enumerate(reversed(body)))
    return (10 - total % 10) % 10 == check


def _expiry(yymmdd: str) -> date | None:
    try:
        yy, mm, dd = int(yymmdd[:2]), int(yymmdd[2:4]), int(yymmdd[4:6])
        year = 2000 + yy if yy < 51 else 1900 + yy
        if not 1 <= mm <= 12:
            return None
        last = calendar.monthrange(year, mm)[1]
        return date(year, mm, last if dd == 0 else min(dd, last))
    except (ValueError, IndexError):
        return None


def parse(text: str) -> Gs1 | None:
    """None when the text is not a GTIN or a GS1 element string."""
    raw = str(text or "").strip()
    if not raw:
        return None
    if re.fullmatch(r"\d{8}|\d{12,14}", raw):
        return Gs1(gtin=raw.zfill(14), raw=raw) if gtin_valid(raw) else None
    out = Gs1(raw=raw)
    if "(" in raw:
        pairs = re.findall(r"\((\d{2,4})\)([^(]*)", raw)
    else:
        pairs, s = [], raw.lstrip("]C1").lstrip("]d2")
        while s:
            ai = next((a for a in sorted({**_FIXED, **_VARIABLE}, key=len, reverse=True) if s.startswith(a)), None)
            if ai is None:
                return None
            s = s[len(ai):]
            if ai in _FIXED:
                value, s = s[:_FIXED[ai]], s[_FIXED[ai]:]
            else:
                end = s.find(GS)
                value, s = (s, "") if end < 0 else (s[:end], s[end + 1:])
            pairs.append((ai, value))
            s = s.lstrip(GS)
    if not pairs:
        return None
    for ai, value in pairs:
        value = value.strip(GS).strip()
        if ai == "01" and gtin_valid(value):
            out.gtin = value
        elif ai == "10":
            out.batch = value[:20]
        elif ai == "17":
            out.expiry = _expiry(value)
        elif ai == "21":
            out.serial = value[:20]
    return out if (out.gtin or out.batch or out.expiry) else None


def gtin_keys(gtin: str) -> set[str]:
    """The forms one GTIN is stored in on product masters (14-digit, EAN-13, UPC-12)."""
    g = gtin.lstrip("0")
    return {gtin, gtin.zfill(14), g.zfill(13), g.zfill(12), g} - {""}
