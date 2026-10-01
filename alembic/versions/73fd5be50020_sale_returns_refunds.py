"""sale returns refunds

Revision ID: 73fd5be50020
Revises: e974fa779758
Create Date: 2026-09-15 22:19:44.016151

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '73fd5be50020'
down_revision: Union[str, Sequence[str], None] = 'e974fa779758'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute("INSERT OR IGNORE INTO permissions (code, module, description) VALUES "
               "('sales.refund','sales','Process a return / refund against an invoice'),"
               "('sales.refund_override','sales','Approve refunds above threshold or to a different method')")
    for role_name, codes in {
        "Administrator": ["sales.refund", "sales.refund_override"],
        "Manager": ["sales.refund", "sales.refund_override"],
        "Pharmacist": ["sales.refund"],
        "Sales Staff": ["sales.refund"],
        "Counter Manager": ["sales.refund"],
    }.items():
        quoted = ",".join("'%s'" % c for c in codes)
        op.execute("INSERT OR IGNORE INTO role_permissions (role_id, permission_id) "
                   "SELECT r.id, p.id FROM roles r, permissions p "
                   "WHERE r.name = '%s' AND p.code IN (%s)" % (role_name, quoted))

    op.create_table('sale_returns',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('return_no', sa.String(length=40), nullable=False),
    sa.Column('sale_id', sa.Integer(), nullable=False),
    sa.Column('customer_id', sa.Integer(), nullable=True),
    sa.Column('business_date', sa.Date(), nullable=False),
    sa.Column('counter_session_id', sa.Integer(), nullable=True),
    sa.Column('total_refund', sa.Numeric(precision=12, scale=2), nullable=False),
    sa.Column('refund_method', sa.String(length=10), nullable=False),
    sa.Column('reason_code', sa.String(length=40), nullable=False),
    sa.Column('reason_note', sa.Text(), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('processed_by_user_id', sa.Integer(), nullable=True),
    sa.Column('processed_at', sa.DateTime(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['counter_session_id'], ['counter_sessions.id'], ),
    sa.ForeignKeyConstraint(['customer_id'], ['customers.id'], ),
    sa.ForeignKeyConstraint(['processed_by_user_id'], ['users.id'], ),
    sa.ForeignKeyConstraint(['sale_id'], ['sales.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('sale_returns', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_sale_returns_business_date'), ['business_date'], unique=False)
        batch_op.create_index(batch_op.f('ix_sale_returns_return_no'), ['return_no'], unique=True)
        batch_op.create_index(batch_op.f('ix_sale_returns_sale_id'), ['sale_id'], unique=False)

    op.create_table('sale_return_items',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('return_id', sa.Integer(), nullable=False),
    sa.Column('sale_item_id', sa.Integer(), nullable=True),
    sa.Column('item_id', sa.Integer(), nullable=True),
    sa.Column('batch_id', sa.Integer(), nullable=True),
    sa.Column('product_name', sa.String(length=250), nullable=False),
    sa.Column('batch_no', sa.String(length=60), nullable=False),
    sa.Column('expiry_date', sa.Date(), nullable=True),
    sa.Column('quantity', sa.Integer(), nullable=False),
    sa.Column('refund_amount', sa.Numeric(precision=12, scale=2), nullable=False),
    sa.Column('disposition', sa.String(length=20), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['batch_id'], ['batches.id'], ),
    sa.ForeignKeyConstraint(['item_id'], ['items.id'], ),
    sa.ForeignKeyConstraint(['return_id'], ['sale_returns.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['sale_item_id'], ['sale_items.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    # ### end Alembic commands ###


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('sale_return_items')
    with op.batch_alter_table('sale_returns', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_sale_returns_sale_id'))
        batch_op.drop_index(batch_op.f('ix_sale_returns_return_no'))
        batch_op.drop_index(batch_op.f('ix_sale_returns_business_date'))

    op.drop_table('sale_returns')