"""invoice line item-master fields (hsn, pack, manufacturer, category)

Revision ID: c7d2e91f4a10
Revises: 0a458587ed94
Create Date: 2026-09-27 21:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c7d2e91f4a10'
down_revision: Union[str, Sequence[str], None] = '0a458587ed94'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('purchase_items', schema=None) as batch_op:
        batch_op.add_column(sa.Column('hsn_code', sa.String(length=20), nullable=False, server_default=''))
        batch_op.add_column(sa.Column('pack_size', sa.String(length=60), nullable=False, server_default=''))
        batch_op.add_column(sa.Column('manufacturer', sa.String(length=150), nullable=False, server_default=''))
    with op.batch_alter_table('review_queue', schema=None) as batch_op:
        batch_op.add_column(sa.Column('raw_hsn', sa.String(length=20), nullable=False, server_default=''))
        batch_op.add_column(sa.Column('raw_pack', sa.String(length=60), nullable=False, server_default=''))
        batch_op.add_column(sa.Column('raw_manufacturer', sa.String(length=150), nullable=False, server_default=''))
        batch_op.add_column(sa.Column('raw_category', sa.String(length=60), nullable=False, server_default=''))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('review_queue', schema=None) as batch_op:
        batch_op.drop_column('raw_category')
        batch_op.drop_column('raw_manufacturer')
        batch_op.drop_column('raw_pack')
        batch_op.drop_column('raw_hsn')
    with op.batch_alter_table('purchase_items', schema=None) as batch_op:
        batch_op.drop_column('manufacturer')
        batch_op.drop_column('pack_size')
        batch_op.drop_column('hsn_code')
