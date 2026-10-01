"""category referential integrity enforced in SQL

Every product's category must exist in the category master, and a category in
use cannot be deleted. Implemented with triggers so the ``items`` table (with
its FTS triggers and many child tables) is not rebuilt.

Revision ID: f2a4b6c8d0e2
Revises: e1f3a5b7c9d1
Create Date: 2026-09-30 14:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'f2a4b6c8d0e2'
down_revision: Union[str, Sequence[str], None] = 'e1f3a5b7c9d1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TRIGGERS = {
    "trg_items_category_insert": """
        CREATE TRIGGER IF NOT EXISTS trg_items_category_insert BEFORE INSERT ON items
        WHEN NOT EXISTS (SELECT 1 FROM categories WHERE code = NEW.category)
        BEGIN SELECT RAISE(ABORT, 'Unknown product category'); END""",
    "trg_items_category_update": """
        CREATE TRIGGER IF NOT EXISTS trg_items_category_update BEFORE UPDATE OF category ON items
        WHEN NOT EXISTS (SELECT 1 FROM categories WHERE code = NEW.category)
        BEGIN SELECT RAISE(ABORT, 'Unknown product category'); END""",
    "trg_categories_delete_in_use": """
        CREATE TRIGGER IF NOT EXISTS trg_categories_delete_in_use BEFORE DELETE ON categories
        WHEN EXISTS (SELECT 1 FROM items WHERE category = OLD.code)
        BEGIN SELECT RAISE(ABORT, 'Category is in use by products; merge it into another category first'); END""",
    "trg_categories_code_update": """
        CREATE TRIGGER IF NOT EXISTS trg_categories_code_update BEFORE UPDATE OF code ON categories
        WHEN EXISTS (SELECT 1 FROM items WHERE category = OLD.code)
        BEGIN SELECT RAISE(ABORT, 'Category code is referenced by products and cannot change'); END""",
}


def upgrade() -> None:
    conn = op.get_bind()
    # any category a product already uses must exist before the triggers apply
    conn.execute(sa.text(
        "INSERT INTO categories (code, name, sort_order, is_active) "
        "SELECT DISTINCT i.category, i.category, 1000, 1 FROM items i "
        "WHERE i.category != '' AND NOT EXISTS (SELECT 1 FROM categories c WHERE c.code = i.category)"))
    conn.execute(sa.text("UPDATE items SET category = 'OTHER' WHERE category = ''"))
    op.create_index('ix_items_category_active', 'items', ['category', 'is_active'])
    for ddl in TRIGGERS.values():
        conn.execute(sa.text(ddl))


def downgrade() -> None:
    conn = op.get_bind()
    for name in TRIGGERS:
        conn.execute(sa.text(f"DROP TRIGGER IF EXISTS {name}"))
    op.drop_index('ix_items_category_active', table_name='items')
