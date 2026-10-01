"""users: two-factor authentication (TOTP), recovery codes and login lock-out

Revision ID: f6b8d0e2a4c7
Revises: e5a7c9d1f3b6
Create Date: 2026-10-01 14:00:00.000000

Additive only.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'f6b8d0e2a4c7'
down_revision: Union[str, Sequence[str], None] = 'e5a7c9d1f3b6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('users') as b:
        b.add_column(sa.Column('mfa_secret', sa.Text(), nullable=False, server_default=''))
        b.add_column(sa.Column('mfa_enabled', sa.Boolean(), nullable=False, server_default=sa.false()))
        b.add_column(sa.Column('mfa_enrolled_at', sa.DateTime(), nullable=True))
        b.add_column(sa.Column('mfa_last_step', sa.Integer(), nullable=True))
        b.add_column(sa.Column('mfa_recovery', sa.JSON(), nullable=True))
        b.add_column(sa.Column('failed_logins', sa.Integer(), nullable=False, server_default='0'))
        b.add_column(sa.Column('locked_until', sa.DateTime(), nullable=True))
        b.add_column(sa.Column('password_changed_at', sa.DateTime(), nullable=True))


def downgrade() -> None:
    raise RuntimeError("Downgrades are not supported: restore the pre-upgrade snapshot (docs/UPGRADES.md)")
