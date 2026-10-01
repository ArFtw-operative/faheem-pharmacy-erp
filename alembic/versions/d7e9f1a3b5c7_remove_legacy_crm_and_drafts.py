"""remove legacy CRM follow-ups and classic-POS drafts

Revision ID: d7e9f1a3b5c7
Revises: c6f8a0b2d4e6
Create Date: 2026-09-30 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'd7e9f1a3b5c7'
down_revision: Union[str, Sequence[str], None] = 'c6f8a0b2d4e6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # the automatic pre-upgrade backup keeps any rows. The ERP has no follow-up
    # feature, and POS bills live per workspace tab (held bills use parked_sales).
    op.drop_table('followups')
    op.drop_table('pos_drafts')


def downgrade() -> None:
    op.create_table(
        'pos_drafts',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('workstation_code', sa.String(40), nullable=False),
        sa.Column('counter_id', sa.Integer(), sa.ForeignKey('pos_counters.id'), nullable=True),
        sa.Column('counter_session_id', sa.Integer(), sa.ForeignKey('counter_sessions.id'), nullable=True),
        sa.Column('cashier_user_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=True),
        sa.Column('customer_id', sa.Integer(), sa.ForeignKey('customers.id'), nullable=True),
        sa.Column('payload', sa.JSON(), nullable=True),
        sa.Column('status', sa.String(20), nullable=False, server_default='ACTIVE'),
        sa.Column('version', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
    )
    op.create_table(
        'followups',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('customer_id', sa.Integer(), sa.ForeignKey('customers.id', ondelete='CASCADE'), nullable=False),
        sa.Column('due_date', sa.DateTime(), nullable=False),
        sa.Column('note', sa.Text(), nullable=False, server_default=''),
        sa.Column('status', sa.String(20), nullable=False, server_default='PENDING'),
        sa.Column('snooze_until', sa.DateTime(), nullable=True),
        sa.Column('assigned_to', sa.Integer(), sa.ForeignKey('users.id'), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('created_by', sa.Integer(), sa.ForeignKey('users.id'), nullable=True),
        sa.Column('completed_at', sa.DateTime(), nullable=True),
    )
    op.create_index('ix_followups_due_date', 'followups', ['due_date'])
    op.create_index('ix_followups_status', 'followups', ['status'])
