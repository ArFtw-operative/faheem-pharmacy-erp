"""counter cash drawer ledger

Revision ID: a93b0a7fa7d5
Revises: 06d341a835c2
Create Date: 2026-09-15 15:46:29.765771

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a93b0a7fa7d5'
down_revision: Union[str, Sequence[str], None] = '06d341a835c2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


COUNTER_PERMISSIONS = [
    ("counter.view", "counter", "View the current counter session"),
    ("counter.open", "counter", "Open a counter session"),
    ("counter.close", "counter", "Close and reconcile a counter session"),
    ("counter.movement", "counter", "Record manual cash in/out"),
    ("counter.refund", "counter", "Record a cash refund"),
    ("counter.view_history", "counter", "View counter history across dates"),
    ("counter.manage", "counter", "Create counters and correct closed sessions"),
]

# role name -> counter permission codes granted to existing installations
ROLE_GRANTS = {
    "Administrator": [p[0] for p in COUNTER_PERMISSIONS],
    "Manager": [p[0] for p in COUNTER_PERMISSIONS],
    "Pharmacist": [
        "counter.view", "counter.open", "counter.close",
        "counter.movement", "counter.refund", "counter.view_history",
    ],
    "Sales Staff": ["counter.view", "counter.open", "counter.close", "counter.movement"],
    "Accountant": ["counter.view", "counter.view_history"],
}


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('pos_counters',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('code', sa.String(length=30), nullable=False),
    sa.Column('name', sa.String(length=120), nullable=False),
    sa.Column('location_id', sa.Integer(), nullable=True),
    sa.Column('is_active', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('pos_counters', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_pos_counters_code'), ['code'], unique=True)

    op.create_table('counter_sessions',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('counter_id', sa.Integer(), nullable=False),
    sa.Column('business_date', sa.Date(), nullable=False),
    sa.Column('opened_at', sa.DateTime(), nullable=False),
    sa.Column('opened_by_user_id', sa.Integer(), nullable=True),
    sa.Column('opening_cash', sa.Numeric(precision=12, scale=2), nullable=False),
    sa.Column('status', sa.String(length=10), nullable=False),
    sa.Column('closed_at', sa.DateTime(), nullable=True),
    sa.Column('closed_by_user_id', sa.Integer(), nullable=True),
    sa.Column('expected_closing_cash', sa.Numeric(precision=12, scale=2), nullable=True),
    sa.Column('actual_closing_cash', sa.Numeric(precision=12, scale=2), nullable=True),
    sa.Column('variance', sa.Numeric(precision=12, scale=2), nullable=True),
    sa.Column('opening_note', sa.Text(), nullable=False),
    sa.Column('closing_note', sa.Text(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['closed_by_user_id'], ['users.id'], ),
    sa.ForeignKeyConstraint(['counter_id'], ['pos_counters.id'], ),
    sa.ForeignKeyConstraint(['opened_by_user_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('counter_sessions', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_counter_sessions_business_date'), ['business_date'], unique=False)
        batch_op.create_index('ix_counter_sessions_counter_date', ['counter_id', 'business_date'], unique=False)
        batch_op.create_index(batch_op.f('ix_counter_sessions_status'), ['status'], unique=False)

    op.create_table('cash_ledger_entries',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('counter_session_id', sa.Integer(), nullable=False),
    sa.Column('entry_type', sa.String(length=20), nullable=False),
    sa.Column('reference_type', sa.String(length=30), nullable=False),
    sa.Column('reference_id', sa.Integer(), nullable=True),
    sa.Column('cash_in', sa.Numeric(precision=12, scale=2), nullable=False),
    sa.Column('cash_out', sa.Numeric(precision=12, scale=2), nullable=False),
    sa.Column('tendered_amount', sa.Numeric(precision=12, scale=2), nullable=True),
    sa.Column('change_amount', sa.Numeric(precision=12, scale=2), nullable=True),
    sa.Column('reason_code', sa.String(length=40), nullable=False),
    sa.Column('note', sa.Text(), nullable=False),
    sa.Column('created_by_user_id', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('reversal_of_entry_id', sa.Integer(), nullable=True),
    sa.ForeignKeyConstraint(['counter_session_id'], ['counter_sessions.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['created_by_user_id'], ['users.id'], ),
    sa.ForeignKeyConstraint(['reversal_of_entry_id'], ['cash_ledger_entries.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('cash_ledger_entries', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_cash_ledger_entries_created_at'), ['created_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_cash_ledger_entries_entry_type'), ['entry_type'], unique=False)
        batch_op.create_index('ix_cash_ledger_session_time', ['counter_session_id', 'created_at'], unique=False)

    with op.batch_alter_table('sales', schema=None) as batch_op:
        batch_op.add_column(sa.Column('counter_session_id', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('tendered_amount', sa.Numeric(precision=12, scale=2), nullable=True))
        batch_op.add_column(sa.Column('change_amount', sa.Numeric(precision=12, scale=2), nullable=True))
        batch_op.add_column(sa.Column('client_request_id', sa.String(length=64), nullable=True))
        batch_op.create_index(batch_op.f('ix_sales_client_request_id'), ['client_request_id'], unique=True)
        batch_op.create_foreign_key(
            'fk_sales_counter_session', 'counter_sessions', ['counter_session_id'], ['id']
        )

    # Grant the new counter permissions to existing installations. On a fresh
    # database roles do not exist yet, so these no-op and the seed supplies them.
    values = ",\n".join(
        "('%s','%s','%s')" % (code, module, desc) for code, module, desc in COUNTER_PERMISSIONS
    )
    op.execute("INSERT OR IGNORE INTO permissions (code, module, description) VALUES " + values)
    for role_name, codes in ROLE_GRANTS.items():
        quoted = ",".join("'%s'" % c for c in codes)
        op.execute(
            "INSERT OR IGNORE INTO role_permissions (role_id, permission_id) "
            "SELECT r.id, p.id FROM roles r, permissions p "
            "WHERE r.name = '%s' AND p.code IN (%s)" % (role_name, quoted)
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('sales', schema=None) as batch_op:
        batch_op.drop_constraint('fk_sales_counter_session', type_='foreignkey')
        batch_op.drop_index(batch_op.f('ix_sales_client_request_id'))
        batch_op.drop_column('client_request_id')
        batch_op.drop_column('change_amount')
        batch_op.drop_column('tendered_amount')
        batch_op.drop_column('counter_session_id')

    with op.batch_alter_table('cash_ledger_entries', schema=None) as batch_op:
        batch_op.drop_index('ix_cash_ledger_session_time')
        batch_op.drop_index(batch_op.f('ix_cash_ledger_entries_entry_type'))
        batch_op.drop_index(batch_op.f('ix_cash_ledger_entries_created_at'))

    op.drop_table('cash_ledger_entries')
    with op.batch_alter_table('counter_sessions', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_counter_sessions_status'))
        batch_op.drop_index('ix_counter_sessions_counter_date')
        batch_op.drop_index(batch_op.f('ix_counter_sessions_business_date'))

    op.drop_table('counter_sessions')
    with op.batch_alter_table('pos_counters', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_pos_counters_code'))

    op.drop_table('pos_counters')
