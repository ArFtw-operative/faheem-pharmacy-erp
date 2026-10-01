"""remove the counter / cash-drawer mechanism

Counters, workstations, counter days (sessions) and the cash ledger are
removed, together with the counter links on sales, returns and held bills.
Sales keep their payments, received and change amounts.

Revision ID: f8a0b2c4d6e8
Revises: e7f9a1b3c5d7
Create Date: 2026-09-30 15:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'f8a0b2c4d6e8'
down_revision: Union[str, Sequence[str], None] = 'e7f9a1b3c5d7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _drop_index(name: str, table: str) -> None:
    conn = op.get_bind()
    if conn.execute(sa.text("SELECT 1 FROM sqlite_master WHERE type='index' AND name=:n"), {"n": name}).first():
        op.drop_index(name, table_name=table)


def upgrade() -> None:
    _drop_index('ix_sales_counter_session_id', 'sales')
    with op.batch_alter_table('sales') as t:
        t.drop_column('counter_session_id')
    with op.batch_alter_table('sale_returns') as t:
        t.drop_column('counter_session_id')
    with op.batch_alter_table('parked_sales') as t:
        for col in ('origin_counter_id', 'origin_workstation_id', 'resumed_counter_id', 'resumed_workstation_id'):
            t.drop_column(col)
    for table in ('cash_ledger_entries', 'counter_sessions', 'pos_workstations', 'pos_counters'):
        op.execute(f"DROP TABLE IF EXISTS {table}")
    op.execute("DELETE FROM settings WHERE key = 'counter_closing_window_minutes'")
    op.execute("DELETE FROM role_permissions WHERE permission_id IN (SELECT id FROM permissions WHERE code LIKE 'counter.%')")
    op.execute("DELETE FROM permissions WHERE code LIKE 'counter.%'")


def downgrade() -> None:
    raise RuntimeError("The counter mechanism was removed; restore the pre-upgrade backup to go back.")
