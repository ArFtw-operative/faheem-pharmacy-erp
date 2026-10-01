"""pos sale drafts

Revision ID: 505a9d11b91f
Revises: 7b29be2ea3e4
Create Date: 2026-09-15 21:14:54.511947

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '505a9d11b91f'
down_revision: Union[str, Sequence[str], None] = '7b29be2ea3e4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('pos_drafts',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('workstation_code', sa.String(length=40), nullable=False),
    sa.Column('counter_id', sa.Integer(), nullable=True),
    sa.Column('counter_session_id', sa.Integer(), nullable=True),
    sa.Column('cashier_user_id', sa.Integer(), nullable=True),
    sa.Column('customer_id', sa.Integer(), nullable=True),
    sa.Column('payload', sa.JSON(), nullable=True),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['cashier_user_id'], ['users.id'], ),
    sa.ForeignKeyConstraint(['counter_id'], ['pos_counters.id'], ),
    sa.ForeignKeyConstraint(['counter_session_id'], ['counter_sessions.id'], ),
    sa.ForeignKeyConstraint(['customer_id'], ['customers.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('pos_drafts', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_pos_drafts_status'), ['status'], unique=False)
        batch_op.create_index(batch_op.f('ix_pos_drafts_workstation_code'), ['workstation_code'], unique=False)
        batch_op.create_index('ix_pos_drafts_ws_status', ['workstation_code', 'status'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('pos_drafts', schema=None) as batch_op:
        batch_op.drop_index('ix_pos_drafts_ws_status')
        batch_op.drop_index(batch_op.f('ix_pos_drafts_workstation_code'))
        batch_op.drop_index(batch_op.f('ix_pos_drafts_status'))

    op.drop_table('pos_drafts')
