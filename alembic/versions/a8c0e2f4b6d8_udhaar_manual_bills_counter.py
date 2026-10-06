"""Udhaar ledger, manual bills as their own documents, counter day closes

- customers: Udhaar limit and days to pay
- udhaar_entries / udhaar_payments / udhaar_reminders: what customers owe, what they paid back,
  every reminder sent
- whatsapp_messages: a message is an invoice or an Udhaar reminder (``kind``); ``sale_id`` may be
  empty (reminders of an opening balance, manual bills sent before this release)
- counter_day_closes: each business day's Counter Report frozen when the day ends
- manual_bills / manual_bill_items: manual bills leave ``sales``. Every existing manual bill is
  copied with its lines, payments as written, customer, date and number, then removed from the
  sales tables (with its lines, payment rows and any returns recorded against it — a manual bill
  never had stock, so no stock movement is involved). The copied totals are checked against the
  removed ones before anything is deleted.

DESTRUCTIVE_APPROVED: manual bills are moved out of the sales tables on purpose (the owner's
requirement: a manual bill must never be part of sales, reports, cash, Udhaar or stock). Nothing
is lost: each one is copied to ``manual_bills`` first and the migration stops if the copy and the
original totals differ. The sales figures below change by exactly the manual bills' figures.

Revision ID: a8c0e2f4b6d8
Revises: f6b8d0a2c4e7
Create Date: 2026-10-06 10:00:00.000000
"""
from decimal import Decimal
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'a8c0e2f4b6d8'
down_revision: Union[str, Sequence[str], None] = 'f6b8d0a2c4e7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_REASON = "manual bills moved from sales to manual_bills (1.10.0) — copied and reconciled first"
# what left sales must be exactly what arrived in manual_bills
RECONCILE_MOVED = {
    "count:sales": "count:manual_bills", "count:sale_items": "count:manual_bill_items",
    "sum:sales.total": "sum:manual_bills.total", "sum:sales.subtotal": "sum:manual_bills.subtotal",
    "sum:sales.discount": "sum:manual_bills.discount", "sum:sales.round_off": "sum:manual_bills.round_off",
    "sum:sale_items.quantity": "sum:manual_bill_items.quantity", "sum:sale_items.line_total": "sum:manual_bill_items.line_total",
    "sum:sale_items.discount": "sum:manual_bill_items.discount",
}
# no counterpart table: payment rows become the bill's written payment; returns against a manual bill
# become a note on it (a manual bill never had stock); cost figures of manual lines were always zero / unknown
RECONCILE_EXEMPT = {k: _REASON for k in (
    "count:sale_payments", "count:sale_returns", "count:sale_return_items", "sum:sale_payments.amount",
    "sum:sale_returns.total_refund", "sum:sale_return_items.quantity", "sum:sale_return_items.refund_amount",
    "sum:sale_items.cost_rate", "sum:sale_items.line_cost", "sum:sale_items.net_sale_value",
    "group:sales.payment_status=PAID", "group:sales.payment_status=CANCELLED", "group:sale_returns.status=COMPLETED",
)}

PERMISSIONS = {
    "udhaar.view": ("udhaar", "Udhaar Ledger: what customers owe, ledgers and statements"),
    "udhaar.receive": ("udhaar", "Receive Udhaar payments and send Udhaar reminders"),
    "udhaar.manage": ("udhaar", "Set customers' Udhaar limit and days, and opening balances"),
    "reports.counter": ("reports", "Counter Report: the day's sales and collections by payment mode"),
}
ALL = set(PERMISSIONS)
GRANTS = {
    "Administrator": ALL, "Manager": ALL, "Pharmacist": ALL,
    "Counter Manager": {"udhaar.view", "udhaar.receive", "reports.counter"},
    "Sales Staff": {"udhaar.view", "udhaar.receive"},
    "Accountant": {"udhaar.view", "reports.counter"},
}


def upgrade() -> None:
    conn = op.get_bind()
    sqlite = conn.dialect.name == "sqlite"

    op.add_column('customers', sa.Column('udhaar_limit', sa.Numeric(12, 2), nullable=True))
    op.add_column('customers', sa.Column('udhaar_days', sa.Integer(), nullable=True))

    op.create_table(
        'manual_bills',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('invoice_no', sa.String(40), nullable=False),
        sa.Column('customer_id', sa.Integer(), sa.ForeignKey('customers.id'), nullable=True),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=True),
        sa.Column('sale_date', sa.DateTime(), nullable=False),
        sa.Column('subtotal', sa.Numeric(12, 2), nullable=False),
        sa.Column('discount', sa.Numeric(12, 2), nullable=False),
        sa.Column('round_off', sa.Numeric(12, 2), nullable=False),
        sa.Column('total', sa.Numeric(12, 2), nullable=False),
        sa.Column('payment_mode', sa.String(10), nullable=False),
        sa.Column('payment_parts', sa.JSON(), nullable=True),
        sa.Column('tendered_amount', sa.Numeric(12, 2), nullable=True),
        sa.Column('change_amount', sa.Numeric(12, 2), nullable=True),
        sa.Column('customer_type', sa.String(20), nullable=False),
        sa.Column('notes', sa.Text(), nullable=False),
        sa.Column('status', sa.String(10), nullable=False),
        sa.Column('deleted_at', sa.DateTime(), nullable=True),
        sa.Column('deleted_by', sa.Integer(), sa.ForeignKey('users.id'), nullable=True),
        sa.Column('delete_reason', sa.String(200), nullable=False),
        sa.Column('legacy_sale_id', sa.Integer(), nullable=True),
        sa.Column('client_request_id', sa.String(64), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
    )
    op.create_index('ix_manual_bills_invoice_no', 'manual_bills', ['invoice_no'], unique=True)
    op.create_index('ix_manual_bills_sale_date', 'manual_bills', ['sale_date'])
    op.create_index('ix_manual_bills_status', 'manual_bills', ['status'])
    op.create_index('ix_manual_bills_client_request_id', 'manual_bills', ['client_request_id'], unique=True)
    op.create_table(
        'manual_bill_items',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('bill_id', sa.Integer(), sa.ForeignKey('manual_bills.id', ondelete='CASCADE'), nullable=False),
        sa.Column('line_no', sa.Integer(), nullable=False),
        sa.Column('item_id', sa.Integer(), sa.ForeignKey('items.id'), nullable=True),
        sa.Column('product_name', sa.String(250), nullable=False),
        sa.Column('item_code', sa.String(40), nullable=False),
        sa.Column('pack_size', sa.String(60), nullable=False),
        sa.Column('batch_no', sa.String(60), nullable=False),
        sa.Column('expiry_date', sa.Date(), nullable=True),
        sa.Column('quantity', sa.Integer(), nullable=False),
        sa.Column('rate', sa.Numeric(12, 2), nullable=False),
        sa.Column('discount', sa.Numeric(12, 2), nullable=False),
        sa.Column('line_total', sa.Numeric(12, 2), nullable=False),
    )
    op.create_index('ix_manual_bill_items_bill_id', 'manual_bill_items', ['bill_id'])

    op.create_table(
        'udhaar_entries',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('customer_id', sa.Integer(), sa.ForeignKey('customers.id'), nullable=False),
        sa.Column('sale_id', sa.Integer(), sa.ForeignKey('sales.id'), nullable=True, unique=True),
        sa.Column('kind', sa.String(10), nullable=False),
        sa.Column('business_date', sa.Date(), nullable=False),
        sa.Column('amount', sa.Numeric(12, 2), nullable=False),
        sa.Column('paid', sa.Numeric(12, 2), nullable=False),
        sa.Column('due_date', sa.Date(), nullable=False),
        sa.Column('reminder_date', sa.Date(), nullable=False),
        sa.Column('status', sa.String(10), nullable=False),
        sa.Column('note', sa.Text(), nullable=False),
        sa.Column('created_by', sa.Integer(), sa.ForeignKey('users.id'), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
    )
    op.create_index('ix_udhaar_entries_business_date', 'udhaar_entries', ['business_date'])
    op.create_index('ix_udhaar_entries_reminder_date', 'udhaar_entries', ['reminder_date'])
    op.create_index('ix_udhaar_customer_status', 'udhaar_entries', ['customer_id', 'status'])
    op.create_index('ix_udhaar_due', 'udhaar_entries', ['status', 'due_date'])
    op.create_table(
        'udhaar_payments',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('entry_id', sa.Integer(), sa.ForeignKey('udhaar_entries.id'), nullable=False),
        sa.Column('customer_id', sa.Integer(), sa.ForeignKey('customers.id'), nullable=False),
        sa.Column('amount', sa.Numeric(12, 2), nullable=False),
        sa.Column('mode', sa.String(10), nullable=False),
        sa.Column('reference', sa.String(80), nullable=False),
        sa.Column('note', sa.Text(), nullable=False),
        sa.Column('business_date', sa.Date(), nullable=False),
        sa.Column('received_at', sa.DateTime(), nullable=False),
        sa.Column('return_id', sa.Integer(), sa.ForeignKey('sale_returns.id'), nullable=True),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=True),
    )
    op.create_index('ix_udhaar_payments_entry_id', 'udhaar_payments', ['entry_id'])
    op.create_index('ix_udhaar_payments_customer_id', 'udhaar_payments', ['customer_id'])
    op.create_index('ix_udhaar_payments_business_date', 'udhaar_payments', ['business_date'])

    with op.batch_alter_table('whatsapp_messages') as t:
        t.alter_column('sale_id', existing_type=sa.Integer(), nullable=True)
        t.add_column(sa.Column('kind', sa.String(20), nullable=False, server_default='INVOICE'))
        t.add_column(sa.Column('udhaar_entry_id', sa.Integer(), nullable=True))
        t.add_column(sa.Column('manual_bill_id', sa.Integer(), nullable=True))
        t.create_foreign_key('fk_whatsapp_messages_udhaar_entry', 'udhaar_entries', ['udhaar_entry_id'], ['id'])
        t.create_foreign_key('fk_whatsapp_messages_manual_bill', 'manual_bills', ['manual_bill_id'], ['id'])
        t.create_index('ix_whatsapp_messages_udhaar_entry_id', ['udhaar_entry_id'])
        t.create_index('ix_whatsapp_messages_manual_bill_id', ['manual_bill_id'])

    op.create_table(
        'udhaar_reminders',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('entry_id', sa.Integer(), sa.ForeignKey('udhaar_entries.id'), nullable=False),
        sa.Column('customer_id', sa.Integer(), sa.ForeignKey('customers.id'), nullable=False),
        sa.Column('channel', sa.String(12), nullable=False),
        sa.Column('whatsapp_message_id', sa.Integer(), sa.ForeignKey('whatsapp_messages.id'), nullable=True),
        sa.Column('message', sa.Text(), nullable=False),
        sa.Column('balance', sa.Numeric(12, 2), nullable=False),
        sa.Column('automatic', sa.Boolean(), nullable=False),
        sa.Column('sent_at', sa.DateTime(), nullable=False),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=True),
    )
    op.create_index('ix_udhaar_reminders_entry_id', 'udhaar_reminders', ['entry_id'])
    op.create_index('ix_udhaar_reminders_customer_id', 'udhaar_reminders', ['customer_id'])

    op.create_table(
        'counter_day_closes',
        sa.Column('business_date', sa.Date(), primary_key=True),
        sa.Column('figures', sa.JSON(), nullable=False),
        sa.Column('digest', sa.String(64), nullable=False),
        sa.Column('closed_at', sa.DateTime(), nullable=False),
    )

    # permissions (existing installations; a fresh one is seeded from app/permissions.py)
    ignore = "INSERT OR IGNORE INTO" if sqlite else "INSERT INTO"
    tail = "" if sqlite else " ON CONFLICT DO NOTHING"
    for code, (module, desc) in PERMISSIONS.items():
        conn.execute(sa.text(f"{ignore} permissions (code, module, description) VALUES (:c, :m, :d){tail}"),
                     {"c": code, "m": module, "d": desc})
    for role, codes in GRANTS.items():
        for code in codes:
            conn.execute(sa.text(
                f"{ignore} role_permissions (role_id, permission_id) SELECT r.id, p.id FROM roles r, permissions p "
                f"WHERE r.name = :r AND p.code = :c{tail}"), {"r": role, "c": code})

    _move_manual_bills(conn)


def _move_manual_bills(conn) -> None:
    sales = conn.execute(sa.text(
        "SELECT id, invoice_no, customer_id, user_id, sale_date, subtotal, discount, round_off, total, payment_mode, "
        "payment_status, tendered_amount, change_amount, customer_type, notes, client_request_id, created_at "
        "FROM sales WHERE invoice_type = 'MANUAL' ORDER BY id")).mappings().all()
    if not sales:
        return
    ids = [s["id"] for s in sales]
    bills = sa.table('manual_bills', *[sa.column(c) for c in (
        'invoice_no', 'customer_id', 'user_id', 'sale_date', 'subtotal', 'discount', 'round_off', 'total', 'payment_mode',
        'tendered_amount', 'change_amount', 'customer_type', 'notes', 'status', 'deleted_at',
        'delete_reason', 'legacy_sale_id', 'client_request_id', 'created_at')], sa.column('payment_parts', sa.JSON()))
    lines = sa.table('manual_bill_items', *[sa.column(c) for c in (
        'bill_id', 'line_no', 'item_id', 'product_name', 'item_code', 'pack_size', 'batch_no', 'expiry_date',
        'quantity', 'rate', 'discount', 'line_total')])
    moved_total = Decimal("0")
    for s in sales:
        parts = [{"mode": m, "amount": str(a), "reference": r or ""} for m, a, r in conn.execute(sa.text(
            "SELECT mode, amount, reference FROM sale_payments WHERE sale_id = :s ORDER BY id"), {"s": s["id"]})]
        returns = conn.execute(sa.text(
            "SELECT return_no, total_refund, refund_method, business_date FROM sale_returns WHERE sale_id = :s ORDER BY id"),
            {"s": s["id"]}).all()
        notes = s["notes"] or ""
        for no, amt, method, day in returns:
            notes += f"\nReturn {no} on {day}: ₹{amt} by {method} (recorded before manual bills left sales)"
        void = s["payment_status"] == "CANCELLED"
        conn.execute(bills.insert().values(
            invoice_no=s["invoice_no"], customer_id=s["customer_id"], user_id=s["user_id"], sale_date=s["sale_date"],
            subtotal=s["subtotal"], discount=s["discount"], round_off=s["round_off"], total=s["total"],
            payment_mode=s["payment_mode"], payment_parts=parts, tendered_amount=s["tendered_amount"],
            change_amount=s["change_amount"], customer_type=s["customer_type"] or "WALK_IN", notes=notes.strip(),
            status="DELETED" if void else "ACTIVE", deleted_at=s["created_at"] if void else None,
            delete_reason="Voided before manual bills left sales" if void else "", legacy_sale_id=s["id"],
            client_request_id=s["client_request_id"], created_at=s["created_at"]))
        bill_id = conn.execute(sa.text("SELECT id FROM manual_bills WHERE invoice_no = :n"), {"n": s["invoice_no"]}).scalar()
        for row in conn.execute(sa.text(
                "SELECT line_no, item_id, product_name, item_code, pack_size, batch_no, expiry_date, quantity, rate, discount, "
                "line_total FROM sale_items WHERE sale_id = :s ORDER BY line_no, id"), {"s": s["id"]}).mappings():
            conn.execute(lines.insert().values(bill_id=bill_id, **{k: (v if v is not None or k in ("item_id", "expiry_date") else "")
                                                                   for k, v in row.items()}))
        moved_total += Decimal(str(s["total"]))

    copied = Decimal(str(conn.execute(sa.text("SELECT COALESCE(SUM(total), 0) FROM manual_bills WHERE legacy_sale_id IS NOT NULL")).scalar()))
    copied_lines = conn.execute(sa.text("SELECT COUNT(*) FROM manual_bill_items i JOIN manual_bills b ON b.id = i.bill_id "
                                        "WHERE b.legacy_sale_id IS NOT NULL")).scalar()
    marks = ", ".join(str(int(i)) for i in ids)
    original_lines = conn.execute(sa.text(f"SELECT COUNT(*) FROM sale_items WHERE sale_id IN ({marks})")).scalar()
    if copied.quantize(Decimal("0.01")) != moved_total.quantize(Decimal("0.01")) or copied_lines != original_lines:
        raise RuntimeError(f"Manual bill copy does not reconcile: ₹{copied} / {copied_lines} lines copied, "
                           f"₹{moved_total} / {original_lines} lines in sales — nothing removed")

    # references kept on the moved bill (WhatsApp history) or cleared (a follow-up / held bill pointing at it)
    conn.execute(sa.text(f"UPDATE whatsapp_messages SET manual_bill_id = (SELECT b.id FROM manual_bills b "
                         f"WHERE b.legacy_sale_id = whatsapp_messages.sale_id), sale_id = NULL WHERE sale_id IN ({marks})"))
    conn.execute(sa.text(f"UPDATE customer_followups SET source_sale_id = NULL WHERE source_sale_id IN ({marks})"))
    conn.execute(sa.text(f"UPDATE parked_sales SET completed_sale_id = NULL WHERE completed_sale_id IN ({marks})"))
    conn.execute(sa.text(f"DELETE FROM sale_return_items WHERE return_id IN (SELECT id FROM sale_returns WHERE sale_id IN ({marks}))"))
    conn.execute(sa.text(f"DELETE FROM sale_returns WHERE sale_id IN ({marks})"))
    conn.execute(sa.text(f"DELETE FROM sale_payments WHERE sale_id IN ({marks})"))
    conn.execute(sa.text(f"DELETE FROM sale_items WHERE sale_id IN ({marks})"))
    conn.execute(sa.text(f"DELETE FROM sales WHERE id IN ({marks})"))


def downgrade() -> None:
    raise NotImplementedError("Restore the pre-upgrade snapshot to go back")
