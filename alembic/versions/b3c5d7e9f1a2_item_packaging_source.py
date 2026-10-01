"""item packaging source (DEFAULT / AUTO / MANUAL)

Existing products start as DEFAULT; the app configures their unit of measure
automatically at startup (see packaging_service.auto_configure_pending).

Revision ID: b3c5d7e9f1a2
Revises: a2b4c6d8e0f1
Create Date: 2026-09-30 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b3c5d7e9f1a2'
down_revision: Union[str, Sequence[str], None] = 'a2b4c6d8e0f1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('items') as t:
        t.add_column(sa.Column('packaging_source', sa.String(10), nullable=False, server_default='DEFAULT'))
    op.create_index('ix_items_packaging_source', 'items', ['packaging_source'])


def downgrade() -> None:
    op.drop_index('ix_items_packaging_source', table_name='items')
    with op.batch_alter_table('items') as t:
        t.drop_column('packaging_source')
