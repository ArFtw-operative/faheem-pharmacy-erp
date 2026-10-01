"""Authentication: login, first-login two-factor enrolment, password reset by
authenticator code, logout. There is no sign-up: accounts are created by the
administrator (installer / scripts/manage.py).

    /login                 user ID + password (5 wrong tries → 15-minute lock)
    /login/2fa/setup       first login: scan the QR, confirm a code → recovery codes → in
    /login/2fa             a code at every login, when ``mfa_at_login`` is on
    /login/reset           forgot password: user ID + authenticator (or recovery) code + new password
"""
from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy import update
from sqlalchemy.orm import Session

from app import audit
from app.config import SECRET_KEY, SESSION_COOKIE
from app.database import get_db
from app.deps import client_ip, get_current_user, templates
from app.models import LoginSession, User
from app.routing import OffloadRoute
from app.security import find_user, hash_password, make_session_token, verify_password
from app.services import mfa, settings_service
from app.utils import utcnow

router = APIRouter(tags=["auth"], route_class=OffloadRoute)

PENDING_COOKIE = "pharmacy_mfa"
_pending = URLSafeTimedSerializer(SECRET_KEY, salt="pharmacy-mfa-pending")
MAX_FAILURES, LOCK_MINUTES = 5, 15


def _page(request: Request, db: Session, mode: str = "login", status: int = 200, **ctx):
    return templates.TemplateResponse(request, "login.html", {
        "app_name": "Faheem Pharmacy", "profile": settings_service.get_profile(db), "mode": mode,
        "next": ctx.pop("next", request.query_params.get("next", "/")), "error": None, "notice": None, **ctx,
    }, status_code=status)


def _safe_next(target: str | None) -> str:
    return target if target and target != "/" and target.startswith("/") and not target.startswith("//") else "/app"


def _locked(user: User) -> bool:
    return bool(user.locked_until and user.locked_until > utcnow())


def _fail(db: Session, user: User | None, username: str, ip: str, why: str) -> None:
    if user is not None:
        user.failed_logins = (user.failed_logins or 0) + 1
        if user.failed_logins >= MAX_FAILURES:
            user.locked_until, user.failed_logins = utcnow() + timedelta(minutes=LOCK_MINUTES), 0
    audit.record(db, action=audit.A_LOGIN_FAILED, entity_type="user", entity_id=user.id if user else username,
                 username=username, details=why, ip_address=ip, commit=True)


def _https(request: Request) -> bool:
    """Reached through the LAN proxy (HTTPS): cookies are then never sent over plain HTTP."""
    return request.url.scheme == "https"


def _set_pending(request: Request, response, user: User, stage: str, nxt: str) -> None:
    response.set_cookie(PENDING_COOKIE, _pending.dumps({"uid": user.id, "stage": stage, "next": nxt}),
                        httponly=True, samesite="strict", max_age=600, secure=_https(request))


def _get_pending(request: Request, db: Session, *stages: str) -> tuple[User | None, dict]:
    try:
        data = _pending.loads(request.cookies.get(PENDING_COOKIE, ""), max_age=600)
    except (BadSignature, SignatureExpired):
        return None, {}
    user = db.get(User, data.get("uid"))
    if user is None or not user.is_active or data.get("stage") not in stages:
        return None, {}
    return user, data


def _start_session(request: Request, db: Session, user: User, nxt: str):
    ip = client_ip(request)
    session = LoginSession(user_id=user.id, ip_address=ip, user_agent=request.headers.get("user-agent", "")[:255])
    db.add(session)
    user.last_login, user.failed_logins, user.locked_until = utcnow(), 0, None
    db.flush()
    audit.record(db, action=audit.A_LOGIN, entity_type="user", entity_id=user.id, user=user, details="Login", ip_address=ip)
    db.commit()
    response = RedirectResponse(url=_safe_next(nxt), status_code=303)
    response.set_cookie(SESSION_COOKIE, make_session_token(user.id, session.id), httponly=True, samesite="lax",
                        max_age=60 * 60 * 12, secure=_https(request))
    response.delete_cookie(PENDING_COOKIE)
    return response


# --------------------------------------------------------------------------- login
@router.get("/login")
def login_page(request: Request, db: Session = Depends(get_db)):
    if get_current_user(request, db) and not request.query_params.get("force"):
        return RedirectResponse("/", status_code=303)
    notice = {"reset": "Password changed. Log in with the new password."}.get(request.query_params.get("done", ""))
    return _page(request, db, notice=notice)


@router.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(...), next: str = Form("/"),
          db: Session = Depends(get_db)):
    user, ip = find_user(db, username), client_ip(request)
    if user is not None and _locked(user):
        mins = max(1, int((user.locked_until - utcnow()).total_seconds() // 60) + 1)
        return _page(request, db, error=f"Too many wrong attempts. Try again in {mins} minute{'s' if mins > 1 else ''}, "
                                         "or reset the password with your authenticator code.", status=429, next=next)
    if user is None or not verify_password(password, user.password_hash):
        _fail(db, user, username, ip, "Invalid credentials")
        return _page(request, db, error="Invalid user ID or password", status=401, next=next)
    if not user.is_active:
        return _page(request, db, error="This account is disabled", status=403, next=next)
    user.failed_logins = 0
    if not user.mfa_enabled:                                   # first login: set up the authenticator before anything else
        if not mfa.unseal(user.mfa_secret):
            user.mfa_secret = mfa.seal(mfa.new_secret())
        db.commit()
        response = RedirectResponse("/login/2fa/setup", status_code=303)
        _set_pending(request, response, user, "enroll", next)
        return response
    # from the network (LAN / store VPN, always via the HTTPS proxy) every sign-in needs the code
    if _https(request) or settings_service.get_setting(db, "mfa_at_login", "false") == "true":
        db.commit()
        response = RedirectResponse("/login/2fa", status_code=303)
        _set_pending(request, response, user, "verify", next)
        return response
    return _start_session(request, db, user, next)


# --------------------------------------------------------------------------- first-login enrolment
def _setup_page(request: Request, db: Session, user: User, error: str | None = None, status: int = 200):
    secret = mfa.unseal(user.mfa_secret)
    return _page(request, db, mode="setup", status=status, error=error, user_name=user.full_name or user.username,
                 qr=mfa.qr_data_uri(mfa.provisioning_uri(secret, user.username)),
                 secret_grouped=" ".join(secret[i:i + 4] for i in range(0, len(secret), 4)))


@router.get("/login/2fa/setup")
def setup_page(request: Request, db: Session = Depends(get_db)):
    user, _ = _get_pending(request, db, "enroll")
    if user is None:
        return RedirectResponse("/login", status_code=303)
    return _setup_page(request, db, user)


@router.post("/login/2fa/setup")
def setup_confirm(request: Request, code: str = Form(...), db: Session = Depends(get_db)):
    user, data = _get_pending(request, db, "enroll")
    if user is None:
        return RedirectResponse("/login", status_code=303)
    step = mfa.verify(mfa.unseal(user.mfa_secret), code)
    if step is None:
        return _setup_page(request, db, user, error="That code is not right. Check the phone's time is automatic and use the newest code.", status=400)
    codes = mfa.new_recovery_codes()
    user.mfa_enabled, user.mfa_enrolled_at, user.mfa_last_step = True, utcnow(), step
    user.mfa_recovery = [mfa.hash_recovery(c) for c in codes]
    audit.record(db, action=audit.A_UPDATE, entity_type="user", entity_id=user.id, user=user,
                 details="Two-factor authentication set up", ip_address=client_ip(request))
    db.commit()
    response = _page(request, db, mode="recovery", codes=codes, user_name=user.full_name or user.username)
    _set_pending(request, response, user, "enrolled", data.get("next", "/"))
    return response


@router.post("/login/2fa/done")
def setup_done(request: Request, db: Session = Depends(get_db)):
    user, data = _get_pending(request, db, "enrolled")
    if user is None:
        return RedirectResponse("/login", status_code=303)
    return _start_session(request, db, user, data.get("next", "/"))


# --------------------------------------------------------------------------- code at login (optional)
@router.get("/login/2fa")
def code_page(request: Request, db: Session = Depends(get_db)):
    user, _ = _get_pending(request, db, "verify")
    if user is None:
        return RedirectResponse("/login", status_code=303)
    return _page(request, db, mode="code", user_name=user.full_name or user.username)


@router.post("/login/2fa")
def code_check(request: Request, code: str = Form(...), db: Session = Depends(get_db)):
    user, data = _get_pending(request, db, "verify")
    if user is None:
        return RedirectResponse("/login", status_code=303)
    if not _second_factor(user, code):
        _fail(db, user, user.username, client_ip(request), "Wrong two-factor code")
        return _page(request, db, mode="code", user_name=user.full_name or user.username, error="That code is not right", status=401)
    return _start_session(request, db, user, data.get("next", "/"))


def _second_factor(user: User, code: str) -> bool:
    """An authenticator code (replay-guarded) or an unused recovery code (spent)."""
    step = mfa.verify(mfa.unseal(user.mfa_secret), code, last_step=user.mfa_last_step)
    if step is not None:
        user.mfa_last_step = step
        return True
    left = mfa.use_recovery(user.mfa_recovery, code)
    if left is not None:
        user.mfa_recovery = left
        return True
    return False


# --------------------------------------------------------------------------- forgot password
def password_problem(password: str, confirm: str) -> str:
    if password != confirm:
        return "The two passwords do not match"
    if len(password) < 8:
        return "Use at least 8 characters"
    if password.isdigit() or password.isalpha():
        return "Use letters and numbers"
    return ""


@router.get("/login/reset")
def reset_page(request: Request, db: Session = Depends(get_db)):
    return _page(request, db, mode="reset")


@router.post("/login/reset")
def reset_password(request: Request, username: str = Form(...), code: str = Form(...), password: str = Form(...),
                   confirm: str = Form(...), db: Session = Depends(get_db)):
    user, ip = find_user(db, username), client_ip(request)
    generic = "User ID or authenticator code is not right"
    if user is not None and _locked(user):
        return _page(request, db, mode="reset", error="Too many wrong attempts. Try again later.", status=429, username=username)
    if user is None or not user.is_active or not user.mfa_enabled:
        _fail(db, user, username, ip, "Password reset refused")
        return _page(request, db, mode="reset", error=generic if user is None or user.mfa_enabled else
                     "This account has no authenticator yet — ask the administrator to reset the password.", status=400, username=username)
    if not _second_factor(user, code):
        _fail(db, user, username, ip, "Password reset: wrong two-factor code")
        return _page(request, db, mode="reset", error=generic, status=400, username=username)
    why = password_problem(password, confirm)
    if why:
        db.commit()                                            # the code is spent either way
        return _page(request, db, mode="reset", error=why + " — enter a new authenticator code and try again.", status=400, username=username)
    user.password_hash, user.password_changed_at = hash_password(password), utcnow()
    user.must_change_password, user.failed_logins, user.locked_until = False, 0, None
    db.execute(update(LoginSession).where(LoginSession.user_id == user.id, LoginSession.logout_at.is_(None))
               .values(logout_at=utcnow()))                    # every other session ends
    audit.record(db, action=audit.A_UPDATE, entity_type="user", entity_id=user.id, user=user,
                 details="Password reset with two-factor code", ip_address=ip)
    db.commit()
    return RedirectResponse("/login?done=reset", status_code=303)


# --------------------------------------------------------------------------- logout
@router.post("/logout")
def logout(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    sid = getattr(request.state, "session_id", None)
    if sid:
        session = db.get(LoginSession, sid)
        if session and session.logout_at is None:
            session.logout_at = utcnow()
    if user:
        audit.record(db, action=audit.A_LOGOUT, entity_type="user", entity_id=user.id, user=user, details="Logout",
                     ip_address=client_ip(request))
    db.commit()
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE)
    return response
