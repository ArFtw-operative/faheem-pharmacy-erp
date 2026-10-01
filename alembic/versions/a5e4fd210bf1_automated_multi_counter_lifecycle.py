"""automated multi-counter lifecycle

Revision ID: a5e4fd210bf1
Revises: 3289277cba57
Create Date: 2026-09-15 16:46:45.853031

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a5e4fd210bf1'
down_revision: Union[str, Sequence[str], None] = '3289277cba57'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


NEW_PERMISSIONS = [
    ("counter.assign_workstation", "counter", "Assign a workstation to a counter"),
    ("counter.reconcile", "counter", "Record a physical cash count for a session"),
    ("counter.view_all", "counter", "View all counters and the aggregate summary"),
]
ROLE_GRANTS = {
    "Administrator": ["counter.assign_workstation", "counter.reconcile", "counter.view_all"],
    "Manager": ["counter.assign_workstation", "counter.reconcile", "counter.view_all"],
    "Pharmacist": ["counter.reconcile", "counter.view_all"],
    "Accountant": ["counter.view_all"],
}


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('pos_workstations',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('workstation_code', sa.String(length=40), nullable=False),
    sa.Column('display_name', sa.String(length=120), nullable=False),
    sa.Column('counter_id', sa.Integer(), nullable=True),
    sa.Column('is_active', sa.Boolean(), nullable=False),
    sa.Column('assigned_at', sa.DateTime(), nullable=True),
    sa.Column('assigned_by', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['assigned_by'], ['users.id'], ),
    sa.ForeignKeyConstraint(['counter_id'], ['pos_counters.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('pos_workstations', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_pos_workstations_workstation_code'), ['workstation_code'], unique=True)

    with op.batch_alter_table('counter_sessions', schema=None) as batch_op:
        batch_op.add_column(sa.Column('open_type', sa.String(length=20), nullable=False, server_default='AUTOMATIC'))
        batch_op.add_column(sa.Column('scheduled_close_at', sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column('close_type', sa.String(length=20), nullable=True))
        batch_op.add_column(sa.Column('reconciliation_status', sa.String(length=20), nullable=False, server_default='PENDING'))
        batch_op.add_column(sa.Column('reconciled_at', sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column('reconciled_by_user_id', sa.Integer(), nullable=True))
        batch_op.create_unique_constraint('uq_counter_session_date', ['counter_id', 'business_date'])
        batch_op.create_foreign_key(
            'fk_counter_sessions_reconciled_by', 'users', ['reconciled_by_user_id'], ['id']
        )

    with op.batch_alter_table('pos_counters', schema=None) as batch_op:
        batch_op.add_column(sa.Column('description', sa.String(length=255), nullable=False, server_default=''))
        batch_op.add_column(sa.Column('default_opening_float', sa.Numeric(precision=12, scale=2), nullable=False, server_default='0'))
        batch_op.add_column(sa.Column('opening_float_strategy', sa.String(length=20), nullable=False, server_default='FIXED'))

    values = ",\n".join(
        "('%s','%s','%s')" % (code, module, desc) for code, module, desc in NEW_PERMISSIONS
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
    with op.batch_alter_table('pos_counters', schema=None) as batch_op:
        batch_op.drop_column('opening_float_strategy')
        batch_op.drop_column('default_opening_float')
        batch_op.drop_column('description')

    with op.batch_alter_table('counter_sessions', schema=None) as batch_op:
        batch_op.drop_constraint('fk_counter_sessions_reconciled_by', type_='foreignkey')
        batch_op.drop_constraint('uq_counter_session_date', type_='unique')
        batch_op.drop_column('reconciled_by_user_id')
        batch_op.drop_column('reconciled_at')
        batch_op.drop_column('reconciliation_status')
        batch_op.drop_column('close_type')
        batch_op.drop_column('scheduled_close_at')
        batch_op.drop_column('open_type')

    with op.batch_alter_table('pos_workstations', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_pos_workstations_workstation_code'))

    op.drop_table('pos_workstations')
