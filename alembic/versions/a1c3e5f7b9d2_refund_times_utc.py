"""store refund processed_at in UTC like every other timestamp

Refunds were stamped with the pharmacy's local wall-clock time while sales and
report bounds use UTC, so a refund made late in the evening fell into the next
day's reports. Existing refund times are converted to UTC.

Revision ID: a1c3e5f7b9d2
Revises: f8a0b2c4d6e8
Create Date: 2026-09-30 19:00:00.000000

"""
from datetime import datetime, timezone
from typing import Sequence, Union
from zoneinfo import ZoneInfo

from alembic import op
import sqlalchemy as sa


revision: str = 'a1c3e5f7b9d2'
down_revision: Union[str, Sequence[str], None] = 'f8a0b2c4d6e8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    row = conn.execute(sa.text("SELECT value FROM settings WHERE key = 'timezone'")).first()
    tz = ZoneInfo((row[0] if row and row[0] else None) or "Asia/Kolkata")
    for rid, when in conn.execute(sa.text("SELECT id, processed_at FROM sale_returns WHERE processed_at IS NOT NULL")).fetchall():
        local = datetime.fromisoformat(str(when)).replace(tzinfo=tz)
        conn.execute(sa.text("UPDATE sale_returns SET processed_at = :t WHERE id = :i"),
                     {"t": local.astimezone(timezone.utc).replace(tzinfo=None), "i": rid})


def downgrade() -> None:
    pass
