"""parked sales

Revision ID: e974fa779758
Revises: b3224ebe2382
Create Date: 2026-09-15 22:07:47.839237

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e974fa779758'
down_revision: Union[str, Sequence[str], None] = 'b3224ebe2382'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('parked_sales',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('park_reference', sa.String(length=40), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('customer_id', sa.Integer(), nullable=True),
    sa.Column('business_date', sa.Date(), nullable=False),
    sa.Column('origin_counter_id', sa.Integer(), nullable=True),
    sa.Column('origin_workstation_id', sa.Integer(), nullable=True),
    sa.Column('parked_by_user_id', sa.Integer(), nullable=True),
    sa.Column('parked_at', sa.DateTime(), nullable=False),
    sa.Column('reason_code', sa.String(length=40), nullable=False),
    sa.Column('note', sa.Text(), nullable=False),
    sa.Column('resumed_counter_id', sa.Integer(), nullable=True),
    sa.Column('resumed_workstation_id', sa.Integer(), nullable=True),
    sa.Column('resumed_by_user_id', sa.Integer(), nullable=True),
    sa.Column('resumed_at', sa.DateTime(), nullable=True),
    sa.Column('completed_sale_id', sa.Integer(), nullable=True),
    sa.Column('discarded_by_user_id', sa.Integer(), nullable=True),
    sa.Column('discarded_at', sa.DateTime(), nullable=True),
    sa.Column('discard_reason', sa.String(length=40), nullable=False),
    sa.Column('payload', sa.JSON(), nullable=True),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['completed_sale_id'], ['sales.id'], ),
    sa.ForeignKeyConstraint(['customer_id'], ['customers.id'], ),
    sa.ForeignKeyConstraint(['discarded_by_user_id'], ['users.id'], ),
    sa.ForeignKeyConstraint(['origin_counter_id'], ['pos_counters.id'], ),
    sa.ForeignKeyConstraint(['origin_workstation_id'], ['pos_workstations.id'], ),
    sa.ForeignKeyConstraint(['parked_by_user_id'], ['users.id'], ),
    sa.ForeignKeyConstraint(['resumed_by_user_id'], ['users.id'], ),
    sa.ForeignKeyConstraint(['resumed_counter_id'], ['pos_counters.id'], ),
    sa.ForeignKeyConstraint(['resumed_workstation_id'], ['pos_workstations.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('parked_sales', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_parked_sales_business_date'), ['business_date'], unique=False)
        batch_op.create_index(batch_op.f('ix_parked_sales_park_reference'), ['park_reference'], unique=True)
        batch_op.create_index(batch_op.f('ix_parked_sales_status'), ['status'], unique=False)
        batch_op.create_index('ix_parked_sales_status_date', ['status', 'business_date'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('parked_sales', schema=None) as batch_op:
        batch_op.drop_index('ix_parked_sales_status_date')
        batch_op.drop_index(batch_op.f('ix_parked_sales_status'))
        batch_op.drop_index(batch_op.f('ix_parked_sales_park_reference'))
        batch_op.drop_index(batch_op.f('ix_parked_sales_business_date'))

    op.drop_table('parked_sales')
