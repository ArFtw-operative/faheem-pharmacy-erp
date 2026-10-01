"""sale payments (split tender)

Revision ID: d4a1b7e2c9f3
Revises: c7d2e91f4a10
Create Date: 2026-09-28 05:20:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd4a1b7e2c9f3'
down_revision: Union[str, Sequence[str], None] = 'c7d2e91f4a10'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'sale_payments',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('sale_id', sa.Integer(), nullable=False),
        sa.Column('mode', sa.String(length=10), nullable=False),
        sa.Column('amount', sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column('reference', sa.String(length=80), nullable=False, server_default=''),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['sale_id'], ['sales.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_sale_payments_sale_id'), 'sale_payments', ['sale_id'], unique=False)
    op.create_index(op.f('ix_sale_payments_mode'), 'sale_payments', ['mode'], unique=False)
    # Every existing bill was paid in full by its single payment mode.
    op.execute(
        "INSERT INTO sale_payments (sale_id, mode, amount, reference, created_at) "
        "SELECT id, COALESCE(NULLIF(payment_mode, ''), 'CASH'), total, '', created_at FROM sales"
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_sale_payments_mode'), table_name='sale_payments')
    op.drop_index(op.f('ix_sale_payments_sale_id'), table_name='sale_payments')
    op.drop_table('sale_payments')
