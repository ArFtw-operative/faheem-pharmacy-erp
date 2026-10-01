"""customer_type on customers and sales

Revision ID: 859d4559094f
Revises: 39376a3245ee
Create Date: 2026-09-19 18:25:42.232654

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '859d4559094f'
down_revision: Union[str, Sequence[str], None] = '39376a3245ee'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('customers', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('customer_type', sa.String(length=20), nullable=False, server_default='WALK_IN')
        )
        batch_op.create_index(
            batch_op.f('ix_customers_customer_type'), ['customer_type'], unique=False
        )

    with op.batch_alter_table('sales', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('customer_type', sa.String(length=20), nullable=False, server_default='WALK_IN')
        )
        batch_op.create_index(
            batch_op.f('ix_sales_customer_type'), ['customer_type'], unique=False
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('sales', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_sales_customer_type'))
        batch_op.drop_column('customer_type')

    with op.batch_alter_table('customers', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_customers_customer_type'))
        batch_op.drop_column('customer_type')
