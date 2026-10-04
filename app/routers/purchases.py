"""Purchases API: supplier invoices (import → review → post), suppliers and
purchase returns. Consumed by the ERP Purchases module; every stock effect
goes through :mod:`app.services.purchasing` and the inventory ledger.
"""
from __future__ import annotations

import csv
import io
from datetime import date, datetime, time, timedelta
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, Response
from sqlalchemy import case, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.database import get_db
from app.deps import client_ip, require_permission
from app.models import Batch, Item, Purchase, PurchaseItem, PurchaseReturn, Supplier, SupplierProductMap, User
from app.routing import OffloadRoute
from app.services import sheet_import, purchase_service, purchasing, units
from app.utils import money

router = APIRouter(tags=["purchases"], route_class=OffloadRoute)
MAX_UPLOAD = 15 * 1024 * 1024


@router.get("/api/erp/medicine-reference")
def medicine_reference(q: str = "", limit: int = 5,
                       user: User = Depends(require_permission("purchase.view"))):
    from app.services.medicine_reference import candidates
    return {"candidates": candidates(q[:250], limit), "automatic_product_match": False}


def _run(db: Session, fn, *args, **kwargs):
    try:
        out = fn(db, *args, **kwargs)
        db.commit()
        return out
    except (purchasing.PurchaseError, purchase_service.PurchaseError) as exc:
        db.rollback()
        detail = {"message": str(exc), "code": getattr(exc, "code", ""), "details": getattr(exc, "details", None)}
        raise HTTPException(409 if detail["code"] in ("DUPLICATE_FILE", "DUPLICATE_INVOICE", "TOTAL_DIFFERENCE") else 400, detail)
    except IntegrityError as exc:
        db.rollback()
        msg = str(exc.orig).split("\n")[0]
        if "uq_purchases_posted_invoice" in msg or "purchases.supplier_id, purchases.invoice_no" in msg:
            msg = "This supplier invoice number is already posted"
        raise HTTPException(409, {"message": msg, "code": "INTEGRITY"})


def _doc(db: Session, purchase_id: int) -> Purchase:
    purchase = purchasing.get(db, purchase_id)
    if purchase is None:
        raise HTTPException(404, "Purchase not found")
    return purchase


def _recount(db: Session, purchases) -> None:
    """Drafts counted by an older engine are counted again (once) before they are shown. A failure
    never stops the screen: the draft is shown as it was."""
    from app.services import purchase_automation
    for p in purchases:
        try:
            if purchase_automation.refresh_if_stale(db, p):
                db.commit()
        except Exception:                      # noqa: BLE001 — shown as stored; the next open tries again
            db.rollback()
            import logging
            logging.getLogger("pharmacy.purchases").exception("recount of purchase %s failed", getattr(p, "id", "?"))


def _line(purchase: Purchase, line_id: int) -> PurchaseItem:
    line = next((l for l in purchase.items if l.id == line_id), None)
    if line is None:
        raise HTTPException(404, "Line not found")
    return line


def _iso(v) -> str:
    return v.isoformat() if v else ""


def _header(p: Purchase) -> dict:
    return {
        "id": p.id, "reference_no": p.reference_no or "", "status": p.status,
        "supplier_id": p.supplier_id, "supplier": p.supplier.name if p.supplier else "",
        "invoice_no": p.invoice_no, "invoice_date": _iso(p.invoice_date), "created_at": _iso(p.created_at),
        "posted_at": _iso(p.posted_at), "total": str(p.total or 0),
        "supplier_total": str(p.supplier_total) if p.supplier_total is not None else "",
        "format": p.source_format, "has_source": bool(p.source_file), "notes": p.notes or "",
        "cancel_reason": p.cancel_reason or "", "warnings": p.extraction_meta or "",
        "charges": p.charges or {}, "column_map": p.column_map or [],
    }


def _line_view(l: PurchaseItem, gst_ctx: tuple | None = None, limits: dict | None = None) -> dict:
    it = l.item
    from app.services import receipt_decision, packaging_service, units
    physical = receipt_decision.definition(l)
    staged = (l.corrections or {}).get('_physical_adjustment')
    form = physical['dosage_form'] or (physical['base_unit'] if staged else packaging_service.detect_form(
        it if it else Item(name=l.product_name, generic_name='', dosage_form='')))
    receipt = l.receipt_decision or {}
    received = receipt.get('received_base_units') if receipt.get('resolved') else None
    physical_view = {**physical, 'form': form, 'received': received,
        'equivalent': units.describe_stock(received, physical['units_per_pack'], physical['base_unit'], physical['pack_unit'])
            if received is not None and (physical['units_per_pack'] or 1) > 1 else ''}
    from app.services import packaging_conversion
    try:
        paid, free = receipt_decision.quantities(purchasing.effective(l))
    except ValueError:
        paid = free = None
    stock = packaging_conversion.receipt_view(receipt, physical, pack_text=l.pack_size, item=it, paid=paid, free=free)
    stock["hierarchy"] = packaging_conversion.hierarchy_text(l.pack_size, form=physical.get("dosage_form") or "",
                                                             description=l.product_name)
    view = {
        "id": l.id, "line_no": l.line_no, "status": l.status, "name": l.product_name, "stock": stock,
        "supplier_code": l.supplier_code, "batch": l.batch_no, "expiry": _iso(l.expiry_date), "expiry_raw": l.expiry_raw,
        "qty": l.quantity_raw if sheet_import.has_fraction(l.quantity_raw) else l.quantity, "free": l.quantity_free, "rate": str(l.rate), "mrp": str(l.mrp),
        "gst": str(l.gst_rate) if l.gst_rate is not None else "", "discount": str(l.discount), "amount": str(l.line_total),
        "hsn": l.hsn_code, "pack": l.pack_size, "manufacturer": l.manufacturer,
        "item": {"id": it.id, "code": it.article_id, "name": it.name, "pack": it.pack_size, "upp": it.units_per_pack or 1,
                 "base_unit": it.base_unit, "pack_unit": it.pack_unit, "category": it.category} if it else None,
        "product_category": (it.category if it else l.category) or "",
        "match": l.match_method, "new_product": bool(l.new_product), "category": l.category,
        "dosage_form": l.dosage_form, "base_unit": l.base_unit, "pack_unit": l.pack_unit, "units_per_pack": l.units_per_pack,
        "raw": l.raw or {}, "corrections": {k: v for k, v in (l.corrections or {}).items()},
        "receipt": l.receipt_decision or {},
        "physical": physical_view,
        "issues": l.issues or [], "batch_id": l.batch_id,
        # GST snapshot: taxable after bill discount, tax split, landed value and rate per pack incl. GST
        "taxable": _s(l.taxable_value), "gst_amount": _s(l.gst_amount), "cgst": _s(l.cgst_amount), "sgst": _s(l.sgst_amount),
        "igst": _s(l.igst_amount), "landed": _s(l.landed_total), "rate_incl": _s(l.landed_rate), "gst_source": l.gst_source or "",
        "gst_worked_out": False, "batch_rate": _s(l.batch.purchase_rate) if l.batch is not None else "",
        "batch_basis": l.batch.rate_basis if l.batch is not None else "",
    }
    if l.gst_amount is None and gst_ctx is not None and l.status != purchasing.CLOSED:
        # posted before GST tracking: shown with the same invoice arithmetic, stored nothing
        from app.services import gst as G

        factor, mode = gst_ctx
        b = G.line_breakdown(l, factor, mode, purchasing._received_packs(l))
        view.update(taxable=str(b["taxable"]), gst_amount=str(b["gst"]), cgst=str(b["cgst"]), sgst=str(b["sgst"]),
                    igst=str(b["igst"]), landed=str(b["landed"]), rate_incl=str(b["rate_incl"]), gst_worked_out=True)
    view["cost"] = packaging_conversion.unit_costs(view["landed"] or None, stock, receipt, physical)
    from app.services import confidence_gate
    view["gate"] = (confidence_gate.assess(l, limits) if l.status not in purchasing.DONE
                    else {"state": confidence_gate.line_state(l), "confidence": None, "fields": {}, "provenance": {}})
    return view


def _s(v) -> str:
    return str(v) if v is not None else ""


def _document(p: Purchase) -> dict:
    from sqlalchemy.orm import object_session

    from app.services import gst as G

    db = object_session(p)
    summary = purchasing.summary(p)
    summary["gst"] = purchasing.gst_summary(db, p)
    from app.services import purchase_automation
    summary["automation"] = purchase_automation.assessment(db, p)
    ctx = (G.bill_discount_factor(p), summary["gst"]["mode"])
    from app.services import confidence_gate
    limits = confidence_gate.thresholds(db)
    lines = [_line_view(l, ctx, limits) for l in sorted(p.items, key=lambda l: l.line_no)]
    places = purchasing.location_suggestions(db, p)
    for view in lines:
        view["location"] = places.get(view["id"], {"state": "NONE"})
    gate = {s: 0 for s in (confidence_gate.AUTO_ACCEPT, confidence_gate.WARNING, confidence_gate.REVIEW, confidence_gate.BLOCK)}
    for l in lines:
        if l["status"] not in purchasing.DONE:
            gate[l["gate"]["state"]] += 1
    summary["gate"] = {**gate, "thresholds": limits}
    return {"purchase": _header(p), "summary": summary, "lines": lines}


# --------------------------------------------------------------------------- register
@router.get("/api/erp/purchases")
def purchase_register(status: str = "", supplier: str = "", q: str = "", start: str = "", end: str = "",
                      limit: int = 200, offset: int = 0, db: Session = Depends(get_db),
                      user: User = Depends(require_permission("purchase.view"))):
    _recount(db, db.scalars(select(Purchase).where(Purchase.status.in_(("DRAFT", "PARTIAL")))).all())
    lines = select(PurchaseItem.purchase_id, func.count(PurchaseItem.id).label("n"),
                   func.sum(case((PurchaseItem.status.in_(("READY", "CORRECTED", "POSTED", "CLOSED")), 0), else_=1)).label("open")
                   ).group_by(PurchaseItem.purchase_id).subquery()
    stmt = (select(Purchase, lines.c.n, lines.c.open).outerjoin(lines, lines.c.purchase_id == Purchase.id)
            .options(selectinload(Purchase.supplier)))
    if status:
        stmt = stmt.where(Purchase.status == status.upper())
    if supplier.isdigit():
        stmt = stmt.where(Purchase.supplier_id == int(supplier))
    if q.strip():
        like = f"%{q.strip()}%"
        inside = select(PurchaseItem.id).where(PurchaseItem.purchase_id == Purchase.id, or_(
            PurchaseItem.product_name.ilike(like), PurchaseItem.batch_no.ilike(like), PurchaseItem.supplier_code.ilike(like)))
        stmt = stmt.outerjoin(Supplier, Supplier.id == Purchase.supplier_id).where(
            or_(Purchase.invoice_no.ilike(like), Purchase.reference_no.ilike(like), Supplier.name.ilike(like), inside.exists()))
    for value, op in ((start, "ge"), (end, "lt")):
        if value:
            try:
                d = date.fromisoformat(value)
            except ValueError:
                raise HTTPException(400, "Dates must be YYYY-MM-DD")
            moment = datetime.combine(d if op == "ge" else d + timedelta(days=1), time.min)
            stmt = stmt.where(Purchase.created_at >= moment if op == "ge" else Purchase.created_at < moment)
    total = db.scalar(select(func.count()).select_from(stmt.subquery()))
    rows = db.execute(stmt.order_by(Purchase.id.desc()).limit(min(max(limit, 1), 500)).offset(max(offset, 0))).all()
    counts = dict(db.execute(select(Purchase.status, func.count(Purchase.id)).group_by(Purchase.status)).all())
    return {"total": total, "counts": counts,
            "purchases": [{**_header(p), "lines": n or 0, "open_lines": open_ or 0} for p, n, open_ in rows]}


@router.get("/api/erp/purchases/template.csv")
def purchase_template(user: User = Depends(require_permission("purchase.view"))):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Product Code", "Product", "Pack", "Manufacturer", "HSN", "Batch", "Expiry", "Qty", "Free", "Rate", "MRP",
                "Disc %", "GST %", "Amount"])
    w.writerow(["DOL650", "DOLO 650 TAB", "15S", "MICRO LABS", "30049099", "DL24031", "May-2028", "10", "1",
                "22.40", "33.60", "0", "12", "224.00"])
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": "attachment; filename=purchase-invoice-template.csv"})


@router.get("/api/erp/purchases/fields")
def purchase_fields(user: User = Depends(require_permission("purchase.view"))):
    """What a supplier column can mean (for the Columns window)."""
    from app.services import invoice_agent

    return {"fields": [{"code": k, "label": v} for k, v in invoice_agent.FIELD_HELP.items()]}


@router.post("/api/erp/purchases/import")
async def purchase_import(file: UploadFile = File(...), supplier_id: str = Form(""), invoice_no: str = Form(""),
                          invoice_date: str = Form(""), supplier_total: str = Form(""),
                          db: Session = Depends(get_db), user: User = Depends(require_permission("purchase.create"))):
    content = await file.read(MAX_UPLOAD + 1)
    if len(content) > MAX_UPLOAD:
        raise HTTPException(413, "File is larger than 15 MB")
    sid = int(supplier_id) if supplier_id.isdigit() else None
    if sid and db.get(Supplier, sid) is None:
        raise HTTPException(400, "Supplier not found")
    inv_date = purchasing.parse_date(invoice_date) if invoice_date else None
    from app.services import purchase_automation
    drafts = _run(db, purchase_automation.import_file, file.filename or "invoice", content, supplier_id=sid,
                  invoice_no=invoice_no, invoice_date=inv_date, supplier_total=supplier_total or None, user=user)
    return {**_document(_doc(db, drafts[0].id)), "drafts": [d.id for d in drafts]}


@router.post("/api/erp/purchases/{purchase_id}/prepare")
def purchase_prepare(purchase_id: int, db: Session = Depends(get_db),
                     user: User = Depends(require_permission("purchase.create"))):
    from app.services import purchase_automation
    p = _doc(db, purchase_id)
    purchasing._open(p)
    _run(db, purchase_automation.prepare, p, user=user)
    return _document(p)


@router.post("/api/erp/purchases")
async def purchase_create(request: Request, db: Session = Depends(get_db),
                          user: User = Depends(require_permission("purchase.create"))):
    data = await request.json()
    sid = int(data["supplier_id"]) if str(data.get("supplier_id") or "").isdigit() else None
    purchase = _run(db, purchasing.create_manual, supplier_id=sid, invoice_no=str(data.get("invoice_no") or ""),
                    invoice_date=purchasing.parse_date(data.get("invoice_date")), supplier_total=data.get("supplier_total"),
                    user=user)
    return _document(_doc(db, purchase.id))


@router.get("/api/erp/purchases/{purchase_id}")
def purchase_detail(purchase_id: int, db: Session = Depends(get_db),
                    user: User = Depends(require_permission("purchase.view"))):
    p = _doc(db, purchase_id)
    _recount(db, [p])
    return _document(_doc(db, purchase_id))


@router.get("/api/erp/purchases/{purchase_id}/source")
def purchase_source(purchase_id: int, db: Session = Depends(get_db),
                    user: User = Depends(require_permission("purchase.view"))):
    p = _doc(db, purchase_id)
    path = Path(p.source_file or "")
    if not p.source_file or not path.is_file():
        raise HTTPException(404, "No source file for this purchase")
    return FileResponse(path, filename=f"{p.reference_no or 'draft-' + str(p.id)}{path.suffix}")


@router.delete("/api/erp/purchases/{purchase_id}")
def purchase_delete(purchase_id: int, db: Session = Depends(get_db),
                    user: User = Depends(require_permission("purchase.create"))):
    _run(db, purchasing.delete_draft, _doc(db, purchase_id), user=user)
    return {"deleted": purchase_id}


@router.put("/api/erp/purchases/{purchase_id}")
async def purchase_update(purchase_id: int, request: Request, db: Session = Depends(get_db),
                          user: User = Depends(require_permission("purchase.create"))):
    data = await request.json()
    p = _doc(db, purchase_id)
    _run(db, purchasing.update_header, p, data, user=user)
    return _document(_doc(db, purchase_id))


@router.put("/api/erp/purchases/{purchase_id}/columns")
async def purchase_columns(purchase_id: int, request: Request, db: Session = Depends(get_db),
                           user: User = Depends(require_permission("purchase.create"))):
    data = await request.json()
    changes = {str(k): str(v or "ignore") for k, v in (data.get("changes") or {}).items()}
    if not changes:
        raise HTTPException(400, "No column changes")
    p = _doc(db, purchase_id)
    _run(db, purchasing.remap, p, changes, user=user)
    return _document(_doc(db, purchase_id))


@router.post("/api/erp/purchases/{purchase_id}/lines")
async def purchase_add_line(purchase_id: int, request: Request, db: Session = Depends(get_db),
                            user: User = Depends(require_permission("purchase.create"))):
    data = await request.json()
    p = _doc(db, purchase_id)
    line = _run(db, purchasing.add_line, p, data, user=user)
    return {**_document(_doc(db, purchase_id)), "line_id": line.id}


@router.put("/api/erp/purchases/{purchase_id}/lines/{line_id}")
async def purchase_correct(purchase_id: int, line_id: int, request: Request, db: Session = Depends(get_db),
                           user: User = Depends(require_permission("purchase.create"))):
    data = await request.json()
    p = _doc(db, purchase_id)
    _run(db, purchasing.correct, p, _line(p, line_id), data, user=user)
    return _document(_doc(db, purchase_id))


@router.delete("/api/erp/purchases/{purchase_id}/lines/{line_id}")
def purchase_delete_line(purchase_id: int, line_id: int, db: Session = Depends(get_db),
                         user: User = Depends(require_permission("purchase.create"))):
    p = _doc(db, purchase_id)
    _run(db, purchasing.delete_line, p, _line(p, line_id), user=user)
    return _document(_doc(db, purchase_id))


@router.post("/api/erp/purchases/{purchase_id}/lines/bulk")
async def purchase_bulk(purchase_id: int, request: Request, db: Session = Depends(get_db),
                        user: User = Depends(require_permission("purchase.create"))):
    """Same correction on many lines: {line_ids, changes} — or confirmed matches {matches: {line_id: item_id}}."""
    data = await request.json()
    p = _doc(db, purchase_id)
    matches = data.get("matches")
    if matches is not None:
        try:
            matches = {int(k): int(v) for k, v in matches.items()}
        except (TypeError, ValueError, AttributeError):
            raise HTTPException(400, "matches must map line ids to product ids")
    ids = data.get("line_ids") or []
    if matches is None and (not isinstance(ids, list) or not ids):
        raise HTTPException(400, "Select at least one line")
    allowed = set(purchasing.EDITABLE) | {"new_product", "accept"}
    changes = {k: v for k, v in (data.get("changes") or {}).items() if k in allowed}
    if matches is None and not changes:
        raise HTTPException(400, "Nothing to change")
    out = _run(db, purchasing.bulk, p, ids, changes, matches=matches, user=user)
    return {**_document(_doc(db, purchase_id)), "result": out}


@router.post("/api/erp/purchases/{purchase_id}/lines/suggest")
async def purchase_suggest_many(purchase_id: int, request: Request, db: Session = Depends(get_db),
                                user: User = Depends(require_permission("purchase.view"))):
    data = await request.json()
    p = _doc(db, purchase_id)
    wanted = {int(i) for i in (data.get("line_ids") or []) if str(i).isdigit()}
    return {"suggestions": {str(l.id): purchasing.suggestions(db, l, 3) for l in p.items
                            if l.id in wanted and l.status not in purchasing.DONE}}


@router.get("/api/erp/purchases/{purchase_id}/lines/{line_id}/suggestions")
def purchase_suggestions(purchase_id: int, line_id: int, db: Session = Depends(get_db),
                         user: User = Depends(require_permission("purchase.view"))):
    p = _doc(db, purchase_id)
    return {"suggestions": purchasing.suggestions(db, _line(p, line_id))}


@router.get("/api/erp/purchases/{purchase_id}/lines/{line_id}/packaging")
def purchase_line_packaging(purchase_id: int, line_id: int, db: Session = Depends(get_db),
                            user: User = Depends(require_permission("purchase.view"))):
    from app.services import packaging_correction

    p = _doc(db, purchase_id)
    return packaging_correction.proposal(_line(p, line_id))


@router.put("/api/erp/purchases/{purchase_id}/lines/{line_id}/packaging")
async def purchase_line_packaging_save(purchase_id: int, line_id: int, request: Request, db: Session = Depends(get_db),
                                       user: User = Depends(require_permission("purchase.create"))):
    """Correction dialog: what one invoice Qty is, saved for this invoice, the product or the supplier."""
    from app.services import packaging_correction

    data = await request.json()
    p = _doc(db, purchase_id)
    _run(db, packaging_correction.apply, p, _line(p, line_id), data, user=user)
    return _document(_doc(db, purchase_id))


@router.get("/api/erp/purchases/{purchase_id}/lines/{line_id}/inspect")
def purchase_line_inspect(purchase_id: int, line_id: int, db: Session = Depends(get_db),
                          user: User = Depends(require_permission("purchase.view"))):
    """Match Inspector: the signals behind the line's product and pack decisions."""
    from app.services import confidence_gate, packaging_parser, product_matcher

    p = _doc(db, purchase_id)
    line = _line(p, line_id)
    out = product_matcher.inspect(db, p, line)
    parsed = packaging_parser.parse(line.pack_size, master_form=(line.item.dosage_form if line.item else ""),
                                    description=line.product_name)
    out["packaging"] = {**parsed.as_dict(), "decision": line.receipt_decision or {}}
    out["gate"] = confidence_gate.assess(line, confidence_gate.thresholds(db)) if line.status not in purchasing.DONE else None
    return out


@router.put("/api/erp/purchases/{purchase_id}/lines/{line_id}/invoice-unit")
async def purchase_invoice_unit(purchase_id: int, line_id: int, request: Request, db: Session = Depends(get_db),
                                user: User = Depends(require_permission("purchase.create"))):
    from app.services import receipt_decision
    data = await request.json()
    if not isinstance(data, dict):
        raise HTTPException(400, "Invoice-unit verification requires a JSON object")
    p = _doc(db, purchase_id)
    _run(db, receipt_decision.confirm, p, _line(p, line_id), factor=data.get("units_per_invoice_unit"),
         mrp_basis=data.get("mrp_basis"), reason=data.get("reason"), user=user)
    return _document(_doc(db, purchase_id))


@router.put('/api/erp/purchases/{purchase_id}/lines/{line_id}/physical-quantity')
async def purchase_physical_quantity(purchase_id:int,line_id:int,request:Request,db:Session=Depends(get_db),
                                    user:User=Depends(require_permission('purchase.create'))):
    from app.services import purchase_adjustment
    data=await request.json()
    if not isinstance(data,dict):
        raise HTTPException(400,'Quantity adjustment requires an object')
    p=_doc(db,purchase_id)
    _run(db,purchase_adjustment.adjust,p,_line(p,line_id),data,user=user)
    return _document(_doc(db,purchase_id))


@router.get("/api/erp/purchases/{purchase_id}/decisions")
def purchase_decisions(purchase_id: int, db: Session = Depends(get_db),
                       user: User = Depends(require_permission("purchase.view"))):
    p = _doc(db, purchase_id)
    return {"purchase_id": p.id, "lines": [{"id": l.id, "status": l.status,
             "receipt": l.receipt_decision or {}, "issues": l.issues or []} for l in p.items]}


@router.post("/api/erp/purchases/{purchase_id}/post")
async def purchase_post(purchase_id: int, request: Request, db: Session = Depends(get_db),
                        user: User = Depends(require_permission("purchase.post"))):
    data = await request.json()
    p = _doc(db, purchase_id)
    ids = data.get("line_ids")
    if ids is not None and (not isinstance(ids, list) or not all(str(i).isdigit() for i in ids)):
        raise HTTPException(400, "line_ids must be a list of line ids")
    _run(db, purchasing.post, p, user=user, accept_difference=bool(data.get("accept_difference")),
         accept_warnings=bool(data.get("accept_warnings")), line_ids=[int(i) for i in ids] if ids is not None else None)
    return _document(_doc(db, purchase_id))


@router.post("/api/erp/purchases/{purchase_id}/close")
async def purchase_close(purchase_id: int, request: Request, db: Session = Depends(get_db),
                         user: User = Depends(require_permission("purchase.post"))):
    data = await request.json()
    p = _doc(db, purchase_id)
    _run(db, purchasing.close_remaining, p, reason=str(data.get("reason") or ""), user=user)
    return _document(_doc(db, purchase_id))


@router.post("/api/erp/purchases/{purchase_id}/rollback")
async def purchase_rollback(purchase_id: int, request: Request, db: Session = Depends(get_db),
                            user: User = Depends(require_permission("purchase.post"))):
    """Posted lines back to review: their stock is reversed through the ledger."""
    data = await request.json()
    p = _doc(db, purchase_id)
    ids = data.get("line_ids")
    _run(db, purchasing.rollback, p, reason=str(data.get("reason") or ""),
         line_ids=[int(i) for i in ids] if isinstance(ids, list) and ids else None, user=user)
    return _document(_doc(db, purchase_id))


@router.post("/api/erp/purchases/{purchase_id}/lines/category")
async def purchase_lines_category(purchase_id: int, request: Request, db: Session = Depends(get_db),
                                  user: User = Depends(require_permission("purchase.create"))):
    """Change only the category of the selected lines' products."""
    data = await request.json()
    p = _doc(db, purchase_id)
    ids = data.get("line_ids") if isinstance(data.get("line_ids"), list) else []
    result = _run(db, purchasing.set_category, p, [int(i) for i in ids], str(data.get("category") or ""), user=user)
    return {**_document(_doc(db, purchase_id)), "result": result}


@router.post("/api/erp/purchases/{purchase_id}/lines/location")
async def purchase_lines_location(purchase_id: int, request: Request, db: Session = Depends(get_db),
                                  user: User = Depends(require_permission("rack.assign"))):
    """Where the selected lines' stock goes: one rack / box, or each line's suggestion (``suggested``)."""
    data = await request.json()
    p = _doc(db, purchase_id)
    ids = data.get("line_ids") if isinstance(data.get("line_ids"), list) else []
    if not all(str(i).isdigit() for i in ids):
        raise HTTPException(400, "line_ids must be a list of line ids")
    result = _run(db, purchasing.set_location, p, [int(i) for i in ids], data.get("rack_id"), data.get("box_id"),
                  suggested=bool(data.get("suggested")), user=user)
    return {**_document(_doc(db, purchase_id)), "result": result}


@router.post("/api/erp/purchases/{purchase_id}/cancel")
async def purchase_cancel(purchase_id: int, request: Request, db: Session = Depends(get_db),
                          user: User = Depends(require_permission("purchase.create"))):
    data = await request.json()
    p = _doc(db, purchase_id)
    _run(db, purchasing.cancel, p, reason=str(data.get("reason") or ""), user=user)
    return _document(_doc(db, purchase_id))


@router.get("/api/erp/purchase-products")
def purchase_products(q: str = "", limit: int = 20, db: Session = Depends(get_db),
                      user: User = Depends(require_permission("purchase.view"))):
    """Product picker for matching a supplier line (every product, stocked or not)."""
    from app.routers.erp import _search_ids

    term = q.strip()
    if not term:
        return {"items": []}
    ids = _search_ids(db, term, min(max(limit, 1), 40))
    items = {i.id: i for i in db.scalars(select(Item).where(Item.id.in_(ids), Item.deleted_at.is_(None)))}
    return {"items": [{"id": i.id, "code": i.article_id, "name": i.name, "pack": i.pack_size, "manufacturer": i.manufacturer,
                       "upp": i.units_per_pack or 1, "base_unit": i.base_unit, "pack_unit": i.pack_unit,
                       "active": bool(i.is_active)} for i in (items[x] for x in ids if x in items)]}


# --------------------------------------------------------------------------- suppliers
def _supplier_view(s: Supplier, stats: dict | None = None) -> dict:
    st = stats or {}
    return {"id": s.id, "code": s.code or "", "name": s.name, "contact": s.contact or "", "phone": s.phone or "",
            "email": s.email or "", "gst_number": s.gst_number or "", "address": s.address or "",
            "payment_terms": s.payment_terms or "", "credit_days": s.credit_days or 0, "active": bool(s.is_active),
            "purchases": st.get("n", 0), "value": str(st.get("value", 0)), "last": st.get("last", "")}


@router.get("/api/erp/suppliers")
def supplier_list(all: int = 0, db: Session = Depends(get_db), user: User = Depends(require_permission("inventory.view"))):
    stats = {sid: {"n": n, "value": money(v or 0), "last": _iso(last)} for sid, n, v, last in db.execute(
        select(Purchase.supplier_id, func.count(Purchase.id), func.sum(Purchase.total), func.max(Purchase.posted_at))
        .where(Purchase.status.in_(("POSTED", "PARTIAL"))).group_by(Purchase.supplier_id))}
    stmt = select(Supplier).order_by(func.lower(Supplier.name))
    if not all:
        stmt = stmt.where(Supplier.is_active.is_(True))
    return {"suppliers": [_supplier_view(s, stats.get(s.id)) for s in db.scalars(stmt)]}


@router.post("/api/erp/suppliers")
async def supplier_create(request: Request, db: Session = Depends(get_db),
                          user: User = Depends(require_permission("supplier.manage"))):
    data = await request.json()
    s = _run(db, purchasing.save_supplier, data, user=user)
    return {"supplier": _supplier_view(s)}


@router.put("/api/erp/suppliers/{supplier_id}")
async def supplier_update(supplier_id: int, request: Request, db: Session = Depends(get_db),
                          user: User = Depends(require_permission("supplier.manage"))):
    s = db.get(Supplier, supplier_id)
    if s is None:
        raise HTTPException(404, "Supplier not found")
    data = await request.json()
    _run(db, purchasing.save_supplier, data, supplier=s, user=user)
    return {"supplier": _supplier_view(s)}


@router.get("/api/erp/suppliers/{supplier_id}/mappings")
def supplier_mappings(supplier_id: int, db: Session = Depends(get_db),
                      user: User = Depends(require_permission("purchase.view"))):
    rows = db.scalars(select(SupplierProductMap).where(SupplierProductMap.supplier_id == supplier_id)
                      .options(selectinload(SupplierProductMap.item)).order_by(SupplierProductMap.description_raw))
    return {"mappings": [{"id": m.id, "supplier_code": m.supplier_code, "description": m.description_raw,
                          "item_id": m.item_id, "item": m.item.name if m.item else "", "uses": m.uses,
                          "confirmed_at": _iso(m.confirmed_at)} for m in rows]}


@router.delete("/api/erp/suppliers/{supplier_id}/mappings/{map_id}")
def supplier_mapping_delete(supplier_id: int, map_id: int, db: Session = Depends(get_db),
                            user: User = Depends(require_permission("supplier.manage"))):
    from app import audit

    m = db.get(SupplierProductMap, map_id)
    if m is None or m.supplier_id != supplier_id:
        raise HTTPException(404, "Mapping not found")
    audit.record(db, action=audit.A_DELETE, entity_type="supplier_product_map", entity_id=m.id, user=user,
                 before={"supplier_code": m.supplier_code, "description": m.description_raw, "item_id": m.item_id},
                 details="Supplier product mapping removed")
    db.delete(m)
    db.commit()
    return supplier_mappings(supplier_id, db, user)


# --------------------------------------------------------------------------- purchase returns
@router.get("/api/erp/purchase-returns")
def purchase_returns(supplier: str = "", limit: int = 200, db: Session = Depends(get_db),
                     user: User = Depends(require_permission("purchase.view"))):
    stmt = select(PurchaseReturn).order_by(PurchaseReturn.id.desc()).limit(min(max(limit, 1), 500))
    if supplier.isdigit():
        stmt = stmt.where(PurchaseReturn.supplier_id == int(supplier))
    rows = list(db.scalars(stmt))
    sup = {s.id: s.name for s in db.scalars(select(Supplier).where(Supplier.id.in_({r.supplier_id for r in rows if r.supplier_id})))}
    bat = {b.id: b for b in db.scalars(select(Batch).where(Batch.id.in_({r.batch_id for r in rows if r.batch_id})))}
    return {"returns": [{
        "id": r.id, "reference_no": r.reference_no or "", "date": _iso(r.return_date), "supplier": sup.get(r.supplier_id, ""),
        "product": r.product_name, "batch": bat[r.batch_id].batch_no if r.batch_id in bat else "",
        "quantity": r.quantity, "value": str(r.value), "reason": r.reason or "", "status": r.status,
        "purchase_id": r.purchase_id} for r in rows]}


@router.get("/api/erp/purchase-returns/batches")
def purchase_return_batches(item_id: int, db: Session = Depends(get_db),
                            user: User = Depends(require_permission("purchase.return"))):
    item = db.get(Item, item_id)
    if item is None:
        raise HTTPException(404, "Product not found")
    rows = db.scalars(select(Batch).where(Batch.item_id == item_id, Batch.quantity > 0)
                      .options(selectinload(Batch.supplier)).order_by(Batch.expiry_date.asc().nulls_last()))
    return {"item": {"id": item.id, "name": item.name, "upp": item.units_per_pack or 1, "base_unit": item.base_unit,
                     "pack_unit": item.pack_unit},
            "batches": [{"id": b.id, "batch_no": b.batch_no, "expiry": _iso(b.expiry_date), "stock": b.quantity,
                         "stock_label": units.describe_stock(b.quantity, item.units_per_pack or 1, item.base_unit, item.pack_unit),
                         "rate": str(b.purchase_rate), "upp": b.units_per_pack or 1,
                         "supplier": b.supplier.name if b.supplier else "", "supplier_id": b.supplier_id} for b in rows]}


@router.post("/api/erp/purchase-returns")
async def purchase_return_create(request: Request, db: Session = Depends(get_db),
                                 user: User = Depends(require_permission("purchase.return"))):
    data = await request.json()
    batch = db.get(Batch, int(data.get("batch_id") or 0))
    if batch is None:
        raise HTTPException(400, "Choose the batch being returned")
    try:
        qty = units.parse_qty_expression(data.get("quantity"), batch.units_per_pack or 1)
    except (units.UnitError, ValueError) as exc:
        raise HTTPException(400, str(exc) or "Quantity not understood")
    value = data.get("value")
    if value in (None, ""):   # default: the batch's purchase rate for the returned units
        value = units.line_amount(batch.purchase_rate, batch.units_per_pack or 1, qty)
    if not str(data.get("reason") or "").strip():
        raise HTTPException(400, "Enter the reason for the return")
    ret = _run(db, purchase_service.create_return, item_id=batch.item_id, batch_id=batch.id, quantity=qty, value=value,
               supplier_id=int(data["supplier_id"]) if str(data.get("supplier_id") or "").isdigit() else None,
               reason=str(data.get("reason")).strip()[:500], user=user, ip_address=client_ip(request) if request else "")
    return {"reference_no": ret.reference_no, "id": ret.id, **purchase_returns("", 200, db, user)}
