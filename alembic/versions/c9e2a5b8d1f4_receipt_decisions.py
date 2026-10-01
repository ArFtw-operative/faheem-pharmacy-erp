"""Persist invoice conversion decisions and supplier convention evidence."""
from alembic import op
import sqlalchemy as sa

revision = "c9e2a5b8d1f4"
down_revision = "b8d0f2a4c6e9"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("purchase_items", sa.Column("receipt_decision", sa.JSON(), nullable=True))
    op.add_column("supplier_product_maps", sa.Column("receipt_conventions", sa.JSON(), nullable=True))


def downgrade():
    op.drop_column("supplier_product_maps", "receipt_conventions")
    op.drop_column("purchase_items", "receipt_decision")
