"""Settings → Invoice Store: the premium WhatsApp invoice templates and the details they print."""
from __future__ import annotations

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.config import UPLOAD_DIR
from app.database import get_db
from app.deps import client_ip, require_permission
from app.models import User
from app.routing import OffloadRoute
from app.services import invoice_premium as P
from app.services import settings_service as S

router = APIRouter(route_class=OffloadRoute)
ADMIN = Depends(require_permission("settings.manage"))
CLASSIC = {"id": "classic", "name": "Classic", "description": "The plain invoice used for printing", "stamp": False}
# editable text fields → settings keys (the shop profile is shared with the printed invoice)
FIELDS = {"pharmacy_name": 120, "address": 300, "contact_numbers": 120, "pharmacy_email": 120,
          "gst_number": 20, "pharmacy_state_code": 2, "drug_license_number": 120,
          "invoice_store_thanks": 60, "invoice_store_note": 120, "invoice_store_closing": 120}
SWITCHES = ("show_gst", "show_drug_license")


def view(db: Session) -> dict:
    o = P.store_options(db)
    return {
        "templates": [{"id": k, **v} for k, v in P.TEMPLATES.items()] + [CLASSIC],
        "template": o["template"] if o["template"] in P.TEMPLATES or o["template"] == "classic" else P.DEFAULT_TEMPLATE,
        "items_per_page": o["items_per_page"],
        "fields": {k: S.get_setting(db, k, "") for k in FIELDS},
        "switches": {k: S.get_setting(db, k, "0").lower() in ("1", "true", "yes", "on") for k in SWITCHES},
        "custom_stamp": bool(o["stamp_path"]),
        "defaults": {"invoice_store_thanks": "Thank you for choosing", "invoice_store_note": "Keep this invoice for your purchase records.",
                     "invoice_store_closing": "Clear details. Thoughtful care."},
    }


@router.get("/api/erp/settings/invoice-store")
def get_store(db: Session = Depends(get_db), user: User = ADMIN):
    return view(db)


@router.put("/api/erp/settings/invoice-store")
async def save_store(request: Request, db: Session = Depends(get_db), user: User = ADMIN):
    body = await request.json()
    values: dict[str, str] = {}
    if "template" in body:
        t = str(body["template"])
        if t not in P.TEMPLATES and t != "classic":
            raise HTTPException(400, "Unknown template")
        values["invoice_store_template"] = t
    if "items_per_page" in body:
        try:
            n = int(body["items_per_page"])
        except (TypeError, ValueError):
            raise HTTPException(400, "Items per page must be a number")
        if not 3 <= n <= 8:
            raise HTTPException(400, "Items per page: 3 to 8")
        values["invoice_store_items_per_page"] = str(n)
    for key, limit in FIELDS.items():
        if key in body.get("fields", {}):
            values[key] = " ".join(str(body["fields"][key] or "").split())[:limit] if key != "address" else str(body["fields"][key] or "").strip()[:limit]
    if values.get("pharmacy_name", "x") == "":
        raise HTTPException(400, "The pharmacy name cannot be empty")
    if values.get("pharmacy_state_code"):
        from app.services.gst import STATES
        if values["pharmacy_state_code"] not in STATES:
            raise HTTPException(400, "Enter a valid two-digit pharmacy state code")
    for key in SWITCHES:
        if key in body.get("switches", {}):
            values[key] = "1" if body["switches"][key] else "0"
    S.set_settings(db, values, user=user, ip_address=client_ip(request))
    return view(db)


def _preview_data(db: Session, items: int) -> dict:
    return P.demo_data(db, max(1, min(30, items)))


@router.get("/api/erp/settings/invoice-store/preview.pdf")
def preview_pdf(template: str = P.DEFAULT_TEMPLATE, items: int = 7, db: Session = Depends(get_db), user: User = ADMIN):
    if template not in P.TEMPLATES:
        raise HTTPException(400, "Preview is for the Invoice Store templates")
    return Response(P.render(_preview_data(db, items), template), media_type="application/pdf",
                    headers={"Content-Disposition": "inline; filename=invoice-preview.pdf", "Cache-Control": "no-store"})


@router.get("/api/erp/settings/invoice-store/preview.png")
def preview_png(template: str = P.DEFAULT_TEMPLATE, items: int = 7, page: int = 1, dpi: int = 80,
                db: Session = Depends(get_db), user: User = ADMIN):
    import pymupdf

    if template not in P.TEMPLATES:
        raise HTTPException(400, "Preview is for the Invoice Store templates")
    doc = pymupdf.open("pdf", P.render(_preview_data(db, items), template))
    page = max(1, min(doc.page_count, page))
    png = doc[page - 1].get_pixmap(dpi=max(40, min(150, dpi))).tobytes("png")
    return Response(png, media_type="image/png", headers={"X-Pages": str(doc.page_count), "Cache-Control": "no-store"})


@router.post("/api/erp/settings/invoice-store/stamp")
async def upload_stamp(request: Request, file: UploadFile = File(...), db: Session = Depends(get_db), user: User = ADMIN):
    import pymupdf

    data = await file.read()
    if len(data) > 2 * 1024 * 1024:
        raise HTTPException(400, "The stamp file is larger than 2 MB")
    name = (file.filename or "").lower()
    kind = "svg" if name.endswith(".svg") or data.lstrip()[:5] in (b"<?xml", b"<svg ") else "png" if data[:8] == b"\x89PNG\r\n\x1a\n" else ""
    if not kind:
        raise HTTPException(400, "Upload the stamp as SVG or PNG (transparent background)")
    try:                                               # it must render, and is only ever shown rendered
        pymupdf.open(stream=data, filetype=kind)[0].get_pixmap(dpi=36)
    except Exception:
        raise HTTPException(400, "That file could not be read as an image")
    folder = UPLOAD_DIR / "branding"
    folder.mkdir(parents=True, exist_ok=True)
    for old in folder.glob("invoice-stamp.*"):
        old.unlink()
    path = folder / f"invoice-stamp.{kind}"
    path.write_bytes(data)
    S.set_settings(db, {"invoice_store_stamp": str(path.relative_to(UPLOAD_DIR))}, user=user, ip_address=client_ip(request))
    return view(db)


@router.delete("/api/erp/settings/invoice-store/stamp")
def reset_stamp(request: Request, db: Session = Depends(get_db), user: User = ADMIN):
    for old in (UPLOAD_DIR / "branding").glob("invoice-stamp.*"):
        old.unlink()
    S.set_settings(db, {"invoice_store_stamp": ""}, user=user, ip_address=client_ip(request))
    return view(db)


@router.get("/api/erp/settings/invoice-store/stamp.png")
def stamp_png(db: Session = Depends(get_db), user: User = ADMIN):
    import pymupdf

    path = P.stamp_file(P.store_options(db))
    doc = pymupdf.open(str(path))
    return Response(doc[0].get_pixmap(dpi=110, alpha=True).tobytes("png"), media_type="image/png", headers={"Cache-Control": "no-store"})
