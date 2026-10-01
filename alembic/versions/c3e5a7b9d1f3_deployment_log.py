"""release management: deployment log (every upgrade and first start of a version)

Revision ID: c3e5a7b9d1f3
Revises: b2d4f6a8c0e1
Create Date: 2026-09-30 21:00:00.000000

Additive only: a new table, nothing existing is changed.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c3e5a7b9d1f3'
down_revision: Union[str, Sequence[str], None] = 'b2d4f6a8c0e1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'deployment_log',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('deployed_at', sa.String(32), nullable=False),
        sa.Column('app_version', sa.String(32), nullable=False, server_default=''),
        sa.Column('build', sa.String(64), nullable=False, server_default=''),
        sa.Column('status', sa.String(20), nullable=False, server_default=''),
        sa.Column('from_revision', sa.String(200), nullable=False, server_default=''),
        sa.Column('to_revision', sa.String(200), nullable=False, server_default=''),
        sa.Column('backup_name', sa.String(200), nullable=False, server_default=''),
        sa.Column('detail', sa.Text(), nullable=False, server_default=''),
    )


def downgrade() -> None:
    # Deployment history is never dropped automatically; recover with a snapshot instead.
    raise RuntimeError("Downgrades are not supported: restore the pre-upgrade snapshot (see docs/UPGRADES.md)")
