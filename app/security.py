"""Password hashing and signed-cookie session handling."""
from __future__ import annotations

from datetime import datetime, timedelta

import bcrypt
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from app.config import SECRET_KEY

_serializer = URLSafeTimedSerializer(SECRET_KEY, salt="pharmacy-session")
SESSION_MAX_AGE = 60 * 60 * 12  # 12 hours


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    if not password_hash:
        return False
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except (ValueError, TypeError):
        return False


def make_session_token(user_id: int, session_id: int) -> str:
    return _serializer.dumps({"uid": user_id, "sid": session_id})


def read_session_token(token: str) -> dict | None:
    try:
        return _serializer.loads(token, max_age=SESSION_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None


def session_expiry() -> datetime:
    return datetime.now() + timedelta(seconds=SESSION_MAX_AGE)


def find_user(db, username: str | None):
    """User by username, ignoring letter case and extra spaces ("Front  desk" finds "Front Desk")."""
    from sqlalchemy import func, select

    from app.models import User

    name = " ".join(str(username or "").split()).lower()
    if not name:
        return None
    return db.scalar(select(User).where(func.lower(User.username) == name))
