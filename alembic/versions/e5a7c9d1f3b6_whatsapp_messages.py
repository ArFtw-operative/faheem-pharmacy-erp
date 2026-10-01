"""WhatsApp invoices: one delivery record per customer-requested send (queued / sent / failed)

Revision ID: e5a7c9d1f3b6
Revises: d4f6b8c0e2a5
Create Date: 2026-10-01 12:00:00.000000

Additive only: a new table.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'e5a7c9d1f3b6'
down_revision: Union[str, Sequence[str], None] = 'd4f6b8c0e2a5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'whatsapp_messages',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('sale_id', sa.Integer(), sa.ForeignKey('sales.id'), nullable=False),
        sa.Column('invoice_no', sa.String(40), nullable=False, server_default=''),
        sa.Column('customer_phone', sa.String(20), nullable=False),
        sa.Column('status', sa.String(10), nullable=False, server_default='QUEUED'),
        sa.Column('attempt_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('last_error', sa.Text(), nullable=False, server_default=''),
        sa.Column('is_resend', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('message_text', sa.Text(), nullable=False, server_default=''),
        sa.Column('pdf_path', sa.String(255), nullable=False, server_default=''),
        sa.Column('provider', sa.String(20), nullable=False, server_default=''),
        sa.Column('provider_message_id', sa.String(120), nullable=False, server_default=''),
        sa.Column('queued_at', sa.DateTime(), nullable=False, server_default=sa.func.current_timestamp()),
        sa.Column('next_attempt_at', sa.DateTime(), nullable=False, server_default=sa.func.current_timestamp()),
        sa.Column('sent_at', sa.DateTime(), nullable=True),
        sa.Column('created_by', sa.Integer(), sa.ForeignKey('users.id'), nullable=True),
    )
    op.create_index('ix_whatsapp_messages_sale_id', 'whatsapp_messages', ['sale_id'])
    op.create_index('ix_whatsapp_due', 'whatsapp_messages', ['status', 'next_attempt_at'])


def downgrade() -> None:
    raise RuntimeError("Downgrades are not supported: restore the pre-upgrade snapshot (docs/UPGRADES.md)")
