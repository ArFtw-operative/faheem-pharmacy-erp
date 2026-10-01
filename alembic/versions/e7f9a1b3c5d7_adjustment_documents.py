"""stock adjustments become numbered, immutable documents with reversals

Adds ADJ-000001 numbering, direction (IN/OUT), the ledger movement it
posted, and reversal links. Existing write-offs are numbered in id order.

Revision ID: e7f9a1b3c5d7
Revises: d6e8f0a2b4c6
Create Date: 2026-09-30 13:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'e7f9a1b3c5d7'
down_revision: Union[str, Sequence[str], None] = 'd6e8f0a2b4c6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('stock_adjustments') as t:
        t.add_column(sa.Column('reference_no', sa.String(20), nullable=True))
        t.add_column(sa.Column('direction', sa.String(3), nullable=False, server_default='OUT'))
        t.add_column(sa.Column('movement_id', sa.Integer(), nullable=True))
        t.add_column(sa.Column('reversal_of_id', sa.Integer(), nullable=True))
        t.add_column(sa.Column('reversed_at', sa.DateTime(), nullable=True))
        t.add_column(sa.Column('reversed_by', sa.Integer(), nullable=True))
    conn = op.get_bind()
    for (aid,) in conn.execute(sa.text("SELECT id FROM stock_adjustments ORDER BY id")).fetchall():
        conn.execute(sa.text("UPDATE stock_adjustments SET reference_no = :r WHERE id = :i"), {"r": f"ADJ-{aid:06d}", "i": aid})
        conn.execute(sa.text(
            "UPDATE stock_adjustments SET movement_id = (SELECT m.id FROM inventory_movements m "
            "WHERE m.reference_type = 'ADJUSTMENT' AND m.reference_id = :i ORDER BY m.id LIMIT 1) WHERE id = :i"), {"i": aid})
    top = conn.execute(sa.text("SELECT COALESCE(MAX(id), 0) FROM stock_adjustments")).scalar() or 0
    conn.execute(sa.text("DELETE FROM number_sequences WHERE key = 'adjustment_ref'"))
    conn.execute(sa.text("INSERT INTO number_sequences (key, next_value) VALUES ('adjustment_ref', :v)"), {"v": top + 1})
    op.create_index('uq_stock_adjustments_reference_no', 'stock_adjustments', ['reference_no'], unique=True)
    op.create_index('ix_stock_adjustments_item', 'stock_adjustments', ['item_id', 'adjustment_date'])


def downgrade() -> None:
    op.drop_index('ix_stock_adjustments_item', table_name='stock_adjustments')
    op.drop_index('uq_stock_adjustments_reference_no', table_name='stock_adjustments')
    with op.batch_alter_table('stock_adjustments') as t:
        for col in ('reversed_by', 'reversed_at', 'reversal_of_id', 'movement_id', 'direction', 'reference_no'):
            t.drop_column(col)
