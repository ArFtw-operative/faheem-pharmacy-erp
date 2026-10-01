"""cash ledger running balance

Revision ID: 7b29be2ea3e4
Revises: a5e4fd210bf1
Create Date: 2026-09-15 17:44:11.966579

"""
from collections import defaultdict
from decimal import Decimal
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '7b29be2ea3e4'
down_revision: Union[str, Sequence[str], None] = 'a5e4fd210bf1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema and backfill the running balance for existing entries."""
    with op.batch_alter_table('cash_ledger_entries', schema=None) as batch_op:
        batch_op.add_column(sa.Column('balance_before', sa.Numeric(precision=12, scale=2), nullable=True))
        batch_op.add_column(sa.Column('balance_after', sa.Numeric(precision=12, scale=2), nullable=True))

    conn = op.get_bind()
    rows = conn.execute(sa.text(
        "SELECT id, counter_session_id, cash_in, cash_out FROM cash_ledger_entries "
        "ORDER BY counter_session_id, created_at, id"
    )).fetchall()
    running: dict[int, Decimal] = defaultdict(lambda: Decimal("0.00"))
    for row in rows:
        before = running[row[1]]
        after = (before + Decimal(str(row[2])) - Decimal(str(row[3]))).quantize(Decimal("0.01"))
        conn.execute(
            sa.text("UPDATE cash_ledger_entries SET balance_before=:b, balance_after=:a WHERE id=:i"),
            {"b": str(before), "a": str(after), "i": row[0]},
        )
        running[row[1]] = after


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('cash_ledger_entries', schema=None) as batch_op:
        batch_op.drop_column('balance_after')
        batch_op.drop_column('balance_before')
