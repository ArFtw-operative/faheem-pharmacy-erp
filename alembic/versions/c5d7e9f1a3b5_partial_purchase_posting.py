"""purchases: post selected lines (partial receipt)

A purchase can be received line by line: PARTIAL while some lines are still
under review, POSTED when every line is in (or the rest were closed as not
received). The same supplier invoice may exist only once among PARTIAL and
POSTED documents. ``difference_ack`` records that a person accepted a
supplier-total difference.

Revision ID: c5d7e9f1a3b5
Revises: b4c6d8e0f2a4
Create Date: 2026-09-30 09:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c5d7e9f1a3b5'
down_revision: Union[str, Sequence[str], None] = 'b4c6d8e0f2a4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_index('uq_purchases_posted_invoice', table_name='purchases')
    op.create_index('uq_purchases_posted_invoice', 'purchases', ['supplier_id', 'invoice_no'], unique=True,
                    sqlite_where=sa.text("status IN ('POSTED', 'PARTIAL') AND invoice_no != ''"))
    with op.batch_alter_table('purchases') as t:
        t.add_column(sa.Column('difference_ack', sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade() -> None:
    with op.batch_alter_table('purchases') as t:
        t.drop_column('difference_ack')
    op.drop_index('uq_purchases_posted_invoice', table_name='purchases')
    op.create_index('uq_purchases_posted_invoice', 'purchases', ['supplier_id', 'invoice_no'], unique=True,
                    sqlite_where=sa.text("status = 'POSTED' AND invoice_no != ''"))
