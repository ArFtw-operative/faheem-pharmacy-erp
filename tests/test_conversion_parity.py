"""The browser's live-preview conversions (erp/core.js) agree with app/services/units.py.

The server decides every stored quantity and price; the browser mirror only previews
while someone types. This keeps the two from drifting apart.
"""
from __future__ import annotations

import json
import random
import shutil
import subprocess
from decimal import Decimal
from pathlib import Path

import pytest

from app.services import units

CORE = Path(__file__).resolve().parent.parent / "app" / "static" / "erp" / "core.js"
pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")


def _cases():
    rnd = random.Random(7)
    cases = []
    for _ in range(400):
        mrp = Decimal(rnd.randint(1, 99999)) / 100
        upp = rnd.choice([1, 1, 2, 4, 6, 7, 10, 14, 15, 30, 100])
        qty = rnd.randint(0, 3 * upp + 5)
        cases.append((str(mrp), upp, qty))
    return cases


def _node(script: str, tmp_path: Path):
    module = tmp_path / "core.mjs"            # the browser file is an ES module without a package.json
    module.write_text(CORE.read_text(encoding="utf-8"), encoding="utf-8")
    out = subprocess.run(["node", "--input-type=module", "-e", script.replace("@CORE@", json.dumps(module.as_uri()))],
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr[-2000:]
    return json.loads(out.stdout)


def test_line_amount_and_describe_match_the_server(tmp_path):
    cases = _cases()
    (tmp_path / "cases.json").write_text(json.dumps(cases))
    script = f"""
import {{ readFileSync }} from "node:fs";
globalThis.window = {{}}; globalThis.document = {{ addEventListener() {{}}, querySelector() {{ return null; }} }};
const core = await import(@CORE@);
const cases = JSON.parse(readFileSync({json.dumps(str(tmp_path / "cases.json"))}, "utf8"));
console.log(JSON.stringify(cases.map(([m, u, q]) => [core.lineAmount(m, u, q), core.describe(q, u, 'TABLET', 'STRIP'),
  core.toBase(q, u), core.parseQty(`${{q}}`, u), core.parseQty(`1s+${{q % 7}}`, u)])));
"""
    got = _node(script, tmp_path)
    for (mrp, upp, qty), (amount, text, base, parsed, pack_expr) in zip(cases, got):
        assert Decimal(str(amount)).quantize(Decimal("0.01")) == units.line_amount(mrp, upp, qty), (mrp, upp, qty)
        assert text == units.describe_stock(qty, upp, "TABLET", "STRIP")
        assert base == qty * upp
        assert parsed == units.parse_qty_expression(str(qty), upp)
        assert pack_expr == units.parse_qty_expression(f"1s+{qty % 7}", upp)
