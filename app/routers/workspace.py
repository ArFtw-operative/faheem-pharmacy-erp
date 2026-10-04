"""Workspace snapshots: the open tabs and unfinished bills of a user at a counter, saved continuously
by the ERP shell so a crash, power cut or reboot never loses work in progress."""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import require_login
from app.models import Sale, User, WorkspaceSnapshot
from app.routing import OffloadRoute
from app.utils import utcnow

router = APIRouter(route_class=OffloadRoute)
MAX_BYTES = 1_000_000


def _terminal(value) -> str:
    return "".join(ch for ch in str(value or "") if ch.isalnum() or ch in "-_")[:64]


@router.get("/api/erp/workspace")
def get_workspace(terminal: str = "", db: Session = Depends(get_db), user: User = Depends(require_login)):
    """This counter's snapshot; a counter that lost its id gets the user's most recent one."""
    q = select(WorkspaceSnapshot).where(WorkspaceSnapshot.user_id == user.id)
    snap = db.scalar(q.where(WorkspaceSnapshot.terminal == _terminal(terminal))) if terminal else None
    other = False
    if snap is None:
        snap = db.scalar(q.order_by(WorkspaceSnapshot.saved_at.desc()))
        other = snap is not None
        # another PC that is in use right now keeps its own tabs and unfinished bills: a second counter
        # (or a browser on another PC) starts clean instead of opening the same bills twice. Only a PC that
        # has been quiet for a while (replaced, crashed) hands its work over.
        if snap is not None and snap.saved_at is not None and (utcnow() - snap.saved_at).total_seconds() < 600:
            return {"data": None, "other_terminal_active": True}
    if snap is None:
        return {"data": None}
    try:
        data = json.loads(snap.data or "{}")
    except ValueError:
        data = None
    return {"data": data, "saved_at": snap.saved_at.isoformat() + "Z", "other_terminal": other}


async def _save(request: Request, db: Session, user: User) -> dict:
    raw = await request.body()
    if len(raw) > MAX_BYTES:
        raise HTTPException(413, "Workspace snapshot too large")
    try:
        body = json.loads(raw or b"{}")
    except ValueError:
        raise HTTPException(400, "Invalid snapshot")
    terminal = _terminal(body.get("terminal"))
    data = body.get("data")
    if not terminal or not isinstance(data, dict):
        raise HTTPException(400, "terminal and data are required")
    snap = db.scalar(select(WorkspaceSnapshot).where(WorkspaceSnapshot.user_id == user.id, WorkspaceSnapshot.terminal == terminal))
    if snap is None:
        snap = WorkspaceSnapshot(user_id=user.id, terminal=terminal)
        db.add(snap)
    snap.data = json.dumps(data, separators=(",", ":"), default=str)
    snap.saved_at = utcnow()
    db.commit()                                   # durable before answering: survives a power cut
    return {"saved_at": snap.saved_at.isoformat() + "Z"}


@router.put("/api/erp/workspace")
async def put_workspace(request: Request, db: Session = Depends(get_db), user: User = Depends(require_login)):
    return await _save(request, db, user)


@router.post("/api/erp/workspace")          # navigator.sendBeacon when the page is closing
async def post_workspace(request: Request, db: Session = Depends(get_db), user: User = Depends(require_login)):
    return await _save(request, db, user)


@router.get("/api/erp/sales/by-request/{request_id}")
def sale_by_request(request_id: str, db: Session = Depends(get_db), user: User = Depends(require_login)):
    """Was this bill already completed (e.g. just before a crash)? Bills carry a request id."""
    sale = db.scalar(select(Sale).where(Sale.client_request_id == str(request_id)[:64]))
    if sale is None:
        raise HTTPException(404, "Not completed")
    return {"id": sale.id, "invoice_no": sale.invoice_no, "total": str(sale.total)}
