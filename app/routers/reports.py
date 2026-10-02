"""Reports pages: sales/purchase/returns/adjustments by cycle and breakdown."""
from __future__ import annotations


from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app import audit
from app.routing import OffloadRoute
from app.database import get_db
from app.deps import client_ip, require_permission
from app.models import User
from app.web import render

router = APIRouter(prefix="/reports", tags=["reports"], route_class=OffloadRoute)

PERIODS = ["today", "week", "month", "year", "all"]


@router.get("")
def reports_page(
    request: Request,
    period: str = "today",
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("reports.sales")),
):
    # Only definitions go to the landing page; generation queries are explicit.
    from app.routers.erp import _boot
    from app.services import report_generator
    boot = _boot(request, db, user)
    boot["initial_module"] = "reports"
    boot["report_catalog"] = report_generator.catalog(user)
    return render(request, "erp.html", db, {"boot": boot}, user=user)



from collections import OrderedDict
from decimal import Decimal
from threading import Lock
from time import monotonic
from secrets import token_urlsafe
from app.permissions import has_permission
from app.services import report_generator, report_document

_snapshots = OrderedDict()
_snapshot_lock = Lock()


def _serialize(value):
    if isinstance(value, Decimal): return str(value)
    if isinstance(value, dict): return {k: _serialize(v) for k, v in value.items()}
    if isinstance(value, list): return [_serialize(v) for v in value]
    return value


def _report_access(user, report_id):
    definition = report_generator.BY_ID.get(report_id)
    if definition is None: raise HTTPException(404, "Unknown report")
    if not has_permission(user, definition["permission"]): raise HTTPException(403, "You do not have access to this report")
    return definition


@router.get("/api/catalog")
def report_catalog(user: User = Depends(require_permission("reports.sales"))):
    return {"reports": report_generator.catalog(user), "can_export": has_permission(user, "reports.export")}


@router.get("/api/options")
def report_options(db: Session = Depends(get_db), user: User = Depends(require_permission("reports.sales"))):
    return report_generator.options(db)


@router.get("/api/lookup")
def report_lookup(kind: str, q: str = "", db: Session = Depends(get_db), user: User = Depends(require_permission("reports.sales"))):
    """Live search behind the Customer and Item filters."""
    from sqlalchemy import select

    from app.models import Item
    from app.routers.erp import _search_ids
    from app.services import customer_service

    if kind == "customer":
        return {"results": [{"id": c.id, "label": c.name, "detail": " · ".join(x for x in (c.mobile, c.customer_id) if x)}
                            for c in customer_service.lookup(db, q, limit=10)]}
    if kind == "item":
        term = q.strip()
        if not term:
            return {"results": []}
        ids = _search_ids(db, term, 12)
        items = {i.id: i for i in db.scalars(select(Item).where(Item.id.in_(ids), Item.deleted_at.is_(None)))}
        return {"results": [{"id": i.id, "label": i.name, "detail": " · ".join(x for x in (i.article_id, i.pack_size, i.manufacturer) if x)}
                            for i in (items[x] for x in ids if x in items)]}
    raise HTTPException(400, "Unknown lookup")


@router.get('/api/purchase-invoices')
def purchase_invoices(supplier: str, period: str = 'all', from_date: str = '', to_date: str = '', invoice: str = '',
                      db: Session = Depends(get_db), user: User = Depends(require_permission('reports.purchase'))):
    from app.services import purchase_audit_report
    raw = dict(supplier=supplier,period=period,invoice=invoice)
    if not supplier: raise HTTPException(400,'Choose a supplier first')
    if period == 'custom': raw.update({'from':from_date,'to':to_date})
    try:
        p,start,end = report_generator.parameters(db,report_generator.BY_ID['supplier-purchases'],raw)
        docs = purchase_audit_report.documents(db,p,start,end)
    except report_generator.ReportError as exc: raise HTTPException(400,str(exc))
    return {'invoices':[dict(id=d.id,invoice=d.invoice_no,reference=d.reference_no or '',
                             date=str(d.invoice_date or report_generator.day_of(d.purchase_date,report_generator.tz_for(db))),
                             status=d.status,total=str(d.total),lines=len(d.items)) for d in docs]}


@router.post("/api/generate")
async def generate_report(request: Request, db: Session = Depends(get_db), user: User = Depends(require_permission("reports.sales"))):
    try: data = await request.json()
    except ValueError: raise HTTPException(400, "Expected a JSON object")
    if not isinstance(data, dict) or not isinstance(data.get("report"), str): raise HTTPException(400, "Choose a report")
    _report_access(user, data["report"])
    try: document = report_generator.generate(db, data["report"], data.get("parameters", {}), data.get("columns"), user=user)
    except report_generator.ReportError as exc: raise HTTPException(400, str(exc))
    document = _serialize(document)
    token = token_urlsafe(24)
    with _snapshot_lock:
        # Limit memory and expire documents after 30 minutes.
        now = monotonic()
        for key in list(_snapshots):
            if now - _snapshots[key][0] > 1800: del _snapshots[key]
        _snapshots[token] = (now, user.id, document)
        while len(_snapshots) > 32: _snapshots.popitem(last=False)
    return {**document, "token": token, "text": report_document.text_document(document)}


@router.get("/api/document/{token}.{fmt}")
def download_document(token: str, fmt: str, request: Request, db: Session = Depends(get_db), user: User = Depends(require_permission("reports.export"))):
    if fmt not in ("csv", "xlsx", "pdf", "txt"): raise HTTPException(400, "Choose PDF, Excel, CSV or text")
    with _snapshot_lock: entry = _snapshots.get(token)
    if entry is None or entry[1] != user.id or monotonic() - entry[0] > 1800:
        raise HTTPException(404, "Report expired; generate it again before exporting")
    document = entry[2]; _report_access(user, document["id"])
    content = {"csv": report_document.csv_bytes, "xlsx": report_document.excel_bytes, "pdf": report_document.pdf_bytes,
               "txt": report_document.text_bytes}[fmt](document)
    audit.record(db, action=audit.A_EXPORT, entity_type="report", entity_id=document["id"], user=user,
                 details=f"Exported {document['title']} ({document['from_date']} to {document['to_date']})", ip_address=client_ip(request), commit=True)
    mime = {"csv": "text/csv", "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "pdf": "application/pdf",
            "txt": "text/plain; charset=utf-8"}[fmt]
    return Response(content, media_type=mime, headers={"Content-Disposition": f"attachment; filename={document['id']}_{document['from_date']}_{document['to_date']}.{fmt}", "Cache-Control": "private, no-store"})
