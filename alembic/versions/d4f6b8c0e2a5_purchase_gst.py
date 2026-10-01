"""purchases: GST snapshot per line (taxable, CGST/SGST/IGST, landed rate incl. GST) and batch cost basis

Revision ID: d4f6b8c0e2a5
Revises: c3e5a7b9d1f3
Create Date: 2026-10-01 10:00:00.000000

Additive only. Existing lines and batches keep their values; the new columns are
filled for open (unposted) lines when they are next checked, and for every line
posted from now on. Batches received before this release keep an empty basis
("recorded before GST tracking"), so their cost is never reinterpreted.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'd4f6b8c0e2a5'
down_revision: Union[str, Sequence[str], None] = 'c3e5a7b9d1f3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('purchase_items') as b:
        b.add_column(sa.Column('taxable_value', sa.Numeric(12, 2), nullable=True))
        b.add_column(sa.Column('gst_amount', sa.Numeric(12, 2), nullable=True))
        b.add_column(sa.Column('cgst_amount', sa.Numeric(12, 2), nullable=True))
        b.add_column(sa.Column('sgst_amount', sa.Numeric(12, 2), nullable=True))
        b.add_column(sa.Column('igst_amount', sa.Numeric(12, 2), nullable=True))
        b.add_column(sa.Column('landed_total', sa.Numeric(12, 2), nullable=True))
        b.add_column(sa.Column('landed_rate', sa.Numeric(12, 2), nullable=True))
        b.add_column(sa.Column('gst_source', sa.String(20), nullable=False, server_default=''))
    with op.batch_alter_table('purchases') as b:
        b.add_column(sa.Column('supply_type', sa.String(8), nullable=False, server_default=''))
    with op.batch_alter_table('batches') as b:
        b.add_column(sa.Column('rate_basis', sa.String(12), nullable=False, server_default=''))


def downgrade() -> None:
    raise RuntimeError("Downgrades are not supported: restore the pre-upgrade snapshot (docs/UPGRADES.md)")
