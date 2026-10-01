"""WhatsApp invoices: send / resend from POS and Sales History, and the Settings → WhatsApp
Invoicing screen (administrators only: pairing, QR, message, invoice image)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import require_permission
from app.models import User, WhatsAppMessage
from app.routing import OffloadRoute
from app.services.whatsapp import service as wa

router = APIRouter(tags=["whatsapp"], route_class=OffloadRoute)


def _run(db: Session, fn, *a, **kw):
    try:
        out = fn(db, *a, **kw)
        db.commit()
        return out
    except wa.WhatsAppError as exc:
        db.rollback()
        raise HTTPException(400, str(exc))


# --------------------------------------------------------------------------- POS / Sales History
@router.get("/api/erp/whatsapp/status")
def whatsapp_status(fresh: int = 0, user: User = Depends(require_permission("whatsapp.send"))):
    return wa.public_status(fresh=bool(fresh))


@router.post("/api/erp/sales/{sale_id}/whatsapp")
async def whatsapp_send(sale_id: int, request: Request, db: Session = Depends(get_db),
                        user: User = Depends(require_permission("whatsapp.send"))):
    from app.routers.sales import _visible_sale

    _visible_sale(db, sale_id, user)                   # own-sales scope applies here too
    data = await request.json() if request.headers.get("content-length") not in (None, "0") else {}
    msg = _run(db, wa.send_invoice, sale_id, (data or {}).get("phone") or None, user=user)
    return {"message": wa.payload(msg), "status": wa.public_status()}


@router.get("/api/erp/sales/{sale_id}/whatsapp")
def whatsapp_history(sale_id: int, db: Session = Depends(get_db), user: User = Depends(require_permission("whatsapp.send"))):
    from app.routers.sales import _visible_sale

    _visible_sale(db, sale_id, user)
    return {"messages": wa.history(db, sale_id), "status": wa.public_status()}


@router.get("/api/erp/whatsapp/messages/{message_id}")
def whatsapp_message(message_id: int, db: Session = Depends(get_db), user: User = Depends(require_permission("whatsapp.send"))):
    m = db.get(WhatsAppMessage, message_id)
    if m is None:
        raise HTTPException(404, "Message not found")
    return {"message": wa.payload(m)}


@router.post("/api/erp/whatsapp/messages/{message_id}/retry")
def whatsapp_retry(message_id: int, db: Session = Depends(get_db), user: User = Depends(require_permission("whatsapp.send"))):
    from app.routers.sales import _visible_sale

    m = db.get(WhatsAppMessage, message_id)
    if m is None:
        raise HTTPException(404, "Message not found")
    _visible_sale(db, m.sale_id, user)
    return {"message": wa.payload(_run(db, wa.retry, message_id, user=user))}


# --------------------------------------------------------------------------- Settings (administrators)
def _settings_view(db: Session) -> dict:
    s = wa.settings(db)
    counts = dict(db.execute(select(WhatsAppMessage.status, func.count()).group_by(WhatsAppMessage.status)).all())
    recent = db.scalars(select(WhatsAppMessage).order_by(WhatsAppMessage.id.desc()).limit(25)).all()
    return {"connection": wa.admin_status(), "template": s["template"], "placeholders": wa.PLACEHOLDERS,
            "default_template": wa.DEFAULT_TEMPLATE, "ad": {"enabled": s["ad_enabled"], "caption": s["ad_caption"],
            "has_image": bool(wa.ad_image(db)), "updated_at": s["ad_updated_at"]},
            "queue": {k.lower(): v for k, v in counts.items()}, "recent": [wa.payload(m) for m in recent],
            "gateway": {"provider": wa.provider().name, "setup": "docs/WHATSAPP.md"}}


@router.get("/api/erp/settings/whatsapp")
def settings_whatsapp(db: Session = Depends(get_db), user: User = Depends(require_permission("settings.manage"))):
    return _settings_view(db)


@router.post("/api/erp/settings/whatsapp/connect")
def settings_connect(db: Session = Depends(get_db), user: User = Depends(require_permission("settings.manage"))):
    _run(db, wa.connect, user=user)
    return _settings_view(db)


@router.get("/api/erp/settings/whatsapp/connection")
def settings_connection(user: User = Depends(require_permission("settings.manage"))):
    return wa.admin_status(fresh=True)                 # polled while the QR is on screen


@router.post("/api/erp/settings/whatsapp/logout")
def settings_logout(db: Session = Depends(get_db), user: User = Depends(require_permission("settings.manage"))):
    _run(db, wa.logout, user=user)
    return _settings_view(db)


@router.put("/api/erp/settings/whatsapp")
async def settings_save(request: Request, db: Session = Depends(get_db), user: User = Depends(require_permission("settings.manage"))):
    _run(db, wa.save_settings, await request.json(), user=user)
    return _settings_view(db)


@router.post("/api/erp/settings/whatsapp/image")
async def settings_image(file: UploadFile = File(...), db: Session = Depends(get_db),
                         user: User = Depends(require_permission("settings.manage"))):
    content = await file.read(wa.AD_MAX_BYTES + 1)
    _run(db, wa.save_ad_image, file.filename or "image", content, user=user)
    return _settings_view(db)


@router.delete("/api/erp/settings/whatsapp/image")
def settings_image_remove(db: Session = Depends(get_db), user: User = Depends(require_permission("settings.manage"))):
    _run(db, wa.remove_ad_image, user=user)
    return _settings_view(db)


@router.get("/api/erp/settings/whatsapp/image")
def settings_image_view(db: Session = Depends(get_db), user: User = Depends(require_permission("settings.manage"))):
    img = wa.ad_image(db)
    if img is None:
        raise HTTPException(404, "No image")
    return Response(img[0], media_type=img[1], headers={"Cache-Control": "no-store"})


@router.post("/api/erp/settings/whatsapp/preview")
async def settings_preview(request: Request, db: Session = Depends(get_db), user: User = Depends(require_permission("settings.manage"))):
    from app.models import Sale

    data = await request.json()
    sale = db.scalar(select(Sale).where(Sale.payment_status != "CANCELLED").order_by(Sale.id.desc()).limit(1))
    if sale is None:
        return {"text": data.get("template", "")}
    return {"text": wa.render_message(db, sale, data.get("template") or None), "invoice_no": sale.invoice_no}


@router.post("/api/erp/settings/whatsapp/test")
async def settings_test(request: Request, db: Session = Depends(get_db), user: User = Depends(require_permission("settings.manage"))):
    """Send the latest bill's invoice to the administrator's own number, to check the setup."""
    from app.models import Sale

    data = await request.json()
    sale = db.scalar(select(Sale).where(Sale.payment_status != "CANCELLED").order_by(Sale.id.desc()).limit(1))
    if sale is None:
        raise HTTPException(400, "Make one bill first — the test sends its invoice")
    msg = _run(db, wa.send_invoice, sale.id, data.get("phone") or "", user=user)
    return {"message": wa.payload(msg)}
