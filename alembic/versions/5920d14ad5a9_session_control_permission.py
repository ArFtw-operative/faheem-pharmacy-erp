"""session control permission

Revision ID: 5920d14ad5a9
Revises: 73fd5be50020
Create Date: 2026-09-15 22:50:00.000000

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = '5920d14ad5a9'
down_revision: Union[str, Sequence[str], None] = '73fd5be50020'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Grant the administrator-only session-control permission."""
    op.execute(
        "INSERT OR IGNORE INTO permissions (code, module, description) VALUES "
        "('settings.session_control','settings','Configure session idle and lock-out timeouts')"
    )
    op.execute(
        "INSERT OR IGNORE INTO role_permissions (role_id, permission_id) "
        "SELECT r.id, p.id FROM roles r, permissions p "
        "WHERE r.name = 'Administrator' AND p.code = 'settings.session_control'"
    )


def downgrade() -> None:
    op.execute(
        "DELETE FROM role_permissions WHERE permission_id IN "
        "(SELECT id FROM permissions WHERE code = 'settings.session_control')"
    )
    op.execute("DELETE FROM permissions WHERE code = 'settings.session_control'")
