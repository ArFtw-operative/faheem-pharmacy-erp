"""counter closing reconciliation

Revision ID: b3224ebe2382
Revises: 505a9d11b91f
Create Date: 2026-09-15 21:38:04.244975

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b3224ebe2382'
down_revision: Union[str, Sequence[str], None] = '505a9d11b91f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('counter_sessions', schema=None) as batch_op:
        batch_op.add_column(sa.Column('variance_reason', sa.Text(), nullable=False, server_default=''))
        batch_op.add_column(sa.Column('disposition', sa.String(length=20), nullable=True))
        batch_op.add_column(sa.Column('deposit_amount', sa.Numeric(precision=12, scale=2), nullable=True))
        batch_op.add_column(sa.Column('retained_float', sa.Numeric(precision=12, scale=2), nullable=True))
        batch_op.add_column(sa.Column('deposit_reference', sa.String(length=120), nullable=False, server_default=''))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('counter_sessions', schema=None) as batch_op:
        batch_op.drop_column('deposit_reference')
        batch_op.drop_column('retained_float')
        batch_op.drop_column('deposit_amount')
        batch_op.drop_column('disposition')
        batch_op.drop_column('variance_reason')
