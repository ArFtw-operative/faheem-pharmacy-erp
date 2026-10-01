"""sale item cost rate for pnl

Revision ID: 48f48dc87018
Revises: 5920d14ad5a9
Create Date: 2026-09-16 14:10:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '48f48dc87018'
down_revision: Union[str, Sequence[str], None] = '5920d14ad5a9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Capture the batch purchase rate on each sale line for immutable P&L."""
    with op.batch_alter_table('sale_items', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('cost_rate', sa.Numeric(precision=12, scale=2), nullable=False, server_default='0')
        )
    # Backfill historical lines from the batch purchase rate where available.
    op.execute(
        "UPDATE sale_items SET cost_rate = COALESCE("
        "(SELECT purchase_rate FROM batches WHERE batches.id = sale_items.batch_id), 0)"
    )


def downgrade() -> None:
    with op.batch_alter_table('sale_items', schema=None) as batch_op:
        batch_op.drop_column('cost_rate')
