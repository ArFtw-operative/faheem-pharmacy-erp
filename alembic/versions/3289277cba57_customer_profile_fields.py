"""customer profile fields

Revision ID: 3289277cba57
Revises: a93b0a7fa7d5
Create Date: 2026-09-15 16:24:16.319930

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '3289277cba57'
down_revision: Union[str, Sequence[str], None] = 'a93b0a7fa7d5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('customers', schema=None) as batch_op:
        batch_op.add_column(sa.Column('alternate_mobile', sa.String(length=20), nullable=False, server_default=''))
        batch_op.add_column(sa.Column('email', sa.String(length=150), nullable=False, server_default=''))
        batch_op.add_column(sa.Column('gender', sa.String(length=20), nullable=False, server_default=''))
        batch_op.add_column(sa.Column('date_of_birth', sa.Date(), nullable=True))
        batch_op.add_column(sa.Column('city', sa.String(length=80), nullable=False, server_default=''))
        batch_op.add_column(sa.Column('state', sa.String(length=80), nullable=False, server_default=''))
        batch_op.add_column(sa.Column('pincode', sa.String(length=12), nullable=False, server_default=''))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('customers', schema=None) as batch_op:
        batch_op.drop_column('pincode')
        batch_op.drop_column('state')
        batch_op.drop_column('city')
        batch_op.drop_column('date_of_birth')
        batch_op.drop_column('gender')
        batch_op.drop_column('email')
        batch_op.drop_column('alternate_mobile')
