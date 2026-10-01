"""purchases: supplier master, supplier product mapping, staged review lines

Purchases become documents (DRAFT → POSTED / CANCELLED); import lines keep raw,
normalised and corrected values; the same supplier invoice can be posted once.

Revision ID: a3b5c7d9e1f3
Revises: f2a4b6c8d0e2
Create Date: 2026-09-30 15:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a3b5c7d9e1f3'
down_revision: Union[str, Sequence[str], None] = 'f2a4b6c8d0e2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('suppliers') as t:
        t.add_column(sa.Column('code', sa.String(20), nullable=True))
        t.add_column(sa.Column('phone', sa.String(40), nullable=False, server_default=''))
        t.add_column(sa.Column('email', sa.String(120), nullable=False, server_default=''))
        t.add_column(sa.Column('payment_terms', sa.String(60), nullable=False, server_default=''))
        t.add_column(sa.Column('credit_days', sa.Integer(), nullable=False, server_default='0'))
        t.add_column(sa.Column('updated_at', sa.DateTime(), nullable=True))
    op.create_index('uq_suppliers_code', 'suppliers', ['code'], unique=True)
    conn = op.get_bind()
    for (sid,) in conn.execute(sa.text("SELECT id FROM suppliers ORDER BY id")).fetchall():
        conn.execute(sa.text("UPDATE suppliers SET code = :c WHERE id = :i"), {"c": f"SUP{sid:04d}", "i": sid})

    op.create_table(
        'supplier_product_maps',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('supplier_id', sa.Integer(), sa.ForeignKey('suppliers.id', ondelete='CASCADE'), nullable=False),
        sa.Column('supplier_code', sa.String(40), nullable=False, server_default=''),
        sa.Column('description_key', sa.String(250), nullable=False, server_default=''),
        sa.Column('description_raw', sa.String(250), nullable=False, server_default=''),
        sa.Column('item_id', sa.Integer(), sa.ForeignKey('items.id'), nullable=False),
        sa.Column('confirmed_by', sa.Integer(), sa.ForeignKey('users.id'), nullable=True),
        sa.Column('confirmed_at', sa.DateTime(), nullable=False, server_default=sa.func.current_timestamp()),
        sa.Column('uses', sa.Integer(), nullable=False, server_default='0'),
        sa.UniqueConstraint('supplier_id', 'supplier_code', 'description_key', name='uq_supplier_product_map'),
    )
    op.create_index('ix_supplier_product_maps_supplier_id', 'supplier_product_maps', ['supplier_id'])
    op.create_index('ix_supplier_product_maps_item_id', 'supplier_product_maps', ['item_id'])

    with op.batch_alter_table('purchases') as t:
        t.add_column(sa.Column('reference_no', sa.String(30), nullable=True))
        t.add_column(sa.Column('invoice_date', sa.Date(), nullable=True))
        t.add_column(sa.Column('supplier_total', sa.Numeric(14, 2), nullable=True))
        t.add_column(sa.Column('source_format', sa.String(10), nullable=False, server_default=''))
        t.add_column(sa.Column('notes', sa.Text(), nullable=False, server_default=''))
        t.add_column(sa.Column('posted_at', sa.DateTime(), nullable=True))
        t.add_column(sa.Column('posted_by', sa.Integer(), nullable=True))
        t.add_column(sa.Column('cancelled_at', sa.DateTime(), nullable=True))
        t.add_column(sa.Column('cancelled_by', sa.Integer(), nullable=True))
        t.add_column(sa.Column('cancel_reason', sa.Text(), nullable=False, server_default=''))
    conn.execute(sa.text("UPDATE purchases SET status = 'POSTED', posted_at = created_at WHERE status IN ('RECEIVED', 'PARTIAL')"))
    conn.execute(sa.text("UPDATE purchases SET status = 'DRAFT' WHERE status NOT IN ('POSTED', 'CANCELLED')"))
    for (pid,) in conn.execute(sa.text("SELECT id FROM purchases WHERE status = 'POSTED' ORDER BY id")).fetchall():
        conn.execute(sa.text("UPDATE purchases SET reference_no = :r WHERE id = :i"), {"r": f"PUR-{pid:06d}", "i": pid})
    op.create_index('uq_purchases_reference_no', 'purchases', ['reference_no'], unique=True)
    op.create_index('ix_purchases_status', 'purchases', ['status'])
    op.create_index('ix_purchases_source_sha256', 'purchases', ['source_sha256'])
    op.create_index('ix_purchases_supplier_invoice', 'purchases', ['supplier_id', 'invoice_no'])
    op.create_index('uq_purchases_posted_invoice', 'purchases', ['supplier_id', 'invoice_no'], unique=True,
                    sqlite_where=sa.text("status = 'POSTED' AND invoice_no != ''"))

    with op.batch_alter_table('purchase_items') as t:
        t.add_column(sa.Column('line_no', sa.Integer(), nullable=False, server_default='0'))
        t.add_column(sa.Column('raw', sa.JSON(), nullable=True))
        t.add_column(sa.Column('corrections', sa.JSON(), nullable=True))
        t.add_column(sa.Column('issues', sa.JSON(), nullable=True))
        t.add_column(sa.Column('status', sa.String(24), nullable=False, server_default='NEEDS_REVIEW'))
        t.add_column(sa.Column('supplier_code', sa.String(40), nullable=False, server_default=''))
        t.add_column(sa.Column('expiry_raw', sa.String(40), nullable=False, server_default=''))
        t.add_column(sa.Column('gst_rate', sa.Numeric(5, 2), nullable=True))
        t.add_column(sa.Column('discount', sa.Numeric(12, 2), nullable=False, server_default='0'))
        t.add_column(sa.Column('match_method', sa.String(20), nullable=False, server_default=''))
        t.add_column(sa.Column('new_product', sa.Boolean(), nullable=False, server_default=sa.false()))
        t.add_column(sa.Column('category', sa.String(30), nullable=False, server_default=''))
        t.add_column(sa.Column('dosage_form', sa.String(20), nullable=False, server_default=''))
        t.add_column(sa.Column('base_unit', sa.String(20), nullable=False, server_default=''))
        t.add_column(sa.Column('pack_unit', sa.String(20), nullable=False, server_default=''))
        t.add_column(sa.Column('units_per_pack', sa.Integer(), nullable=True))
        t.add_column(sa.Column('batch_id', sa.Integer(), nullable=True))
    conn.execute(sa.text("UPDATE purchase_items SET status = 'POSTED' WHERE purchase_id IN (SELECT id FROM purchases WHERE status = 'POSTED')"))
    op.create_index('ix_purchase_items_status', 'purchase_items', ['status'])

    with op.batch_alter_table('purchase_returns') as t:
        t.add_column(sa.Column('reference_no', sa.String(30), nullable=True))
        t.add_column(sa.Column('purchase_item_id', sa.Integer(), nullable=True))
    for (rid,) in conn.execute(sa.text("SELECT id FROM purchase_returns ORDER BY id")).fetchall():
        conn.execute(sa.text("UPDATE purchase_returns SET reference_no = :r WHERE id = :i"), {"r": f"PR-{rid:06d}", "i": rid})
    op.create_index('uq_purchase_returns_reference_no', 'purchase_returns', ['reference_no'], unique=True)

    # the OCR review queue is replaced by the staged review lines above
    op.drop_table('review_queue')

    # new document numbers continue after the ones assigned above
    for key, table, where in (("purchase_ref", "purchases", "WHERE status = 'POSTED'"), ("purchase_return_ref", "purchase_returns", "")):
        top = conn.execute(sa.text(f"SELECT COALESCE(MAX(id), 0) FROM {table} {where}")).scalar() or 0
        conn.execute(sa.text("DELETE FROM number_sequences WHERE key = :k"), {"k": key})
        conn.execute(sa.text("INSERT INTO number_sequences (key, next_value) VALUES (:k, :v)"), {"k": key, "v": top + 1})


def downgrade() -> None:
    op.create_table(
        'review_queue',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('purchase_id', sa.Integer(), sa.ForeignKey('purchases.id'), nullable=True),
        sa.Column('purchase_item_id', sa.Integer(), sa.ForeignKey('purchase_items.id'), nullable=True),
        sa.Column('raw_name', sa.String(250), nullable=False),
        sa.Column('raw_batch', sa.String(60), nullable=False, server_default=''),
        sa.Column('raw_expiry', sa.String(30), nullable=False, server_default=''),
        sa.Column('raw_quantity', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('raw_quantity_free', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('raw_hsn', sa.String(20), nullable=False, server_default=''),
        sa.Column('raw_pack', sa.String(60), nullable=False, server_default=''),
        sa.Column('raw_manufacturer', sa.String(150), nullable=False, server_default=''),
        sa.Column('raw_category', sa.String(60), nullable=False, server_default=''),
        sa.Column('source_serial', sa.Integer(), nullable=True),
        sa.Column('source_page', sa.Integer(), nullable=True),
        sa.Column('review_reasons', sa.String(250), nullable=False, server_default=''),
        sa.Column('raw_rate', sa.Numeric(12, 2), nullable=False, server_default='0'),
        sa.Column('raw_mrp', sa.Numeric(12, 2), nullable=False, server_default='0'),
        sa.Column('best_match_item_id', sa.Integer(), sa.ForeignKey('items.id'), nullable=True),
        sa.Column('confidence', sa.Numeric(5, 2), nullable=False, server_default='0'),
        sa.Column('status', sa.String(20), nullable=False, server_default='PENDING'),
        sa.Column('resolved_by', sa.Integer(), sa.ForeignKey('users.id'), nullable=True),
        sa.Column('resolved_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
    )
    op.drop_index('uq_purchase_returns_reference_no', table_name='purchase_returns')
    with op.batch_alter_table('purchase_returns') as t:
        t.drop_column('purchase_item_id')
        t.drop_column('reference_no')
    op.drop_index('ix_purchase_items_status', table_name='purchase_items')
    with op.batch_alter_table('purchase_items') as t:
        for col in ('batch_id', 'units_per_pack', 'pack_unit', 'base_unit', 'dosage_form', 'category', 'new_product',
                    'match_method', 'discount', 'gst_rate', 'expiry_raw', 'supplier_code', 'status', 'issues',
                    'corrections', 'raw', 'line_no'):
            t.drop_column(col)
    for name in ('uq_purchases_posted_invoice', 'ix_purchases_supplier_invoice', 'ix_purchases_source_sha256',
                 'ix_purchases_status', 'uq_purchases_reference_no'):
        op.drop_index(name, table_name='purchases')
    with op.batch_alter_table('purchases') as t:
        for col in ('cancel_reason', 'cancelled_by', 'cancelled_at', 'posted_by', 'posted_at', 'notes',
                    'source_format', 'supplier_total', 'invoice_date', 'reference_no'):
            t.drop_column(col)
    op.drop_table('supplier_product_maps')
    op.drop_index('uq_suppliers_code', table_name='suppliers')
    with op.batch_alter_table('suppliers') as t:
        for col in ('updated_at', 'credit_days', 'payment_terms', 'email', 'phone', 'code'):
            t.drop_column(col)
