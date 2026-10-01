"""Shortcut validation, atomic saves and per-operator isolation."""
import pytest
from types import SimpleNamespace
from app.services import keymap_service as keys
from tests.conftest import login


def test_defaults_and_scoped_collisions():
    assert keys.check({}) == {}
    assert keys.check({'pos.search': 'F3'}) == {}  # Inventory may use the same key.
    assert keys.check({'pos.search': 'F4'})
    assert keys.check({'app.lookup': 'F2'})
    assert keys.check({'pos.search': '', 'pos.qty': 'F2'}) == {}


@pytest.mark.parametrize('key', ['Ctrl+T', 'Ctrl+W', 'Ctrl+Tab', 'Ctrl+L', 'F11', 'F1',
    'Shift+F10', 'Alt+1', 'Ctrl+1', 'Alt+ArrowLeft', 'A', 'Shift+A', 'Enter', 'Bogus+Q',
    'Ctrl+Bogus', 'Ctrl+F99', 'Ctrl+Alt+Q', 'Ctrl++'])
def test_refuses_unsafe_or_invalid_keys(key):
    assert keys.problem(key)


def test_api_persistence_validation_and_reset(client):
    login(client)
    url = '/api/erp/keymap'
    assert client.put(url, json={'overrides': {'pos.search': 'alt+q'}}).json() == {'overrides': {'pos.search': 'Alt+Q'}}
    assert client.get(url).json()['overrides'] == {'pos.search': 'Alt+Q'}
    for overrides in ({'pos.search': 'Ctrl+W'}, {'pos.search': 'F4'}, {'missing': 'Alt+Q'}, {'pos.search': 42}, [], None):
        assert client.put(url, json={'overrides': overrides}).status_code == 400
        assert client.get(url).json()['overrides'] == {'pos.search': 'Alt+Q'}
    assert client.put(url, json=[]).status_code == 400
    assert client.put(url, content='{', headers={'Content-Type': 'application/json'}).status_code == 400
    assert client.put(url, json={'overrides': {}}).json() == {'overrides': {}}


def test_per_operator_storage(db):
    a = SimpleNamespace(id=1001, username='operator-a')
    b = SimpleNamespace(id=1002, username='operator-b')
    keys.save(db, a, {'pos.search': 'Alt+Q'})
    assert keys.overrides_for(db, b) == {}
    assert keys.overrides_for(db, a) == {'pos.search': 'Alt+Q'}


def test_reset_cannot_introduce_collision(client):
    login(client)
    url = '/api/erp/keymap'
    assert client.put(url, json={'overrides': {'pos.search': '', 'pos.qty': 'F2'}}).status_code == 200
    assert client.put(url, json={'overrides': {'pos.qty': 'F2'}}).status_code == 400


def test_purchase_screens_are_in_the_shortcut_map(client):
    login(client)
    actions = {a["id"]: a for a in client.get("/api/erp/keymap").json()["actions"]}
    for aid in ("purchases.import", "purchases.manual", "purchases.panel", "purchases.supplier", "purchases.return",
                "purchase.header", "purchase.add", "purchase.product", "purchase.newProduct", "purchase.accept",
                "purchase.next", "purchase.post", "purchase.source", "purchase.cancel"):
        assert aid in actions and actions[aid]["key"] and not keys.problem(actions[aid]["key"]), aid
