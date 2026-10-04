"""Reference catalogue, kept separate from verified ERP product identity."""
import csv
import hashlib
import re
import sqlite3
from pathlib import Path
from rapidfuzz.fuzz import ratio


def normalize_name(text):
    text = text.lower()
    for pattern, replacement in [(r"\btabs?\b|\btablets\b", "tablet"), (r"\bcaps?\b|\bcapsules\b", "capsule"), (r"\bsyp\b", "syrup"), (r"\binj\b", "injection")]:
        text = re.sub(pattern, replacement, text)
    # Do not remove strengths, release markers (SR/ER/CR), or numerical punctuation.
    text = re.sub(r"(?<=\d)\s+(?=mg\b|mcg\b|ml\b)", "", text)
    return re.sub(r"[^a-z0-9./]+", " ", text).strip()


class Catalog:
    def __init__(self, path, *, initialize=True):
        self.path = str(path)
        if not initialize:
            return
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS medicines(
                    id TEXT PRIMARY KEY, name TEXT, normalized TEXT, manufacturer TEXT,
                    pack TEXT, discontinued INTEGER, source TEXT, source_row INTEGER);
                CREATE INDEX IF NOT EXISTS catalog_name ON medicines(normalized);
            ''')
            columns = {r[1] for r in db.execute("PRAGMA table_info(medicines)")}
            if "price" not in columns:          # reference MRP of the listed pack (older catalogues had none)
                db.execute("ALTER TABLE medicines ADD COLUMN price REAL")

    def connect(self):
        return sqlite3.connect(self.path, timeout=30)

    @staticmethod
    def _rows(path):
        """(row number, {column: value}) from a CSV or XLSX catalogue; column names normalised
        (``price(₹)`` → ``price``)."""
        def key(h):
            k = re.sub(r"[^a-z_]", "", str(h or "").strip().lower().replace(" ", "_"))
            return "price" if k.startswith("price") else k
        if path.suffix.lower() in (".xlsx", ".xlsm"):
            import openpyxl

            wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
            rows = wb.active.iter_rows(values_only=True)
            head = [key(h) for h in next(rows)]
            for n, values in enumerate(rows, 2):
                yield n, {h: ("" if v is None else v) for h, v in zip(head, values)}
            wb.close()
            return
        with path.open(encoding="utf-8-sig", newline="") as stream:
            reader = csv.reader(stream)
            head = [key(h) for h in next(reader)]
            for n, values in enumerate(reader, 2):
                yield n, dict(zip(head, values))

    def ingest(self, path):
        path = Path(path)
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        count = 0
        rows = self._rows(path)
        first = next(rows, None)
        if first is None or not {"name", "manufacturer_name", "pack_size_label"} <= set(first[1]):
            raise ValueError("Catalogue requires name, manufacturer_name, pack_size_label")
        with self.connect() as db:
            # Replace this source atomically so a previous, active version cannot
            # override a newer discontinued / changed-pack record.
            db.execute("DELETE FROM medicines WHERE source=?", (path.name,))
            for n, row in [first, *rows]:
                if not str(row.get("name") or "").strip():
                    continue
                identity = hashlib.sha256(f"{digest}:{n}".encode()).hexdigest()[:32]
                try:
                    price = float(row.get("price")) if str(row.get("price", "")).strip() else None
                except ValueError:
                    price = None
                db.execute("INSERT INTO medicines (id, name, normalized, manufacturer, pack, discontinued, source, source_row, price) "
                           "VALUES(?,?,?,?,?,?,?,?,?)", (
                    identity, str(row["name"]), normalize_name(str(row["name"])), str(row["manufacturer_name"]),
                    str(row["pack_size_label"]), str(row.get("is_discontinued", "")).lower() == "true", path.name, n,
                    price if price and price > 0 else None))
                count += 1
        return {"source": path.name, "rows": count, "sha256": digest}

    def search(self, name, limit=5):
        normalized = normalize_name(name)
        if not normalized:
            return []
        with self.connect() as db:
            db.row_factory = sqlite3.Row
            # Read all exact-name variants before deduplication: repeated source
            # records must never hide a conflicting pack beyond a row limit.
            rows = db.execute("SELECT * FROM medicines WHERE normalized=?", (normalized,)).fetchall()
            exact = bool(rows)
            if not rows:
                prefix = re.sub(r"[^a-z0-9]", "", normalized.split()[0])[:8]
                if not prefix:
                    return []
                rows = db.execute("SELECT * FROM medicines WHERE normalized>=? AND normalized<? LIMIT 400", (prefix, prefix + "\uffff")).fetchall()
        candidates = []
        seen = set()
        for row in rows:
            item = dict(row)
            signature = (item["normalized"], item["manufacturer"].lower(), item["pack"].lower(), item["discontinued"])
            if signature in seen:
                continue
            seen.add(signature)
            item["similarity"] = round(ratio(normalized, item["normalized"]), 2)
            item["match"] = "exact_name_reference" if exact else "suggestion_only"
            candidates.append(item)
        return sorted(candidates, key=lambda x: (-x["similarity"], x["id"]))[:limit]


def candidates(name, limit=5):
    from app.config import DATA_DIR
    path = DATA_DIR / "medicine-reference.sqlite"
    if not path.is_file():
        return []
    return Catalog(path, initialize=False).search(name, min(max(limit, 1), 20))


def unique_pack_evidence(name, manufacturer=""):
    """Only exact-name references with one agreed packaging definition are usable evidence.

    Manufacturer abbreviations are not expanded speculatively. Fuzzy candidates
    remain suggestions; discontinued entries cannot establish a current pack.
    """
    from app.services import units
    rows = [r for r in candidates(name, 20) if r["match"] == "exact_name_reference"]
    if any(r["discontinued"] for r in rows) or len(rows) >= 20:
        return None  # conflict / truncated candidate set is not unique evidence
    if manufacturer:
        rows = [r for r in rows if normalize_name(r["manufacturer"]) == normalize_name(manufacturer)]
    identities = {(r["manufacturer"].casefold(), r["pack"].casefold()) for r in rows}
    if len(identities) != 1:
        return None
    row = rows[0]
    pack = units.parse_pack(row["pack"])
    if not pack.confident:
        return None
    return {"name": row["name"], "pack": row["pack"], "manufacturer": row["manufacturer"],
            "source": row["source"], "source_row": row["source_row"], "reference_id": row["id"]}


def identity_key(name):
    """Typographical equivalence only; keep strength, salts and release modifiers."""
    text = normalize_name(name)
    text = re.sub(r"(?<=\d)(mg)(?=\b)", "", text)
    text = re.sub(r"\b(tablet|capsule|syrup|injection)\b", "", text)
    text = re.sub(r"(?<=[a-z])(?=\d)", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def maker_compatible(printed, reference):
    def words(value):
        return [w for w in normalize_name(value).split() if w not in
                {"ltd", "limited", "pvt", "private", "healthcare", "laboratories", "laboratory", "pharmaceuticals", "pharma"}]
    a, b = words(printed), words(reference)
    return bool(a and b and len(a[0]) >= 4 and a[0] == b[0] and a == b[:len(a)])


def packaging_evidence(name, manufacturer="", printed_pack=""):
    """Read compatible form/retail packaging, never use a fuzzy drug identity.

    All matching reference variants are examined. Truncation, discontinued
    records, differing forms or counts return no evidence.
    """
    from app.config import DATA_DIR
    from app.models import Item
    from app.services import packaging_service, units
    path = DATA_DIR / "medicine-reference.sqlite"
    key = identity_key(name)
    prefix = key.split()[0] if key else ""
    if not path.is_file() or not prefix:
        return None
    # A hyphen is normalized to a space; the first word is an indexed range.
    with Catalog(path, initialize=False).connect() as db:
        db.row_factory = sqlite3.Row
        rows = db.execute("SELECT * FROM medicines WHERE normalized>=? AND normalized<? LIMIT 2001",
                          (prefix, prefix + "\uffff")).fetchall()
    if len(rows) > 2000:
        return None
    matches = [dict(r) for r in rows if identity_key(r["name"]) == key
               and (not manufacturer or maker_compatible(manufacturer, r["manufacturer"]))]
    evidence_kind = "exact_identity_pack"
    if not matches and manufacturer and units.parse_pack(printed_pack).kind in {"COUNT", "NESTED"}:
        # A supplier may omit secondary strengths from a combination name.
        # This can corroborate the FORM only, never select an existing medicine
        # or supply a missing strength. All branded modifiers and printed strength
        # components must agree, and every candidate must imply the same UOM.
        def components(text):
            value = identity_key(text)
            return re.findall(r"[a-z]+", value), re.findall(r"\d+(?:\.\d+)?", value)
        words, numbers = components(name)
        matches = [dict(r) for r in rows if components(r["name"])[0] == words
                   and components(r["name"])[1][:len(numbers)] == numbers
                   and maker_compatible(manufacturer, r["manufacturer"])]
        evidence_kind = "form_only_variant_consensus"
    if not matches or any(r["discontinued"] for r in matches):
        return None
    if evidence_kind == "form_only_variant_consensus":
        forms = {packaging_service.detect_form(Item(name=r["name"], generic_name="", dosage_form="")) for r in matches}
        if len(forms) != 1 or "" in forms:
            return None
    printed = units.parse_pack(printed_pack)
    expected_form = packaging_service.detect_form(Item(name=name, generic_name="", dosage_form=""))
    choices = []
    for row in matches:
        pack = units.parse_pack(row["pack"])
        probe = Item(name=row["name"], pack_size=row["pack"], generic_name="", dosage_form="")
        uom = packaging_service.resolve(probe)
        if not pack.confident or not uom or (expected_form and uom.dosage_form != expected_form):
            continue
        counts = {printed.units_per_pack}
        if printed.kind == "NESTED":
            counts.add(printed.units_per_pack * (printed.outer_count or 1))
        if printed.kind in ("COUNT", "NESTED") and uom.units_per_pack not in counts:
            continue
        if printed.kind == "CONTENT" and (printed.content_qty, printed.content_unit) != (pack.content_qty, pack.content_unit):
            continue
        choices.append((row, uom))
    definitions = {(u.base_unit, u.pack_unit, u.units_per_pack, u.dosage_form,
                    normalize_name(r["manufacturer"])) for r, u in choices}
    if len(definitions) != 1:
        return None
    row, uom = choices[0]
    return {"reference_id": row["id"], "name": row["name"], "manufacturer": row["manufacturer"],
            "pack": row["pack"], "source": row["source"], "source_row": row["source_row"], "uom": uom.as_dict(),
            "evidence_kind": evidence_kind}


def price_evidence(name, manufacturer=""):
    """The reference MRP of this medicine's listed retail pack: {count, unit, content, price, rows}.

    Exact identity only (strength and release markers kept); every priced record must agree on
    the pack, and their prices are summarised by the median. None when there is no such evidence.
    """
    from app.config import DATA_DIR
    from app.services import units
    path = DATA_DIR / "medicine-reference.sqlite"
    key = identity_key(name)
    prefix = key.split()[0] if key else ""
    if not path.is_file() or not prefix:
        return None
    with Catalog(path, initialize=False).connect() as db:
        db.row_factory = sqlite3.Row
        cols = {r[1] for r in db.execute("PRAGMA table_info(medicines)")}
        if "price" not in cols:
            return None
        rows = db.execute("SELECT name, manufacturer, pack, price, discontinued FROM medicines "
                          "WHERE normalized>=? AND normalized<? AND price IS NOT NULL LIMIT 2001",
                          (prefix, prefix + "\uffff")).fetchall()
    if len(rows) > 2000:
        return None
    from app.services import packaging_parser as pp

    family = lambda f: {"SOFTGEL": "CAPSULE", "ROTACAP": "CAPSULE", "SUSPENSION": "SYRUP", "AMPOULE": "INJECTION",
                        "VIAL": "INJECTION", "IV_FLUID": "INJECTION"}.get(f, f)
    want = family(pp.detect_dosage_form(description=name)[0])
    rows = [r for r in rows if identity_key(r["name"]) == key and not r["discontinued"]
            and (not manufacturer or maker_compatible(manufacturer, r["manufacturer"]))
            # the identity key drops form words, so the forms must agree (a syrup is not the tablet)
            and (family(pp.detect_dosage_form(description=f"{r['name']} {r['pack']}")[0]) == want if want
                 else not pp.detect_dosage_form(description=r["name"])[0])]
    packs = {}
    for r in rows:
        p = units.parse_pack(r["pack"])
        if not p.confident:
            continue
        hint = p.unit_hint or ("" if p.kind != "CONTENT" else p.content_unit)
        packs.setdefault((p.units_per_pack or 1, hint, str(p.content_qty or "")), []).append(float(r["price"]))
    if len(packs) != 1:
        return None
    (count, hint, content), prices = next(iter(packs.items()))
    prices.sort()
    return {"count": count, "unit": hint, "content": content, "price": prices[len(prices) // 2], "rows": len(prices)}
