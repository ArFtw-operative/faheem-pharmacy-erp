"""Every shortcut a screen handles is registered (so it is listed in Keyboard shortcuts and can be
reassigned), and every registered key is one the browser lets a page use."""
from __future__ import annotations

import re
from pathlib import Path

from app.permissions import PERMISSION_CATALOG
from app.services import keymap_service as k

JS = Path(__file__).resolve().parents[1] / "app" / "static" / "erp"


def test_every_action_a_screen_uses_is_registered():
    ids = {a["id"] for a in k.ACTIONS}
    scopes = {i.split(".")[0] for i in ids}
    pattern = re.compile(r"""["']((?:%s)\.[A-Za-z]+)["']""" % "|".join(sorted(scopes)))
    used = {m.group(1) for f in JS.glob("*.js") for m in pattern.finditer(f.read_text())}
    unknown = sorted(used - ids - set(PERMISSION_CATALOG))      # permission codes share the dotted form
    assert not unknown, f"shortcut actions used by the screens but not registered: {unknown}"


def test_no_two_actions_share_a_key_where_both_apply():
    seen: dict[tuple, str] = {}
    for a in k.ACTIONS:
        if not a.get("key"):
            continue
        for scope in ({a["scope"]} if a["scope"] != "global" else {"global"}):
            key = (scope, a["key"])
            assert key not in seen, f'{a["id"]} and {seen[key]} both use {a["key"]}'
            seen[key] = a["id"]
    globals_ = {a["key"]: a["id"] for a in k.ACTIONS if a["scope"] == "global" and a.get("key")}
    clashes = [(a["id"], globals_[a["key"]]) for a in k.ACTIONS if a["scope"] != "global" and a.get("key") in globals_]
    assert not clashes, f"screen keys that hide a global key: {clashes}"


def test_registered_keys_are_usable_in_a_browser():
    blocked = [a["id"] for a in k.ACTIONS if a.get("key") in k.BLOCKED]
    assert not blocked, blocked


def test_tab_switcher_is_registered():
    a = next(a for a in k.ACTIONS if a["id"] == "app.tabSwitcher")
    assert a["scope"] == "global" and a["key"] == "Alt+Z"


def test_no_saved_personal_shortcut_hides_a_global_default(db):
    """A person's own key that equals a global default silently hides that default on their screen:
    new global defaults must avoid keys people already chose (checked against what is stored)."""
    from sqlalchemy import select

    from app.models import Setting

    globals_ = {a["key"]: a["id"] for a in k.ACTIONS if a["scope"] == "global" and a.get("key")}
    for row in db.scalars(select(Setting).where(Setting.key.like("keymap:user:%"))):
        import json

        for action, key in (json.loads(row.value or "{}") or {}).items():
            assert key not in globals_ or globals_[key] == action, f"{row.key}: {action} on {key} hides {globals_[key]}"
