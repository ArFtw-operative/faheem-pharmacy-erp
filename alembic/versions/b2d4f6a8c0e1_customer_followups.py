"""customers: follow-ups (one record behind Inbox, Calendar and activity)

Revision ID: b2d4f6a8c0e1
Revises: a1c3e5f7b9d2
Create Date: 2026-09-30 20:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b2d4f6a8c0e1'
down_revision: Union[str, Sequence[str], None] = 'a1c3e5f7b9d2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'customer_followups',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('customer_id', sa.Integer(), sa.ForeignKey('customers.id'), nullable=False),
        sa.Column('source_sale_id', sa.Integer(), sa.ForeignKey('sales.id'), nullable=True),
        sa.Column('due_date', sa.Date(), nullable=False),
        sa.Column('due_time', sa.String(5), nullable=False, server_default=''),
        sa.Column('reason', sa.String(40), nullable=False, server_default='GENERAL'),
        sa.Column('note', sa.Text(), nullable=False, server_default=''),
        sa.Column('contact_method', sa.String(20), nullable=False, server_default=''),
        sa.Column('status', sa.String(12), nullable=False, server_default='OPEN'),
        sa.Column('reschedule_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('created_by', sa.Integer(), sa.ForeignKey('users.id'), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.func.current_timestamp()),
        sa.Column('completed_by', sa.Integer(), sa.ForeignKey('users.id'), nullable=True),
        sa.Column('completed_at', sa.DateTime(), nullable=True),
        sa.Column('completion_note', sa.Text(), nullable=False, server_default=''),
    )
    op.create_index('ix_followups_due', 'customer_followups', ['status', 'due_date'])
    op.create_index('ix_followups_customer', 'customer_followups', ['customer_id', 'due_date'])
    with op.batch_alter_table('customers') as t:
        t.add_column(sa.Column('reference', sa.String(120), nullable=False, server_default=''))
        t.add_column(sa.Column('updated_at', sa.DateTime(), nullable=True))
    op.create_index('ix_sales_customer_date', 'sales', ['customer_id', 'sale_date'])
    op.create_index('ix_customers_alternate_mobile', 'customers', ['alternate_mobile'])


def downgrade() -> None:
    op.drop_index('ix_customers_alternate_mobile', table_name='customers')
    op.drop_index('ix_sales_customer_date', table_name='sales')
    with op.batch_alter_table('customers') as t:
        t.drop_column('updated_at')
        t.drop_column('reference')
    op.drop_table('customer_followups')
