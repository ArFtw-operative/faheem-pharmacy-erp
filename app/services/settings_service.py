"""Typed access to the Settings table plus QR code generation from coordinates."""
from __future__ import annotations

import base64
from io import BytesIO
from pathlib import Path

import qrcode
from sqlalchemy.orm import Session

from app import audit
from app.models import Setting, User
from app.utils import utcnow


def get_setting(db: Session, key: str, default: str = "") -> str:
    row = db.get(Setting, key)
    return row.value if row and row.value is not None else default


def get_int(db: Session, key: str, default: int = 0) -> int:
    try:
        return int(get_setting(db, key, str(default)))
    except (TypeError, ValueError):
        return default


def get_float(db: Session, key: str, default: float = 0.0) -> float:
    try:
        return float(get_setting(db, key, str(default)))
    except (TypeError, ValueError):
        return default


def get_profile(db: Session) -> dict[str, str]:
    keys = [
        "pharmacy_name",
        "tagline",
        "contact_numbers",
        "pharmacy_email",
        "pharmacy_website",
        "gst_number",
        "show_gst",
        "drug_license_number",
        "show_drug_license",
        "address",
        "logo_path",
        "latitude",
        "longitude",
        "invoice_footer",
        "expiry_threshold_days",
        "followup_default_days",
        "round_off_mode",
        "max_discount_pct",
        "idle_timeout_minutes",
        "lock_timeout_minutes",
    ]
    return {k: get_setting(db, k) for k in keys}


def set_setting(
    db: Session,
    key: str,
    value: str,
    *,
    user: User | None = None,
    ip_address: str = "",
    commit: bool = False,
) -> Setting:
    row = db.get(Setting, key)
    before = row.value if row else None
    if row is None:
        row = Setting(key=key, value=value)
        db.add(row)
    else:
        row.value = value
        row.updated_at = utcnow()
    db.flush()
    audit.record(
        db,
        action=audit.A_UPDATE,
        entity_type="setting",
        entity_id=key,
        user=user,
        before={"key": key, "value": before},
        after={"key": key, "value": value},
        ip_address=ip_address,
    )
    if commit:
        db.commit()
    return row


def set_settings(
    db: Session,
    values: dict[str, str],
    *,
    user: User | None = None,
    ip_address: str = "",
) -> None:
    for key, value in values.items():
        set_setting(db, key, value, user=user, ip_address=ip_address)
    db.commit()


def maps_url(latitude: str, longitude: str) -> str:
    return f"https://www.google.com/maps/dir/?api=1&destination={latitude},{longitude}"


def qr_png_bytes(db: Session) -> bytes:
    """Generate a QR PNG for the stored pharmacy coordinates.

    Called on demand, so editing coordinates in Settings automatically changes
    the QR on the next render (no stale cache).
    """
    lat = get_setting(db, "latitude")
    lng = get_setting(db, "longitude")
    qr = qrcode.QRCode(version=None, box_size=8, border=2)
    qr.add_data(maps_url(lat, lng))
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def qr_data_uri(db: Session) -> str:
    encoded = base64.b64encode(qr_png_bytes(db)).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def save_logo(db: Session, filename: str, content: bytes, *, user: User | None = None) -> str:
    from app.config import UPLOAD_DIR

    logo_dir = UPLOAD_DIR / "branding"
    logo_dir.mkdir(parents=True, exist_ok=True)
    suffix = Path(filename).suffix.lower() or ".png"
    target = logo_dir / f"logo{suffix}"
    for old in logo_dir.glob("logo.*"):  # one logo at a time (png/jpg may alternate)
        if old.suffix != suffix:
            old.unlink(missing_ok=True)
    target.write_bytes(content)
    # stored relative to the uploads folder so the app can move between machines
    set_setting(db, "logo_path", f"branding/{target.name}", user=user)
    db.commit()
    return str(target)


def logo_file(db: Session) -> Path | None:
    """The uploaded logo on disk, or None.

    Accepts the portable ``branding/logo.png`` form and older absolute paths
    (including ones written by another install): only the file name is
    trusted, and it must live in this install's uploads/branding folder.
    """
    from app.config import UPLOAD_DIR

    stored = (get_setting(db, "logo_path") or "").strip()
    if not stored:
        return None
    name = Path(stored.replace("\\", "/")).name
    if not name.lower().startswith("logo.") or Path(name).suffix.lower() not in (".png", ".jpg", ".jpeg", ".webp", ".svg"):
        return None
    candidate = (UPLOAD_DIR / "branding" / name).resolve()
    root = (UPLOAD_DIR / "branding").resolve()
    return candidate if candidate.parent == root and candidate.is_file() else None
