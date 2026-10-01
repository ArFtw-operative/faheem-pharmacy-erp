"""Pharmacy brand assets printed on bills: logo and payment QR code."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Response
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import require_login
from app.models import User
from app.routing import OffloadRoute
from app.services import settings_service

router = APIRouter(prefix="/assets", route_class=OffloadRoute)


@router.get("/logo.png")
def logo_png(db: Session = Depends(get_db), user: User = Depends(require_login)):
    path = settings_service.logo_file(db)
    if path is None:
        return Response(status_code=404)
    media = {".png": "image/png", ".webp": "image/webp", ".svg": "image/svg+xml"}.get(path.suffix.lower(), "image/jpeg")
    return Response(content=path.read_bytes(), media_type=media, headers={"Cache-Control": "no-cache"})


@router.get("/qr.png")
def qr_png(db: Session = Depends(get_db), user: User = Depends(require_login)):
    return Response(content=settings_service.qr_png_bytes(db), media_type="image/png", headers={"Cache-Control": "no-store"})
