"""inventory ledger, product packaging, batch unit prices, sale-line snapshots

Every existing batch quantity is carried into the new ledger as one
OPENING_STOCK movement, so ``batches.quantity == SUM(movements)`` holds from
the first moment. Existing products get packaging 1 unit per pack and loose
sale off: their stock keeps exactly the meaning it had (nothing is multiplied).

Revision ID: f1a8c3d5e7b2
Revises: d4a1b7e2c9f3
Create Date: 2026-09-29 18:00:00.000000

"""
import re
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f1a8c3d5e7b2'
down_revision: Union[str, Sequence[str], None] = 'd4a1b7e2c9f3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _norm(value) -> str:
    return re.sub(r"[\s\-_/.]", "", str(value or "")).upper()[:60]


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('items') as t:
        t.add_column(sa.Column('dosage_form', sa.String(20), nullable=False, server_default=''))
        t.add_column(sa.Column('base_unit', sa.String(20), nullable=False, server_default='UNIT'))
        t.add_column(sa.Column('pack_unit', sa.String(20), nullable=False, server_default='PACK'))
        t.add_column(sa.Column('units_per_pack', sa.Integer(), nullable=False, server_default='1'))
        t.add_column(sa.Column('loose_sale', sa.Boolean(), nullable=False, server_default=sa.false()))
        t.add_column(sa.Column('content_qty', sa.Numeric(10, 2), nullable=True))
        t.add_column(sa.Column('content_unit', sa.String(10), nullable=False, server_default=''))
        t.add_column(sa.Column('reorder_level', sa.Integer(), nullable=False, server_default='0'))

    with op.batch_alter_table('batches') as t:
        t.add_column(sa.Column('batch_no_normalized', sa.String(60), nullable=False, server_default=''))
        t.add_column(sa.Column('units_per_pack', sa.Integer(), nullable=False, server_default='1'))
        t.add_column(sa.Column('unit_mrp', sa.Numeric(12, 4), nullable=False, server_default='0'))
        t.add_column(sa.Column('unit_cost', sa.Numeric(12, 4), nullable=False, server_default='0'))

    with op.batch_alter_table('sale_items') as t:
        t.add_column(sa.Column('pack_mrp', sa.Numeric(12, 2), nullable=False, server_default='0'))
        t.add_column(sa.Column('units_per_pack', sa.Integer(), nullable=False, server_default='1'))
        t.add_column(sa.Column('pack_size', sa.String(60), nullable=False, server_default=''))
        t.add_column(sa.Column('base_unit', sa.String(20), nullable=False, server_default='UNIT'))
        t.add_column(sa.Column('line_no', sa.Integer(), nullable=False, server_default='0'))
        t.alter_column('cost_rate', type_=sa.Numeric(12, 4), existing_type=sa.Numeric(12, 2))

    op.create_table(
        'inventory_movements',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('item_id', sa.Integer(), nullable=False),
        sa.Column('batch_id', sa.Integer(), nullable=False),
        sa.Column('movement_type', sa.String(24), nullable=False),
        sa.Column('quantity', sa.Integer(), nullable=False),
        sa.Column('balance_after', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('txn_quantity', sa.Integer(), nullable=True),
        sa.Column('txn_unit', sa.String(10), nullable=False, server_default='BASE'),
        sa.Column('units_per_pack', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('reference_type', sa.String(24), nullable=False, server_default=''),
        sa.Column('reference_id', sa.Integer(), nullable=True),
        sa.Column('reference_no', sa.String(60), nullable=False, server_default=''),
        sa.Column('reason', sa.Text(), nullable=False, server_default=''),
        sa.Column('reversal_of_id', sa.Integer(), nullable=True),
        sa.Column('user_id', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['item_id'], ['items.id']),
        sa.ForeignKeyConstraint(['batch_id'], ['batches.id']),
        sa.ForeignKeyConstraint(['reversal_of_id'], ['inventory_movements.id']),
        sa.ForeignKeyConstraint(['user_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_inventory_movements_movement_type', 'inventory_movements', ['movement_type'])
    op.create_index('ix_inventory_movements_reversal_of_id', 'inventory_movements', ['reversal_of_id'])
    op.create_index('ix_movements_item_time', 'inventory_movements', ['item_id', 'created_at'])
    op.create_index('ix_movements_batch', 'inventory_movements', ['batch_id', 'id'])
    op.create_index('ix_movements_reference', 'inventory_movements', ['reference_type', 'reference_id'])
    op.create_index('ix_batches_item_stock', 'batches', ['item_id', 'quantity', 'expiry_date'])
    op.create_index('ix_batches_batch_no_normalized', 'batches', ['batch_no_normalized'])

    conn = op.get_bind()
    # unit prices (1 unit per pack for every existing batch) and batch keys
    conn.execute(sa.text("UPDATE batches SET unit_mrp = mrp, unit_cost = purchase_rate, units_per_pack = 1"))
    conn.execute(sa.text("UPDATE sale_items SET pack_mrp = mrp"))
    rows = conn.execute(sa.text("SELECT id, item_id, batch_no FROM batches ORDER BY id")).fetchall()
    taken: set[tuple[int, str]] = set()
    for batch_id, item_id, batch_no in rows:
        key = _norm(batch_no)
        # legacy duplicates (same number, different expiry) keep a unique key
        # so the partial unique index can be built; they surface as conflicts
        # only if new stock arrives for that number.
        if key and (item_id, key) in taken:
            key = f"{key}#{batch_id}"[:60]
        taken.add((item_id, key))
        conn.execute(sa.text("UPDATE batches SET batch_no_normalized = :k WHERE id = :i"), {"k": key, "i": batch_id})
    op.create_index('uq_batch_item_normalized', 'batches', ['item_id', 'batch_no_normalized'], unique=True,
                    sqlite_where=sa.text("batch_no_normalized != ''"))

    # the ledger starts from the stock on hand today
    conn.execute(sa.text(
        "INSERT INTO inventory_movements (item_id, batch_id, movement_type, quantity, balance_after, "
        "txn_quantity, txn_unit, units_per_pack, reference_type, reference_no, reason, created_at) "
        "SELECT item_id, id, 'OPENING_STOCK', quantity, quantity, quantity, 'BASE', 1, 'MIGRATION', "
        "'LEDGER-START', 'Balance carried into the stock ledger', CURRENT_TIMESTAMP "
        "FROM batches WHERE quantity > 0"
    ))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('uq_batch_item_normalized', table_name='batches')
    op.drop_index('ix_batches_batch_no_normalized', table_name='batches')
    op.drop_index('ix_batches_item_stock', table_name='batches')
    op.drop_table('inventory_movements')
    with op.batch_alter_table('sale_items') as t:
        for col in ('line_no', 'base_unit', 'pack_size', 'units_per_pack', 'pack_mrp'):
            t.drop_column(col)
    with op.batch_alter_table('batches') as t:
        for col in ('unit_cost', 'unit_mrp', 'units_per_pack', 'batch_no_normalized'):
            t.drop_column(col)
    with op.batch_alter_table('items') as t:
        for col in ('reorder_level', 'content_unit', 'content_qty', 'loose_sale', 'units_per_pack',
                    'pack_unit', 'base_unit', 'dosage_form'):
            t.drop_column(col)
