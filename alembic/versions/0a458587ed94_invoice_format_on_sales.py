"""invoice_format on sales

Revision ID: 0a458587ed94
Revises: 859d4559094f
Create Date: 2026-09-19 21:07:54.441619

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0a458587ed94'
down_revision: Union[str, Sequence[str], None] = '859d4559094f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('sales', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('invoice_format', sa.String(length=20), nullable=False, server_default='CLASSIC')
        )
        batch_op.create_index(
            batch_op.f('ix_sales_invoice_format'), ['invoice_format'], unique=False
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('sales', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_sales_invoice_format'))
        batch_op.drop_column('invoice_format')
