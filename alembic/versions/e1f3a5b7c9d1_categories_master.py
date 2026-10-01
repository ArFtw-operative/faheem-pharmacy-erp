"""product category master (adds GENERAL; categories become data)

Revision ID: e1f3a5b7c9d1
Revises: d7e9f1a3b5c7
Create Date: 2026-09-30 13:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'e1f3a5b7c9d1'
down_revision: Union[str, Sequence[str], None] = 'd7e9f1a3b5c7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

DEFAULTS = ["PHARMA", "GENERIC", "FMCG", "SURGICAL", "BABY", "BEVERAGES", "AYURVEDIC", "GENERAL", "OTHER"]
NAMES = {"PHARMA": "Pharma", "GENERIC": "Generic", "FMCG": "FMCG", "SURGICAL": "Surgical", "BABY": "Baby care",
         "BEVERAGES": "Beverages", "AYURVEDIC": "Ayurvedic", "GENERAL": "General", "OTHER": "Other"}


def upgrade() -> None:
    op.create_table(
        'categories',
        sa.Column('code', sa.String(30), primary_key=True),
        sa.Column('name', sa.String(60), nullable=False, server_default=''),
        sa.Column('sort_order', sa.Integer(), nullable=False, server_default='100'),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.func.current_timestamp()),
    )
    conn = op.get_bind()
    codes = list(DEFAULTS)
    for (code,) in conn.execute(sa.text("SELECT DISTINCT category FROM items WHERE category != ''")):
        if code and code.upper() not in codes:
            codes.append(code.upper())
    for i, code in enumerate(codes):
        conn.execute(sa.text("INSERT INTO categories (code, name, sort_order, is_active) VALUES (:c, :n, :s, 1)"),
                     {"c": code, "n": NAMES.get(code, code.title()), "s": (i + 1) * 10})


def downgrade() -> None:
    op.drop_table('categories')
