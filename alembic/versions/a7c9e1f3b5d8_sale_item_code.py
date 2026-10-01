"""sale_items.item_code: the code typed on a manual-bill line

Revision ID: a7c9e1f3b5d8
Revises: f6b8d0e2a4c7
Create Date: 2026-10-01 18:00:00.000000

Additive only. Stock lines keep taking their code from the product; a manual-bill line can carry
its own (typed or edited) code.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a7c9e1f3b5d8'
down_revision: Union[str, Sequence[str], None] = 'f6b8d0e2a4c7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('sale_items') as b:
        b.add_column(sa.Column('item_code', sa.String(40), nullable=False, server_default=''))


def downgrade() -> None:
    raise RuntimeError("Downgrades are not supported: restore the pre-upgrade snapshot (docs/UPGRADES.md)")
