"""stock history index; purchase invoice charges and column mapping

The ledger is indexed by time for the all-products stock history. Purchases
keep the invoice-level charges read from the supplier file (round off,
freight, adjustments, credit/debit notes, bill discount %) and the column
mapping the importer worked out, so reviewers can see where each value came
from.

Revision ID: b4c6d8e0f2a4
Revises: a3b5c7d9e1f3
Create Date: 2026-09-30 01:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b4c6d8e0f2a4'
down_revision: Union[str, Sequence[str], None] = 'a3b5c7d9e1f3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index('ix_movements_time', 'inventory_movements', ['created_at', 'id'])
    with op.batch_alter_table('purchases') as t:
        t.add_column(sa.Column('charges', sa.JSON(), nullable=True))
        t.add_column(sa.Column('column_map', sa.JSON(), nullable=True))
    with op.batch_alter_table('suppliers') as t:
        t.add_column(sa.Column('column_profile', sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('suppliers') as t:
        t.drop_column('column_profile')
    with op.batch_alter_table('purchases') as t:
        t.drop_column('column_map')
        t.drop_column('charges')
    op.drop_index('ix_movements_time', table_name='inventory_movements')
