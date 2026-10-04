"""change counters for multi-counter screens (sync_versions)

Revision ID: f6b8d0a2c4e7
Revises: e3a5c7e9b1d4
Create Date: 2026-10-04 22:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'f6b8d0a2c4e7'
down_revision: Union[str, Sequence[str], None] = 'e3a5c7e9b1d4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

AREAS = ("inventory", "purchases", "sales", "customers", "locations", "masters")


def upgrade() -> None:
    op.create_table(
        'sync_versions',
        sa.Column('area', sa.String(20), primary_key=True),
        sa.Column('version', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('changed_at', sa.DateTime(), nullable=True),
    )
    op.bulk_insert(sa.table('sync_versions', sa.column('area', sa.String), sa.column('version', sa.Integer)),
                   [{"area": a, "version": 0} for a in AREAS])


def downgrade() -> None:
    op.drop_table('sync_versions')
