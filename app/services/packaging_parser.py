"""Supplier packing text → structured packaging (purchase → retail → base, plus content).

Built as separate stages, each small and testable, instead of one large expression::

    normalize → tokenize → detect_measurement → detect_hierarchy → detect_dosage_form
      → assign_units_to_levels → calculate_conversions → score_confidence → validate

Pattern handlers recognise the shapes (``10X1X10``, ``20X10``, ``60ML``, ``1PCS``, ``VIAL``);
a new shape is a new handler, not an edit to an old one. Stock quantity and content stay
separate: ``OMNIGEL 20GM`` is one tube holding 20 g, never 20 stock units.

The parser never guesses silently. Anything it cannot read with certainty comes back as
LOW or UNRESOLVED with the reason, and the receipt decision sends it to review.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

HIGH, MEDIUM, LOW, UNRESOLVED = "HIGH", "MEDIUM", "LOW", "UNRESOLVED"
RANK = {HIGH: 3, MEDIUM: 2, LOW: 1, UNRESOLVED: 0}
MAX_BASE_PER_OUTER = 10000          # larger multipliers are flagged (unexpected for a pharmacy pack)

SOLID = {"TABLET", "CAPSULE", "SOFTGEL", "ROTACAP", "SUPPOSITORY"}
LIQUID = {"SYRUP", "SUSPENSION", "DROPS", "LOTION", "SPRAY", "INJECTION", "VIAL", "AMPOULE", "IV_FLUID", "RESPULE"}
TOPICAL = {"CREAM", "GEL", "OINTMENT"}
PIECES = {"DEVICE", "SYRINGE", "NEEDLE", "CANNULA", "INHALER", "SOAP", "BANDAGE", "TEST_STRIP", "KIT", "PAIR"}
# what one retail container of a form is called, and the unit stock is counted in
CONTAINER = {"SYRUP": "BOTTLE", "SUSPENSION": "BOTTLE", "DROPS": "BOTTLE", "LOTION": "BOTTLE", "SPRAY": "BOTTLE",
             "IV_FLUID": "BOTTLE", "INJECTION": "VIAL", "VIAL": "VIAL", "AMPOULE": "AMPOULE", "RESPULE": "PIECE",
             "CREAM": "TUBE", "GEL": "TUBE", "OINTMENT": "TUBE", "POWDER": "PACK", "SACHET": "SACHET",
             "SOAP": "PIECE", "INHALER": "PIECE", "DEVICE": "PIECE", "SYRINGE": "PIECE", "NEEDLE": "PIECE",
             "CANNULA": "PIECE", "BANDAGE": "PIECE", "TEST_STRIP": "PIECE", "KIT": "KIT", "PAIR": "PAIR"}
_CONTENT = {"ML": "ML", "MLS": "ML", "MILLILITRE": "ML", "L": "L", "LTR": "L", "LITRE": "L", "G": "GRAM", "GM": "GRAM",
            "GMS": "GRAM", "GRM": "GRAM", "GRMS": "GRAM", "GRAM": "GRAM", "GRAMS": "GRAM", "KG": "KG", "MG": "MG", "MCG": "MCG"}
_PIECE_WORDS = {"PCS": "PIECE", "PC": "PIECE", "PIECE": "PIECE", "PIECES": "PIECE", "NOS": "PIECE", "NO": "PIECE",
                "N": "PIECE", "EA": "PIECE", "EACH": "PIECE", "UNIT": "PIECE", "UNITS": "PIECE"}
_COUNT_MARK = {"S": "", "T": "TABLET", "TAB": "TABLET", "TABS": "TABLET", "C": "CAPSULE", "CAP": "CAPSULE", "CAPS": "CAPSULE"}
_NAMED = {"VIAL": "VIAL", "VAIL": "VIAL", "AMP": "AMPOULE", "AMPOULE": "AMPOULE", "AMPULE": "AMPOULE", "TUBE": "TUBE",
          "BOTTLE": "BOTTLE", "BTL": "BOTTLE", "JAR": "JAR", "SACHET": "SACHET", "BOX": "BOX", "STRIP": "STRIP",
          "KIT": "KIT", "PAIR": "PAIR", "BAG": "BAG"}
# dosage form words in descriptions (order matters: the first hit wins)
_FORM_WORDS: tuple[tuple[str, str], ...] = (
    ("SOFTGEL", r"\b(SG\s*CAP|SOFTGELS?|SOFT\s*GEL(?:ATIN)?\s*CAP\w*)\b"),
    ("GEL", r"\b[A-Z]{3,}GEL\b"),                     # brand names ending in -GEL (OMNIGEL)
    ("ROTACAP", r"\b(ROTACAPS?|TRANSCAPS?|INHALATION\s+CAP\w*)\b"),
    ("SUPPOSITORY", r"\b(SUPP|SUPPOSITOR\w*)\b"),
    ("TABLET", r"\b(TAB|TABS|TABLETS?|LOZ\w*|CHEWABLE)\b"),
    ("CAPSULE", r"(?<!KNEE )(?<!ANKLE )(?<!ELBOW )\b(CAP|CAPS|CAPSULES?)\b"),
    ("IV_FLUID", r"\b(IV\s*FLUID|INFUSION|NS\s*\d|DNS|RINGER\w*|DEXTROSE\s+\d)\b"),
    ("AMPOULE", r"\b(AMP|AMPOULES?|AMPULES?)\b"),
    ("INJECTION", r"\b(INJ|INJECTION|VIAL|VAIL)\b"),
    ("CANNULA", r"\b(CANNULA|CANULA|CANNULAE|IV\s*SET)\b"),
    ("SYRINGE", r"\b(SYRINGES?|SYR\s*\d+\s*ML)\b"),
    ("NEEDLE", r"\b(NEEDLES?)\b"),
    ("TEST_STRIP", r"\b(TEST\s*STRIPS?|GLUCO\w*\s+STRIPS?)\b"),
    ("SYRUP", r"\b(SYP|SYR|SYRUP|ELIXIR|TONIC|LIQUID|ORAL\s+SOL\w*)\b"),
    ("SUSPENSION", r"\b(SUSP|SUSPENSION)\b"),
    ("DROPS", r"\b(DROPS?|DRP|E/D|EYE|EAR|NASAL)\b"),
    ("CREAM", r"\b(CREAM|CRM)\b"),
    ("OINTMENT", r"\b(OINT|OINTMENT|OIT)\b"),
    ("GEL", r"\bGEL\b"),
    ("LOTION", r"\b(LOTION|SHAMPOO|WASH|MOUTHWASH|GARGLE)\b"),
    ("SPRAY", r"\b(SPRAY)\b"),
    ("INHALER", r"\b(INHALER|MDI|RESPULES?)\b"),
    ("SACHET", r"\b(SACHETS?|SAC)\b"),
    ("POWDER", r"\b(POWDER|PWD|GRANULES)\b"),
    ("SOAP", r"\b(SOAP)\b"),
    ("BANDAGE", r"\b(BANDAGE|DRESSING|GAUZE|PLASTER|CREPE)\b"),
    ("DEVICE", r"\b(SWABS?|GLOVES?|MASKS?|THERMOMETER|LANCETS?|CATHETER|URINE\s+BAG|DIAPERS?|FIXATOR|BELT|SUPPORT|BRACE|COLLAR)\b"),
    ("KIT", r"\bKIT\b"),
)


@dataclass
class Level:
    quantity: int
    unit: str

    def as_dict(self) -> dict:
        return {"quantity": self.quantity, "unit": self.unit}


@dataclass
class Packaging:
    """The structured reading. ``purchase``/``retail``/``base`` are per ONE purchase pack."""
    raw: str
    normalized: str = ""
    handler: str = ""
    dosage_form: str = ""
    form_source: str = ""
    levels: list[Level] = field(default_factory=list)
    purchase: Level | None = None
    retail: Level | None = None           # retail packs in one purchase pack
    base: Level | None = None             # base (stock) units in one purchase pack
    retail_to_base: int | None = None     # base units in one retail pack
    content: tuple[Decimal, str] | None = None   # what one container holds (60 ML, 20 GRAM)
    confidence: str = UNRESOLVED
    issues: list[str] = field(default_factory=list)
    ambiguous_count: bool = False         # the count itself has two readings (10X1, a bare 1, "18 NO")

    @property
    def base_per_outer(self) -> int | None:
        return self.base.quantity if self.base else None

    def as_dict(self) -> dict:
        d = {k: v for k, v in asdict(self).items() if k not in ("levels", "purchase", "retail", "base", "content")}
        d.update(levels=[l.as_dict() for l in self.levels], purchase=self.purchase and self.purchase.as_dict(),
                 retail=self.retail and self.retail.as_dict(), base=self.base and self.base.as_dict(),
                 base_per_outer=self.base_per_outer,
                 content={"quantity": format(self.content[0].normalize(), "f"), "unit": self.content[1]} if self.content else None)
        return d


# --------------------------------------------------------------------------- stage 1: normalize
def normalize(raw: Any) -> str:
    """``10 x 1 x 10's`` → ``10X1X10S``; ``75 gms`` → ``75GMS``. The raw text is kept elsewhere."""
    text = str(raw if raw is not None else "").upper().strip()
    text = text.replace("×", "X").replace("’", "").replace("'", "").replace("`", "")
    text = re.sub(r"(?<=\d)\s*\*\s*(?=\d)", "X", text)                  # 10*1*10
    text = re.sub(r"(?<=\d)\s*X\s*(?=\d)", "X", text)                   # 10 X 1 X 10
    text = re.sub(r"(?<=\d)\s+(?=[A-Z])", "", text)                     # 15 ML, 10 S
    text = re.sub(r"\s+", " ", text).strip().rstrip(".")
    return text


# --------------------------------------------------------------------------- stage 2: tokenize
_TOKEN = re.compile(r"\d+(?:\.\d+)?|[A-Z]+|X|[+\-/]")


def tokenize(text: str) -> list[str]:
    return _TOKEN.findall(text)


# --------------------------------------------------------------------------- stage 3: measurement
def detect_measurement(text: str) -> tuple[Decimal, str] | None:
    """A trailing content measurement: ``60ML``, ``1X100ML``, ``75GMS`` → (60, ML)."""
    m = re.fullmatch(r"(?:\d+X)*(\d+(?:\.\d+)?)(ML|MLS|L|LTR|G|GM|GMS|GRM|GRMS|GRAM|GRAMS|KG|MG|MCG)", text.replace(" ", ""))
    if not m:
        return None
    try:
        qty = Decimal(m.group(1))
    except InvalidOperation:
        return None
    return (qty, _CONTENT[m.group(2)]) if qty > 0 else None


# --------------------------------------------------------------------------- stage 4: hierarchy
def detect_hierarchy(text: str) -> list[int] | None:
    """The counts of a multi-level pack, outermost first: ``10X1X10S`` → [10, 1, 10]."""
    m = re.fullmatch(r"(\d+)(?:X(\d+))?(?:X(\d+))?(?:X(\d+))?(S|T|C|TAB|TABS|CAP|CAPS|PCS|PC|N|NOS)?", text.replace(" ", ""))
    if not m:
        return None
    return [int(g) for g in m.groups()[:4] if g is not None]


# --------------------------------------------------------------------------- stage 5: dosage form
def detect_dosage_form(*, master_form: str = "", description: str = "", product_name: str = "",
                       packing: str = "", confirmed_form: str = "") -> tuple[str, str]:
    """(form, source) in the order product master → description → name → packing → confirmed packaging."""
    if master_form:
        return master_form.upper(), "PRODUCT_MASTER"
    for text, source in ((description, "DESCRIPTION"), (product_name, "PRODUCT_NAME")):
        if text:
            padded = " " + re.sub(r"(\d)(?=(?:TABS?|CAPS?|TABLETS?|CAPSULES?)\b)", r"\1 ", str(text).upper()) + " "
            for form, pattern in _FORM_WORDS:
                if re.search(pattern, padded):
                    return form, source
    marker = re.search(r"\d(T|TAB|TABS|C|CAP|CAPS)$", normalize(packing))
    if marker:
        return ("TABLET" if marker.group(1).startswith("T") else "CAPSULE"), "PACKING"
    if confirmed_form:
        return confirmed_form.upper(), "CONFIRMED_PACKAGING"
    return "", ""


# --------------------------------------------------------------------------- handlers
class PackagePatternHandler:
    name = "Handler"

    def matches(self, text: str, ctx: dict) -> bool:
        raise NotImplementedError

    def parse(self, text: str, ctx: dict) -> Packaging:
        raise NotImplementedError


def _base_unit(form: str, hint: str = "") -> str:
    if hint in ("TABLET", "CAPSULE"):
        return hint
    if form in SOLID:
        return "CAPSULE" if form in ("CAPSULE", "SOFTGEL", "ROTACAP") else ("PIECE" if form == "SUPPOSITORY" else "TABLET")
    if form in PIECES or form in ("RESPULE",):
        return "PIECE"
    return CONTAINER.get(form, "")


class MultiLevelPackHandler(PackagePatternHandler):
    """``10X1X10``, ``5X2X15``, ``10X1X4``: outer × inner × count."""
    name = "MultiLevelPackHandler"

    def matches(self, text, ctx):
        h = detect_hierarchy(text)
        return bool(h and len(h) >= 3)

    def parse(self, text, ctx):
        counts = detect_hierarchy(text)
        p = Packaging(raw=ctx["raw"], normalized=text, handler=self.name)
        p.levels = [Level(counts[0], "INNER_BOX")] + [Level(c, "STRIP") for c in counts[1:-1]] + [Level(counts[-1], "")]
        retail = 1
        for c in counts[:-1]:
            retail *= c
        p.retail_to_base = counts[-1]
        p.retail = Level(retail, "STRIP")
        p.base = Level(retail * counts[-1], "")
        p.purchase = Level(1, "BOX")
        p.confidence = HIGH
        return p


class TwoLevelPackHandler(PackagePatternHandler):
    """``20X10``, ``10X10``, ``1X10``, ``1X14``, ``10X1``, ``1X100``."""
    name = "TwoLevelPackHandler"

    def matches(self, text, ctx):
        h = detect_hierarchy(text)
        return bool(h and len(h) == 2)

    def parse(self, text, ctx):
        outer, inner = detect_hierarchy(text)
        p = Packaging(raw=ctx["raw"], normalized=text, handler=self.name)
        if outer == 1:                                  # 1X10: one pack holding ten
            p.levels = [Level(1, "STRIP"), Level(inner, "")]
            p.purchase, p.retail, p.base, p.retail_to_base = Level(1, "STRIP"), Level(1, "STRIP"), Level(inner, ""), inner
            p.confidence = HIGH
        elif inner == 1:                                # 10X1: ten packs of one, or a reversed strip of ten
            p.levels = [Level(outer, "STRIP"), Level(1, "")]
            p.purchase, p.retail, p.base, p.retail_to_base = Level(1, "BOX"), Level(outer, "STRIP"), Level(outer, ""), 1
            p.confidence, p.ambiguous_count = LOW, True
            p.issues.append(f"{ctx['raw']!r} can mean {outer} packs of one or a strip of {outer}; confirm it once for this product")
        else:                                           # 20X10: a box of 20 strips of 10
            p.levels = [Level(outer, "STRIP"), Level(inner, "")]
            p.purchase, p.retail, p.base, p.retail_to_base = Level(1, "BOX"), Level(outer, "STRIP"), Level(outer * inner, ""), inner
            p.confidence = HIGH
        return p


class CountPackHandler(PackagePatternHandler):
    """``10``, ``10S``, ``10T``, ``15TABS``, ``1``."""
    name = "CountPackHandler"

    def matches(self, text, ctx):
        return bool(re.fullmatch(r"\d+(S|T|C|TAB|TABS|CAP|CAPS)?", text))

    def parse(self, text, ctx):
        m = re.fullmatch(r"(\d+)(S|T|C|TAB|TABS|CAP|CAPS)?", text)
        n, mark = int(m.group(1)), m.group(2) or ""
        p = Packaging(raw=ctx["raw"], normalized=text, handler=self.name)
        hint = _COUNT_MARK.get(mark, "")
        if hint:
            ctx.setdefault("unit_hint", hint)
        p.levels = [Level(n, hint)]
        p.purchase, p.retail, p.base, p.retail_to_base = Level(1, "STRIP"), Level(1, "STRIP"), Level(n, hint), n
        p.confidence = HIGH if n > 1 or mark else MEDIUM
        if n == 1 and not mark:
            p.purchase = p.retail = Level(1, "")
            ctx["bare_single"] = True
        return p


class LiquidContainerHandler(PackagePatternHandler):
    """``60ML``, ``1X100ML``, ``500ML``, ``5X5ML`` (five ampoules of 5 mL)."""
    name = "LiquidContainerHandler"

    def matches(self, text, ctx):
        m = detect_measurement(text)
        return bool(m and m[1] in ("ML", "L"))

    def parse(self, text, ctx):
        qty, unit = detect_measurement(text)
        outer = detect_hierarchy(re.sub(r"(?:ML|MLS|L|LTR)$", "", text))
        count = outer[0] if outer and len(outer) == 2 else 1
        p = Packaging(raw=ctx["raw"], normalized=text, handler=self.name, content=(qty, unit))
        p.levels = ([Level(count, "")] if count > 1 else []) + [Level(1, "")]
        p.purchase = Level(1, "BOX" if count > 1 else "")
        p.retail = Level(count, "")
        p.base = Level(count, "")
        p.retail_to_base = 1
        p.confidence = HIGH if count == 1 else MEDIUM
        if count > 1:
            p.ambiguous_count = True
            p.issues.append(f"{ctx['raw']!r} is {count} containers of {format(qty.normalize(), 'f')} mL; confirm whether one invoice Qty is the box or one container")
        return p


class WeightContainerHandler(PackagePatternHandler):
    """``5GM``, ``20GM``, ``75GMS``, ``1GM``: one container holding a weight."""
    name = "WeightContainerHandler"

    def matches(self, text, ctx):
        m = detect_measurement(text)
        return bool(m and m[1] in ("GRAM", "KG", "MG", "MCG"))

    def parse(self, text, ctx):
        qty, unit = detect_measurement(text)
        outer = detect_hierarchy(re.sub(r"[A-Z]+$", "", text))
        count = outer[0] if outer and len(outer) == 2 else 1
        p = Packaging(raw=ctx["raw"], normalized=text, handler=self.name, content=(qty, unit))
        p.levels = ([Level(count, "")] if count > 1 else []) + [Level(1, "")]
        p.purchase = Level(1, "BOX" if count > 1 else "")
        p.retail, p.base, p.retail_to_base = Level(count, ""), Level(count, ""), 1
        p.confidence = HIGH if count == 1 else MEDIUM
        if unit in ("MG", "MCG"):
            p.confidence = LOW
            p.issues.append(f"{ctx['raw']!r} looks like a strength, not a pack size")
        return p


class PiecePackHandler(PackagePatternHandler):
    """``1PCS``, ``10PCS``, ``1N``, ``2NOS``, ``PCS``."""
    name = "PiecePackHandler"

    def matches(self, text, ctx):
        return bool(re.fullmatch(r"(\d+)?(PCS|PC|PIECES?|NOS|NO|N|EA|EACH|UNITS?)", text))

    def parse(self, text, ctx):
        m = re.fullmatch(r"(\d+)?(PCS|PC|PIECES?|NOS|NO|N|EA|EACH|UNITS?)", text)
        n = int(m.group(1) or 1)
        p = Packaging(raw=ctx["raw"], normalized=text, handler=self.name)
        ctx.setdefault("unit_hint", "PIECE")
        p.levels = [Level(n, "PIECE")]
        p.purchase = Level(1, "PIECE" if n == 1 else "BOX")
        p.retail = Level(1 if n == 1 else n, "PIECE")
        p.base, p.retail_to_base = Level(n, "PIECE"), 1
        p.confidence = HIGH
        if n > 1 and m.group(2) in ("NO", "NOS", "N"):
            p.confidence, p.ambiguous_count = LOW, True   # "18 NO" is usually a size (gauge), not 18 pieces
            p.issues.append(f"{ctx['raw']!r} may be a size number rather than a count of pieces; confirm it once")
        return p


class VialHandler(PackagePatternHandler):
    """``VIAL``, ``VAIL``, ``1VIAL``, ``AMP``, ``TUBE``, ``BOTTLE``: one named container."""
    name = "VialHandler"

    def matches(self, text, ctx):
        return bool(re.fullmatch(r"(\d+)?([A-Z]+)", text)) and re.fullmatch(r"(\d+)?([A-Z]+)", text).group(2) in _NAMED

    def parse(self, text, ctx):
        m = re.fullmatch(r"(\d+)?([A-Z]+)", text)
        n, unit = int(m.group(1) or 1), _NAMED[m.group(2)]
        p = Packaging(raw=ctx["raw"], normalized=text.replace("VAIL", "VIAL"), handler=self.name)
        ctx.setdefault("unit_hint", unit)
        p.levels = [Level(n, unit)]
        p.purchase = Level(1, unit if n == 1 else "BOX")
        p.retail, p.base, p.retail_to_base = Level(n, unit), Level(n, unit), 1
        p.confidence = HIGH if n == 1 else MEDIUM
        return p


class MeteredDoseHandler(PackagePatternHandler):
    """``200MD``, ``120 MDI``, ``200MTS``, ``200 DOSES``: one inhaler or spray holding N metered doses."""
    name = "MeteredDoseHandler"
    _RE = re.compile(r"(\d+)(MD|MDS|MDI|MTS|DOSES?|PUFFS?|ACTUATIONS?)")

    def matches(self, text, ctx):
        return bool(self._RE.fullmatch(text.replace(" ", "")))

    def parse(self, text, ctx):
        n = int(self._RE.fullmatch(text.replace(" ", "")).group(1))
        p = Packaging(raw=ctx["raw"], normalized=text.replace(" ", ""), handler=self.name, content=(Decimal(n), "DOSE"))
        ctx.setdefault("unit_hint", "PIECE")
        ctx.setdefault("form_hint", "INHALER")
        p.levels = [Level(1, "PIECE")]
        p.purchase = p.retail = p.base = Level(1, "PIECE")
        p.retail_to_base = 1
        p.confidence = HIGH if n > 0 else UNRESOLVED
        return p


class UnknownPackHandler(PackagePatternHandler):
    name = "UnknownPackHandler"

    def matches(self, text, ctx):
        return True

    def parse(self, text, ctx):
        p = Packaging(raw=ctx["raw"], normalized=text, handler=self.name, confidence=UNRESOLVED)
        if text:
            p.issues.append(f"Packing {ctx['raw']!r} is not a known pack shape; confirm its conversion once")
        return p


HANDLERS: list[PackagePatternHandler] = [MultiLevelPackHandler(), TwoLevelPackHandler(), CountPackHandler(),
                                         LiquidContainerHandler(), WeightContainerHandler(), PiecePackHandler(),
                                         VialHandler(), MeteredDoseHandler(), UnknownPackHandler()]


# --------------------------------------------------------------------------- stages 6–9
def assign_units_to_levels(p: Packaging, form: str, ctx: dict) -> None:
    """The last level's unit comes from the dosage form: 10X1X10 is capsules for a capsule."""
    hint = ctx.get("unit_hint", "")
    base = _base_unit(form, hint if hint in ("TABLET", "CAPSULE") else "")
    container = CONTAINER.get(form, "")
    if hint and hint not in ("TABLET", "CAPSULE"):
        base = base or ("PIECE" if hint == "PIECE" else hint)
        container = container or hint
    if p.content and not base:
        unit = p.content[1]
        if unit in ("ML", "L"):
            base = container = "BOTTLE"
        elif form in ("", "CREAM", "GEL", "OINTMENT") and unit == "GRAM":
            base = container = ""                      # tube, sachet, jar or soap: the form decides
    if p.handler in ("LiquidContainerHandler", "WeightContainerHandler", "VialHandler", "MeteredDoseHandler"):
        unit = base or container
        for level in p.levels:
            level.unit = level.unit or unit
        if p.purchase and not p.purchase.unit:
            p.purchase.unit = unit
        for lv in (p.retail, p.base):
            if lv and not lv.unit:
                lv.unit = unit
        return
    retail_unit = "STRIP" if base in ("TABLET", "CAPSULE") else ("PIECE" if base == "PIECE" else base)
    if p.levels:
        p.levels[-1].unit = p.levels[-1].unit or base
        for level in p.levels[:-1]:
            if level.unit == "STRIP" and base and base not in ("TABLET", "CAPSULE"):
                level.unit = "PACK"
    if p.retail and p.retail.unit == "STRIP" and base and base not in ("TABLET", "CAPSULE"):
        p.retail.unit = retail_unit if p.handler != "TwoLevelPackHandler" or base != "PIECE" else "PIECE"
    if p.base:
        p.base.unit = p.base.unit or base
    if p.purchase and not p.purchase.unit:
        p.purchase.unit = base or "PACK"
    if base == "PIECE" and p.handler in ("TwoLevelPackHandler", "CountPackHandler") and p.base:
        # devices: 1X100 cannula = a box of 100 pieces, each piece sold on its own
        n = p.base.quantity
        p.purchase = Level(1, "BOX" if n > 1 else "PIECE")
        p.retail = Level(n, "PIECE")
        p.retail_to_base = 1


def calculate_conversions(p: Packaging) -> None:
    if p.retail and p.base and p.retail.quantity and p.retail_to_base is None:
        p.retail_to_base = p.base.quantity // p.retail.quantity if p.base.quantity % p.retail.quantity == 0 else None


def score_confidence(p: Packaging, form: str, ctx: dict) -> None:
    if p.confidence == UNRESOLVED:
        return
    base = p.base.unit if p.base else ""
    if not base:
        p.confidence = min(p.confidence, LOW, key=RANK.get)
        p.issues.append("The dosage form is not known, so the stock unit of this pack cannot be decided")
    if ctx.get("bare_single") and (form in SOLID or not form):
        p.confidence = min(p.confidence, LOW, key=RANK.get)
        # a bare 1 is a doubtful count only for forms that come in strips; for anything else it is one unit
        p.ambiguous_count = form in SOLID
        p.issues.append("A bare '1' does not say how many tablets or capsules one pack holds")
    if p.handler == "CountPackHandler" and not form and not ctx.get("unit_hint") and p.base and p.base.quantity > 1:
        p.confidence = min(p.confidence, MEDIUM, key=RANK.get)


def validate(p: Packaging, form: str) -> None:
    """Impossible pairs, zero quantities and implausible multipliers never pass unflagged."""
    levels = [l.quantity for l in p.levels]
    if any(q <= 0 for q in levels):
        p.confidence = UNRESOLVED
        p.issues.append("A pack level of zero or less is impossible")
        return
    base = p.base.unit if p.base else ""
    if form in LIQUID | TOPICAL | {"POWDER", "SACHET"} and base in ("TABLET", "CAPSULE"):
        p.confidence = UNRESOLVED
        p.issues.append(f"A {form.lower().replace('_', ' ')} cannot be counted in {base.lower()}s")
    if form in SOLID and p.content and p.content[1] in ("ML", "L"):
        p.confidence = UNRESOLVED
        p.issues.append(f"{form.title()}s are not measured in mL; the packing contradicts the product's form")
    if form in TOPICAL | LIQUID and p.retail and p.retail.unit == "STRIP":
        p.confidence = UNRESOLVED
        p.issues.append(f"A {form.lower()} does not come in strips")
    if (form in LIQUID | TOPICAL and p.handler in ("MultiLevelPackHandler", "TwoLevelPackHandler", "CountPackHandler")
            and p.base_per_outer and p.base_per_outer > 1):
        p.confidence = min(p.confidence, LOW, key=RANK.get)
        p.issues.append(f"A count pack ({p.raw!r}) for a {form.lower().replace('_', ' ')} is unusual; confirm how many containers one pack holds")
    if p.base_per_outer and p.base_per_outer > MAX_BASE_PER_OUTER:
        p.confidence = min(p.confidence, LOW, key=RANK.get)
        p.issues.append(f"{p.base_per_outer} units in one pack is unexpectedly large; check the packing")


def parse(raw: Any, *, master_form: str = "", description: str = "", product_name: str = "",
          confirmed_form: str = "") -> Packaging:
    """Read one supplier packing text in context. Never raises; never returns a zero conversion."""
    try:
        text = normalize(raw)
        ctx: dict = {"raw": str(raw if raw is not None else "")}
        if re.search(r"[+]|\d[A-Z]{2,}\d|\d[A-WYZ]\d", text):      # 5ML+, 10ML57: trailing noise or glued numbers
            p = Packaging(raw=ctx["raw"], normalized=text, handler="UnknownPackHandler", confidence=UNRESOLVED)
            p.issues.append(f"Packing {ctx['raw']!r} has extra characters; confirm what one pack contains")
            p.dosage_form, p.form_source = detect_dosage_form(master_form=master_form, description=description,
                                                              product_name=product_name, packing=text, confirmed_form=confirmed_form)
            return p
        handler = next(h for h in HANDLERS if h.matches(text, ctx))
        p = handler.parse(text, ctx)
        form, source = detect_dosage_form(master_form=master_form, description=description, product_name=product_name,
                                          packing=text, confirmed_form=confirmed_form)
        if not form and ctx.get("form_hint"):
            form, source = ctx["form_hint"], "PACKING"
        p.dosage_form, p.form_source = form, source
        if p.confidence != UNRESOLVED:
            assign_units_to_levels(p, form, ctx)
            calculate_conversions(p)
            score_confidence(p, form, ctx)
            validate(p, form)
        if p.confidence == UNRESOLVED or (p.base and p.base.quantity <= 0):
            p.purchase = p.retail = p.base = None
            p.retail_to_base = None
        return p
    except Exception as exc:                                         # the parser must never break an import
        p = Packaging(raw=str(raw), normalized="", handler="UnknownPackHandler", confidence=UNRESOLVED)
        p.issues.append(f"Packing could not be read ({type(exc).__name__})")
        return p


def same_package(a: Any, b: Any) -> bool:
    """``10x1x10`` = ``10 X 1 X 10`` = ``10*1*10`` = ``10x1x10S``."""
    pa, pb = parse(a), parse(b)
    if pa.confidence == UNRESOLVED or pb.confidence == UNRESOLVED:
        return normalize(a) == normalize(b)
    return [l.quantity for l in pa.levels] == [l.quantity for l in pb.levels] and pa.content == pb.content


def pack_key(raw: Any) -> str:
    """Stable key for a printed pack: the normalised text without a trailing strip marker."""
    text = normalize(raw).replace(" ", "")
    return re.sub(r"(?<=\d)S$", "", text)[:60]
