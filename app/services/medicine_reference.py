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

    def connect(self):
        return sqlite3.connect(self.path, timeout=30)

    def ingest(self, path):
        path = Path(path)
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        count = 0
        with path.open(encoding="utf-8-sig", newline="") as stream, self.connect() as db:
            reader = csv.DictReader(stream)
            if not {"name", "manufacturer_name", "pack_size_label"} <= set(reader.fieldnames or []):
                raise ValueError("Catalogue requires name, manufacturer_name, pack_size_label")
            # Replace this source atomically so a previous, active version cannot
            # override a newer discontinued / changed-pack record.
            db.execute("DELETE FROM medicines WHERE source=?", (path.name,))
            for n, row in enumerate(reader, 2):
                identity = hashlib.sha256(f"{digest}:{n}".encode()).hexdigest()[:32]
                db.execute("INSERT INTO medicines VALUES(?,?,?,?,?,?,?,?)", (
                    identity, row["name"], normalize_name(row["name"]), row["manufacturer_name"],
                    row["pack_size_label"], row.get("is_discontinued", "").lower() == "true", path.name, n))
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
