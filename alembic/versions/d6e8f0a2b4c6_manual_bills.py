"""sales: manual bills (invoice_type MANUAL — no stock ledger)

Revision ID: d6e8f0a2b4c6
Revises: c5d7e9f1a3b5
Create Date: 2026-09-30 11:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'd6e8f0a2b4c6'
down_revision: Union[str, Sequence[str], None] = 'c5d7e9f1a3b5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('sales') as t:
        t.add_column(sa.Column('invoice_type', sa.String(10), nullable=False, server_default='INVENTORY'))
    op.create_index('ix_sales_invoice_type', 'sales', ['invoice_type'])


def downgrade() -> None:
    op.drop_index('ix_sales_invoice_type', table_name='sales')
    with op.batch_alter_table('sales') as t:
        t.drop_column('invoice_type')
