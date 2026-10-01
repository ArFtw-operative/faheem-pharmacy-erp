"""Operator accounts: create, rename, reset password, enable/disable.

Used by ``scripts/manage.py``; there is no user-management screen in the ERP.
"""
from __future__ import annotations

import re
import secrets


from sqlalchemy.orm import Session

from app import audit
from app.security import find_user
from app.models import Role, User
from app.sequences import next_employee_id
from app.security import hash_password


class UserError(Exception):
    pass


def create_user(
    db: Session,
    *,
    username: str,
    full_name: str,
    role_id: int,
    password: str | None = None,
    phone: str = "",
    must_change_password: bool = True,
    actor: User | None = None,
    ip_address: str = "",
) -> tuple[User, str]:
    username = clean_username(username)
    if find_user(db, username):
        raise UserError(f"Username already exists: {username}")
    role = db.get(Role, role_id)
    if role is None:
        raise UserError("Role not found")
    temp_password = password or secrets.token_urlsafe(8)
    user = User(
        employee_id=next_employee_id(db),
        username=username,
        full_name=full_name or username,
        password_hash=hash_password(temp_password),
        role_id=role_id,
        phone=phone,
        must_change_password=must_change_password,
        created_by=actor.id if actor else None,
    )
    db.add(user)
    db.flush()
    audit.record(
        db,
        action=audit.A_CREATE,
        entity_type="user",
        entity_id=user.employee_id,
        user=actor,
        after={"username": user.username, "role": role.name, "employee_id": user.employee_id},
        details="User account created",
        ip_address=ip_address,
    )
    return user, temp_password


def reset_password(
    db: Session, user: User, *, new_password: str | None = None, actor: User | None = None,
    ip_address: str = "", force_change: bool = True,
) -> str:
    new_password = new_password or secrets.token_urlsafe(8)
    user.password_hash = hash_password(new_password)
    user.must_change_password = force_change
    db.flush()
    audit.record(
        db,
        action=audit.A_UPDATE,
        entity_type="user",
        entity_id=user.employee_id,
        user=actor,
        after={"password_reset": True, "username": user.username},
        details="Password reset by admin",
        ip_address=ip_address,
    )
    return new_password


def set_active(db: Session, user: User, active: bool, *, actor: User | None = None, ip_address: str = "") -> None:
    if user.id == (actor.id if actor else None) and not active:
        raise UserError("You cannot disable your own account")
    user.is_active = active
    db.flush()
    audit.record(
        db,
        action=audit.A_UPDATE,
        entity_type="user",
        entity_id=user.employee_id,
        user=actor,
        after={"is_active": active},
        details="Account " + ("enabled" if active else "disabled"),
        ip_address=ip_address,
    )


USERNAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{1,59}$")


def clean_username(value: str | None) -> str:
    """Trim and collapse spaces; letters, numbers, spaces and . _ - only, 2–60 characters."""
    name = " ".join(str(value or "").split())
    if not name:
        raise UserError("Username is required")
    if not USERNAME_RE.match(name):
        raise UserError("Username must be 2–60 characters: letters, numbers, spaces, dot, dash or underscore")
    return name


def update_user(
    db: Session, user: User, *, full_name=None, role_id=None, phone=None, username=None,
    actor: User | None = None, ip_address: str = "",
) -> None:
    before = {"username": user.username, "full_name": user.full_name, "role_id": user.role_id, "phone": user.phone}
    renamed = ""
    if username is not None:
        new_name = clean_username(username)
        if new_name != user.username:
            other = find_user(db, new_name)
            if other is not None and other.id != user.id:
                raise UserError(f"The username “{new_name}” is already taken by {other.full_name or other.username}")
            renamed = f"Username changed from “{user.username}” to “{new_name}”"
            user.username = new_name
    if full_name is not None:
        user.full_name = full_name
    if phone is not None:
        user.phone = phone
    if role_id is not None:
        role = db.get(Role, role_id)
        if role is None:
            raise UserError("Role not found")
        user.role_id = role_id
    db.flush()
    audit.record(
        db,
        action=audit.A_UPDATE,
        entity_type="user",
        entity_id=user.employee_id,
        user=actor,
        before=before,
        after={"username": user.username, "full_name": user.full_name, "role_id": user.role_id, "phone": user.phone},
        details=renamed or "User updated",
        ip_address=ip_address,
    )
