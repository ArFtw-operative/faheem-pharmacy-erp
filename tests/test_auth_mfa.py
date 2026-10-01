"""Login with first-login two-factor enrolment, password reset by authenticator code, lock-out."""
from __future__ import annotations

import re

from app.models import LoginSession, User
from app.seed import DEFAULT_ADMIN
from app.services import mfa, settings_service
from tests.conftest import login

U, P = DEFAULT_ADMIN["username"], DEFAULT_ADMIN["password"]


def _enrol(client):
    r = client.post("/login", data={"username": U, "password": P}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login/2fa/setup"
    page = client.get("/login/2fa/setup").text
    assert "otpauth" not in page and 'alt="QR code' in page                   # QR image, key shown grouped
    secret = re.search(r'class="secret">([A-Z2-7 ]+)<', page).group(1).replace(" ", "")
    return secret


def test_first_login_requires_scanning_the_qr_then_recovery_codes(client, db):
    secret = _enrol(client)
    assert client.get("/api/erp/whatsapp/status", follow_redirects=False).status_code in (303, 401)   # not in yet
    bad = client.post("/login/2fa/setup", data={"code": "000000"})
    assert bad.status_code == 400 and "not right" in bad.text
    ok = client.post("/login/2fa/setup", data={"code": mfa.code_at(secret, mfa.current_step())})
    codes = re.findall(r"<code>([A-Z2-9]{4}-[A-Z2-9]{4})</code>", ok.text)
    assert len(codes) == 10
    done = client.post("/login/2fa/done", follow_redirects=False)
    assert done.status_code == 303 and done.headers["location"] == "/app"
    user = db.query(User).filter(User.username == U).one()
    db.refresh(user)
    assert user.mfa_enabled and secret not in user.mfa_secret and mfa.unseal(user.mfa_secret) == secret   # sealed at rest
    assert all(c not in str(user.mfa_recovery) for c in codes)                                        # only hashes kept
    # next time: user ID + password straight in
    client.post("/logout")
    again = client.post("/login", data={"username": U, "password": P}, follow_redirects=False)
    assert again.headers["location"] == "/app"


def test_code_at_every_login_when_switched_on(client, db):
    login(client)
    client.post("/logout")
    settings_service.set_setting(db, "mfa_at_login", "true"); db.commit()
    r = client.post("/login", data={"username": U, "password": P}, follow_redirects=False)
    assert r.headers["location"] == "/login/2fa"
    user = db.query(User).filter(User.username == U).one()
    secret = mfa.unseal(user.mfa_secret)
    code = mfa.code_at(secret, mfa.current_step() + 1)          # the newest code (not used at enrolment)
    assert client.post("/login/2fa", data={"code": code}, follow_redirects=False).headers["location"] == "/app"


def test_password_reset_with_authenticator_code_ends_other_sessions(client, db):
    secret = _enrol(client)
    page = client.post("/login/2fa/setup", data={"code": mfa.code_at(secret, mfa.current_step())}).text
    recovery = re.findall(r"<code>([A-Z2-9]{4}-[A-Z2-9]{4})</code>", page)
    client.post("/login/2fa/done")
    user = db.query(User).filter(User.username == U).one()
    assert db.query(LoginSession).filter(LoginSession.user_id == user.id, LoginSession.logout_at.is_(None)).count() == 1
    newest = mfa.code_at(secret, mfa.current_step() + 1)
    weak = client.post("/login/reset", data={"username": U, "code": newest, "password": "short", "confirm": "short"})
    assert weak.status_code == 400 and "8 characters" in weak.text
    replay = client.post("/login/reset", data={"username": U, "code": newest, "password": "NewPass2026", "confirm": "NewPass2026"})
    assert replay.status_code == 400                                                       # a code works once
    ok = client.post("/login/reset", data={"username": U, "code": recovery[0], "password": "NewPass2026", "confirm": "NewPass2026"},
                     follow_redirects=False)
    assert ok.status_code == 303 and ok.headers["location"] == "/login?done=reset"
    assert db.query(LoginSession).filter(LoginSession.user_id == user.id, LoginSession.logout_at.is_(None)).count() == 0
    assert client.get("/api/erp/whatsapp/status", follow_redirects=False).status_code in (303, 401)   # the old session is over
    assert client.post("/login", data={"username": U, "password": P}).status_code == 401
    assert client.post("/login", data={"username": U, "password": "NewPass2026"}, follow_redirects=False).headers["location"] == "/app"


def test_recovery_code_resets_the_password_once(client, db):
    secret = _enrol(client)
    page = client.post("/login/2fa/setup", data={"code": mfa.code_at(secret, mfa.current_step())}).text
    code = re.findall(r"<code>([A-Z2-9]{4}-[A-Z2-9]{4})</code>", page)[0]
    client.post("/login/2fa/done")
    ok = client.post("/login/reset", data={"username": U, "code": code, "password": "Recover2026", "confirm": "Recover2026"},
                     follow_redirects=False)
    assert ok.status_code == 303
    again = client.post("/login/reset", data={"username": U, "code": code, "password": "Other2026x", "confirm": "Other2026x"})
    assert again.status_code == 400                                                       # used once


def test_wrong_passwords_lock_the_account_for_a_while(client, db):
    for _ in range(5):
        assert client.post("/login", data={"username": U, "password": "wrong"}).status_code == 401
    locked = client.post("/login", data={"username": U, "password": P})
    assert locked.status_code == 429 and "Too many wrong attempts" in locked.text
    unknown = client.post("/login/reset", data={"username": "nobody", "code": "123456", "password": "abc12345", "confirm": "abc12345"})
    assert unknown.status_code == 400 and "not right" in unknown.text                      # no hint whether the user exists


def test_network_sign_in_over_https_always_needs_the_code_and_secure_cookies(client, db):
    login(client)                                         # enrolled at the counter (http://127.0.0.1)
    client.post("/logout")
    client.cookies.clear()
    client.base_url = "https://testserver"                # the LAN / VPN proxy forwards as https
    r = client.post("/login", data={"username": U, "password": P}, follow_redirects=False)
    assert r.headers["location"] == "/login/2fa"          # password alone is not enough from the network
    assert "secure" in r.headers["set-cookie"].lower()
    user = db.query(User).filter(User.username == U).one()
    code = mfa.code_at(mfa.unseal(user.mfa_secret), mfa.current_step() + 1)
    done = client.post("/login/2fa", data={"code": code}, follow_redirects=False)
    assert done.headers["location"] == "/app" and "secure" in done.headers["set-cookie"].lower()
