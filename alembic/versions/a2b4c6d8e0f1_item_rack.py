"""item rack location

Revision ID: a2b4c6d8e0f1
Revises: f1a8c3d5e7b2
Create Date: 2026-09-29 20:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a2b4c6d8e0f1'
down_revision: Union[str, Sequence[str], None] = 'f1a8c3d5e7b2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('items') as t:
        t.add_column(sa.Column('rack', sa.String(20), nullable=False, server_default=''))


def downgrade() -> None:
    with op.batch_alter_table('items') as t:
        t.drop_column('rack')
