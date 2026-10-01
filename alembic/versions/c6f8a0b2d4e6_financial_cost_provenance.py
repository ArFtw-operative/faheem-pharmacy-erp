"""Cost quality and immutable sale/movement financial snapshots.

Revision ID: c6f8a0b2d4e6
Revises: b3c5d7e9f1a2
"""
from alembic import op
import sqlalchemy as sa
revision='c6f8a0b2d4e6'
down_revision='b3c5d7e9f1a2'
branch_labels=None
depends_on=None


def upgrade():
    with op.batch_alter_table('batches') as t:
        t.add_column(sa.Column('cost_status',sa.String(24),nullable=False,server_default='COST_MISSING'))
        t.alter_column('unit_cost',type_=sa.Numeric(18,6),existing_type=sa.Numeric(12,4))
    with op.batch_alter_table('sale_items') as t:
        t.alter_column('cost_rate',type_=sa.Numeric(18,6),existing_type=sa.Numeric(12,4))
        t.add_column(sa.Column('financial_status',sa.String(24),nullable=False,server_default='LEGACY_UNCHECKED'))
        t.add_column(sa.Column('financial_cost_source',sa.String(32),nullable=False,server_default=''))
        t.add_column(sa.Column('reconstructed_unit_cost',sa.Numeric(18,6),nullable=True))
        t.add_column(sa.Column('cost_reconstructed_at',sa.DateTime(),nullable=True))
        t.add_column(sa.Column('cost_reconstruction_version',sa.String(24),nullable=False,server_default=''))
        t.add_column(sa.Column('sale_uom',sa.String(20),nullable=False,server_default='BASE'))
        t.add_column(sa.Column('sale_uom_factor',sa.Integer(),nullable=False,server_default='1'))
        t.add_column(sa.Column('sale_quantity',sa.Numeric(18,6),nullable=True))
        t.add_column(sa.Column('net_sale_value',sa.Numeric(18,2),nullable=True))
        t.add_column(sa.Column('line_cost',sa.Numeric(18,2),nullable=True))
    with op.batch_alter_table('inventory_movements') as t:
        t.add_column(sa.Column('unit_cost_snapshot',sa.Numeric(18,6),nullable=True))
        t.add_column(sa.Column('cost_amount',sa.Numeric(18,2),nullable=True))
        t.add_column(sa.Column('financial_status',sa.String(24),nullable=False,server_default='LEGACY_UNCHECKED'))
    op.create_table('item_uoms',sa.Column('id',sa.Integer(),primary_key=True),sa.Column('item_id',sa.Integer(),sa.ForeignKey('items.id',ondelete='CASCADE'),nullable=False),
        sa.Column('unit',sa.String(20),nullable=False),sa.Column('parent_unit',sa.String(20),nullable=False),sa.Column('factor',sa.Integer(),nullable=False),sa.UniqueConstraint('item_id','unit'))
    # No historical cost is guessed here. The reviewed, idempotent backfill
    # command records evidence separately and never replaces cost_rate.
    op.execute("UPDATE batches SET cost_status='COST_RESOLVED' WHERE purchase_rate > 0")


def downgrade():
    op.drop_table('item_uoms')
    for table, columns in [('inventory_movements',['unit_cost_snapshot','cost_amount','financial_status']),
        ('sale_items',['financial_status','financial_cost_source','reconstructed_unit_cost','cost_reconstructed_at','cost_reconstruction_version','net_sale_value','line_cost','sale_uom','sale_uom_factor','sale_quantity']),('batches',['cost_status'])]:
        with op.batch_alter_table(table) as t:
            for name in columns: t.drop_column(name)
