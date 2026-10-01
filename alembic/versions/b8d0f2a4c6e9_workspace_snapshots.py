"""workspace_snapshots: open tabs and unfinished bills per user and counter (crash recovery)

Revision ID: b8d0f2a4c6e9
Revises: a7c9e1f3b5d8
Create Date: 2026-10-01 20:00:00.000000

Additive only.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b8d0f2a4c6e9'
down_revision: Union[str, Sequence[str], None] = 'a7c9e1f3b5d8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'workspace_snapshots',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('terminal', sa.String(64), nullable=False, server_default=''),
        sa.Column('data', sa.Text(), nullable=False, server_default='{}'),
        sa.Column('saved_at', sa.DateTime(), nullable=False),
        sa.UniqueConstraint('user_id', 'terminal', name='uq_workspace_user_terminal'),
    )
    op.create_index('ix_workspace_snapshots_user_id', 'workspace_snapshots', ['user_id'])


def downgrade() -> None:
    raise RuntimeError("Downgrades are not supported: restore the pre-upgrade snapshot (docs/UPGRADES.md)")
