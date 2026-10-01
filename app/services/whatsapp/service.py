"""WhatsAppService — customer-requested invoices on WhatsApp.

    send_invoice(db, sale_id, phone)      queue the sale's invoice (POS or Sales History)
    retry(db, message_id)                 send a failed one again (same invoice file)
    process_due(db)                       background worker: deliver what is due

Nothing here can stop a sale: sending is queued in the database and delivered by
a background worker; network or WhatsApp trouble only affects the queue. The
invoice PDF is generated once per version of the bill and stored, so a resend
delivers exactly the same document — never a new sale or invoice.

Only transactional, customer-requested invoices are sent; there is no bulk or
promotional sending. The optional image is attached to the invoice the customer
asked for, never sent on its own.
"""
from __future__ import annotations

import hashlib
import logging
import re
import threading
import time
from datetime import timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import audit
from app.config import UPLOAD_DIR
from app.models import Sale, User, WhatsAppMessage
from app.services import settings_service
from app.services.whatsapp import provider as P
from app.utils import utcnow

log = logging.getLogger("pharmacy.whatsapp")

MAX_ATTEMPTS = 3
BACKOFF = (timedelta(seconds=30), timedelta(minutes=2))      # before the 2nd and 3rd attempt
DEFAULT_TEMPLATE = "Thank you for shopping at {pharmacy}. Please find your invoice #{invoice_no} attached."
PLACEHOLDERS = {"pharmacy": "pharmacy name", "invoice_no": "invoice number", "customer": "customer name",
                "amount": "bill amount", "date": "bill date"}
DISCONNECTED_MSG = "WhatsApp disconnected — reconnect from Settings."
AD_TYPES = {b"\x89PNG": ("image/png", "png"), b"\xff\xd8\xff": ("image/jpeg", "jpg"), b"RIFF": ("image/webp", "webp")}
AD_MAX_BYTES = 2 * 1024 * 1024

_provider_override: P.Provider | None = None       # tests inject a fake gateway here


class WhatsAppError(Exception):
    pass


def provider() -> P.Provider:
    return _provider_override or P.default_provider()


# --------------------------------------------------------------------------- phone
def normalize_phone(raw: str | None) -> str:
    """Indian mobile → 91XXXXXXXXXX. Accepts 98765 43210, +91-98765-43210, 098765 43210, 919876543210."""
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    elif len(digits) == 13 and digits.startswith("091"):
        digits = digits[3:]
    if len(digits) != 10 or digits[0] not in "6789":
        raise WhatsAppError(f"“{raw or ''}” is not an Indian mobile number (10 digits starting 6–9)")
    return "91" + digits


# --------------------------------------------------------------------------- settings
def settings(db: Session) -> dict:
    g = lambda k, d="": settings_service.get_setting(db, k, d) or d
    return {"template": g("whatsapp_message_template", DEFAULT_TEMPLATE),
            "ad_enabled": g("whatsapp_ad_enabled", "false") == "true",
            "ad_caption": g("whatsapp_ad_caption", ""),
            "ad_image": g("whatsapp_ad_image", ""),
            "ad_updated_at": g("whatsapp_ad_updated_at", ""),
            "paired": g("whatsapp_paired", "false") == "true"}


def save_settings(db: Session, data: dict, *, user: User | None = None) -> dict:
    if "template" in data:
        t = str(data["template"] or "").strip()
        if not t:
            raise WhatsAppError("The message cannot be empty")
        if len(t) > 1000:
            raise WhatsAppError("Keep the message under 1,000 characters")
        unknown = set(re.findall(r"\{(\w+)\}", t)) - set(PLACEHOLDERS)
        if unknown:
            raise WhatsAppError("Unknown placeholder " + ", ".join("{" + u + "}" for u in sorted(unknown))
                                + " — use " + ", ".join("{" + p + "}" for p in PLACEHOLDERS))
        settings_service.set_setting(db, "whatsapp_message_template", t, user=user)
    if "ad_enabled" in data:
        settings_service.set_setting(db, "whatsapp_ad_enabled", "true" if data["ad_enabled"] else "false", user=user)
    if "ad_caption" in data:
        settings_service.set_setting(db, "whatsapp_ad_caption", str(data["ad_caption"] or "").strip()[:300], user=user)
    return settings(db)


def save_ad_image(db: Session, filename: str, content: bytes, *, user: User | None = None) -> dict:
    kind = next((v for k, v in AD_TYPES.items() if content.startswith(k)), None)
    if kind is None or (kind[1] == "webp" and content[8:12] != b"WEBP"):
        raise WhatsAppError("Use a PNG, JPEG or WebP image")
    if len(content) > AD_MAX_BYTES:
        raise WhatsAppError(f"The image is {len(content) / 1048576:.1f} MB — keep it under 2 MB")
    folder = UPLOAD_DIR / "whatsapp"
    folder.mkdir(parents=True, exist_ok=True)
    stamp = utcnow().strftime("%Y%m%d%H%M%S")
    path = folder / f"invoice-image-{stamp}.{kind[1]}"       # old images are kept (they were sent with past invoices)
    path.write_bytes(content)
    settings_service.set_setting(db, "whatsapp_ad_image", str(path.relative_to(UPLOAD_DIR)), user=user)
    settings_service.set_setting(db, "whatsapp_ad_updated_at", utcnow().isoformat(timespec="seconds"), user=user)
    audit.record(db, action=audit.A_UPDATE, entity_type="setting", entity_id="whatsapp_ad_image", user=user,
                 details=f"WhatsApp invoice image replaced ({filename}, {len(content)} bytes)")
    return settings(db)


def remove_ad_image(db: Session, *, user: User | None = None) -> dict:
    settings_service.set_setting(db, "whatsapp_ad_image", "", user=user)
    settings_service.set_setting(db, "whatsapp_ad_enabled", "false", user=user)
    return settings(db)


def ad_image(db: Session) -> tuple[bytes, str, str] | None:
    rel = settings(db)["ad_image"]
    if not rel:
        return None
    path = (UPLOAD_DIR / rel).resolve()
    if UPLOAD_DIR.resolve() not in path.parents or not path.is_file():
        return None
    data = path.read_bytes()
    kind = next((v for k, v in AD_TYPES.items() if data.startswith(k)), ("image/png", "png"))
    return data, kind[0], path.name


# --------------------------------------------------------------------------- invoice
def render_message(db: Session, sale: Sale, template: str | None = None) -> str:
    from app.services.business_time import timezone_name

    profile = settings_service.get_profile(db)
    local = sale.sale_date.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(timezone_name(db))) if sale.sale_date else None
    values = {"pharmacy": profile.get("pharmacy_name") or "Faheem Pharmacy", "invoice_no": sale.invoice_no,
              "customer": sale.customer.name if sale.customer else "", "amount": f"₹{sale.total:,.2f}",
              "date": local.strftime("%d-%b-%Y") if local else ""}
    text = template or settings(db)["template"]
    return re.sub(r"\{(\w+)\}", lambda m: str(values.get(m.group(1), m.group(0))), text).strip()


def invoice_pdf(db: Session, sale: Sale) -> Path:
    """The invoice PDF of this version of the bill, generated once and stored. An edited bill
    (same number, different lines) gets its own file; a resend reuses the stored one."""
    from app.services import invoice_render

    version = hashlib.sha256(f"{sale.id}|{sale.total}|{sorted((i.id, i.quantity, str(i.line_total)) for i in sale.items)}".encode()).hexdigest()[:12]
    safe = re.sub(r"[^A-Za-z0-9_-]+", "-", sale.invoice_no)
    path = UPLOAD_DIR / "invoices" / f"{safe}-{version}.pdf"
    if not path.is_file():
        path.parent.mkdir(parents=True, exist_ok=True)
        pdf = invoice_render.build_invoice_pdf(sale, settings_service.get_profile(db), settings_service.qr_png_bytes(db))
        tmp = path.with_suffix(".part")
        tmp.write_bytes(pdf)
        tmp.replace(path)
    return path


# --------------------------------------------------------------------------- queue
def send_invoice(db: Session, sale_id: int, customer_phone: str | None = None, *, user: User | None = None) -> WhatsAppMessage:
    """Queue the sale's invoice for WhatsApp. Returns at once; the worker delivers it."""
    sale = db.get(Sale, sale_id)
    if sale is None:
        raise WhatsAppError("Sale not found")
    if sale.payment_status == "CANCELLED":
        raise WhatsAppError(f"{sale.invoice_no} was voided — its invoice cannot be sent")
    raw = customer_phone or (sale.customer.mobile if sale.customer else "")
    if not raw:
        raise WhatsAppError("This bill has no customer mobile — type the number the customer wants it on")
    phone = normalize_phone(raw)
    earlier = db.scalar(select(WhatsAppMessage.id).where(WhatsAppMessage.sale_id == sale.id).limit(1))
    msg = WhatsAppMessage(sale_id=sale.id, invoice_no=sale.invoice_no, customer_phone=phone, status="QUEUED",
                          is_resend=earlier is not None, message_text=render_message(db, sale),
                          pdf_path=str(invoice_pdf(db, sale).relative_to(UPLOAD_DIR)), provider=provider().name,
                          queued_at=utcnow(), next_attempt_at=utcnow(), created_by=user.id if user else None)
    db.add(msg)
    db.flush()
    audit.record(db, action=audit.A_CREATE, entity_type="sale", entity_id=sale.invoice_no, user=user,
                 details=f"Invoice {'re' if msg.is_resend else ''}sent to WhatsApp queue for {phone[:4]}…{phone[-3:]}")
    _wake.set()
    return msg


def retry(db: Session, message_id: int, *, user: User | None = None) -> WhatsAppMessage:
    msg = db.get(WhatsAppMessage, message_id)
    if msg is None:
        raise WhatsAppError("Message not found")
    if msg.status == "SENT":
        raise WhatsAppError("Already delivered — use Resend to send it again")
    msg.status, msg.next_attempt_at, msg.last_error = "QUEUED", utcnow(), ""
    audit.record(db, action=audit.A_UPDATE, entity_type="sale", entity_id=msg.invoice_no, user=user, details="WhatsApp invoice retried")
    _wake.set()
    return msg


def history(db: Session, sale_id: int) -> list[dict]:
    rows = db.scalars(select(WhatsAppMessage).where(WhatsAppMessage.sale_id == sale_id).order_by(WhatsAppMessage.id.desc())).all()
    return [payload(m) for m in rows]


def payload(m: WhatsAppMessage) -> dict:
    return {"id": m.id, "invoice_id": m.sale_id, "invoice_no": m.invoice_no,
            "customer_phone": f"+{m.customer_phone[:2]} {m.customer_phone[2:7]} {m.customer_phone[7:]}",
            "whatsapp_status": m.status.lower(), "attempt_count": m.attempt_count, "last_error": m.last_error,
            "is_resend": m.is_resend, "queued_at": m.queued_at.isoformat() + "Z" if m.queued_at else None,
            "sent_at": m.sent_at.isoformat() + "Z" if m.sent_at else None}


def _deliver(db: Session, msg: WhatsAppMessage, gw: P.Provider) -> None:
    conn = gw.connection()
    if conn.state != P.CONNECTED:
        raise P.TemporaryError(DISCONNECTED_MSG if conn.state in (P.DISCONNECTED, P.QR_REQUIRED, P.STARTING) else conn.detail)
    path = (UPLOAD_DIR / msg.pdf_path).resolve()
    if UPLOAD_DIR.resolve() not in path.parents or not path.is_file():
        raise P.PermanentError("The stored invoice PDF is missing")
    msg.provider_message_id = gw.send_file(msg.customer_phone, f"Invoice-{msg.invoice_no}.pdf", path.read_bytes(),
                                           "application/pdf", msg.message_text)[:120]
    s = settings(db)
    img = ad_image(db) if s["ad_enabled"] else None
    if img:
        try:                     # the image never decides whether the invoice was delivered
            gw.send_file(msg.customer_phone, img[2], img[0], img[1], s["ad_caption"])
        except (P.TemporaryError, P.PermanentError) as exc:
            log.warning("Invoice %s sent; its image was not: %s", msg.invoice_no, exc)


def process_due(db: Session, limit: int = 10) -> int:
    """Deliver queued messages that are due. Temporary failures retry (30 s, 2 min), then fail."""
    now = utcnow()
    due = db.scalars(select(WhatsAppMessage).where(WhatsAppMessage.status == "QUEUED", WhatsAppMessage.next_attempt_at <= now)
                     .order_by(WhatsAppMessage.id).limit(limit)).all()
    if not due:
        return 0
    gw = provider()
    for msg in due:
        msg.status, msg.attempt_count = "SENDING", msg.attempt_count + 1
        db.commit()
        try:
            _deliver(db, msg, gw)
            msg.status, msg.sent_at, msg.last_error = "SENT", utcnow(), ""
        except P.PermanentError as exc:
            msg.status, msg.last_error = "FAILED", str(exc)[:500]
        except Exception as exc:                       # network, gateway, disconnected: temporary
            msg.last_error = str(exc)[:500]
            if msg.attempt_count < MAX_ATTEMPTS:
                msg.status, msg.next_attempt_at = "QUEUED", utcnow() + BACKOFF[min(msg.attempt_count - 1, len(BACKOFF) - 1)]
            else:
                msg.status = "FAILED"
        db.commit()
    return len(due)


def recover(db: Session) -> None:
    """After a restart: anything caught mid-send goes back to the queue."""
    for m in db.scalars(select(WhatsAppMessage).where(WhatsAppMessage.status == "SENDING")):
        m.status, m.next_attempt_at = "QUEUED", utcnow()
    db.commit()


# --------------------------------------------------------------------------- connection
_cache: dict = {"at": 0.0, "conn": None}
_cache_lock = threading.Lock()
_wake = threading.Event()


def connection(fresh: bool = False) -> P.Connection:
    with _cache_lock:
        if not fresh and _cache["conn"] is not None and time.monotonic() - _cache["at"] < 10:
            return _cache["conn"]
    conn = provider().connection()
    with _cache_lock:
        _cache.update(at=time.monotonic(), conn=conn)
    return conn


def public_status(fresh: bool = False) -> dict:
    """What POS / Sales History may see: the state and a message — never the QR or credentials."""
    c = connection(fresh)
    msg = {P.CONNECTED: "WhatsApp connected", P.NOT_CONFIGURED: "WhatsApp is not set up — ask the administrator (Settings)"}.get(c.state, DISCONNECTED_MSG)
    return {"state": c.state, "connected": c.state == P.CONNECTED, "message": msg}


def admin_status(fresh: bool = True) -> dict:
    c = connection(fresh)
    return {"state": c.state, "connected": c.state == P.CONNECTED, "detail": c.detail, "qr": c.qr}


def connect(db: Session, *, user: User | None = None) -> dict:
    c = provider().connect()
    with _cache_lock:
        _cache.update(at=time.monotonic(), conn=c)
    if c.state == P.CONNECTED:
        settings_service.set_setting(db, "whatsapp_paired", "true", user=user)
    audit.record(db, action=audit.A_UPDATE, entity_type="setting", entity_id="whatsapp", user=user, details=f"WhatsApp connect: {c.state}")
    return {"state": c.state, "connected": c.state == P.CONNECTED, "detail": c.detail, "qr": c.qr}


def logout(db: Session, *, user: User | None = None) -> dict:
    try:
        provider().logout()
    except P.TemporaryError as exc:
        raise WhatsAppError(str(exc))
    settings_service.set_setting(db, "whatsapp_paired", "false", user=user)
    audit.record(db, action=audit.A_UPDATE, entity_type="setting", entity_id="whatsapp", user=user, details="WhatsApp logged out")
    return admin_status(fresh=True)


def keep_connected(db: Session) -> None:
    """After a reboot: a paired session that dropped is started again (no QR needed while its keys last)."""
    if not settings(db)["paired"]:
        return
    c = connection(fresh=True)
    if c.state in (P.DISCONNECTED, P.ERROR):
        c = provider().connect()
        with _cache_lock:
            _cache.update(at=time.monotonic(), conn=c)
        log.info("WhatsApp reconnect after restart: %s", c.state)


def wait_for_work(timeout: float) -> None:
    _wake.wait(timeout)
    _wake.clear()
