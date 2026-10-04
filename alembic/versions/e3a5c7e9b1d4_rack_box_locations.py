"""rack / box locations: racks, boxes, item locations over time, location events

Adds the rack and box masters, the dated product-location table, the append-only
location event log, a preferred rack per category, the confirmed rack / box of a
purchase line and the location permissions. Existing products start Unassigned;
a legacy free-text ``items.rack`` value (if any) becomes a real rack and an
assignment recorded as a MIGRATION event, so nothing is invented and nothing is lost.

Revision ID: e3a5c7e9b1d4
Revises: d4f6a8c0e2b5
Create Date: 2026-10-04 09:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'e3a5c7e9b1d4'
down_revision: Union[str, Sequence[str], None] = 'd4f6a8c0e2b5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

PERMISSIONS = {
    "rack.view": "See racks, boxes and product locations",
    "rack.create": "Create racks",
    "rack.edit": "Edit racks and location settings",
    "rack.disable": "Disable and enable racks",
    "rack.assign": "Assign or move a product's location",
    "rack.bulk_move": "Move many products to a rack at once",
    "rack.history.view": "See location history",
    "rack.report.view": "Rack inventory reports",
    "rack.snapshot.view": "Historical (as-of) rack inventory",
    "box.manage": "Create, rename and disable boxes",
}
ALL = set(PERMISSIONS)
GRANTS = {
    "Administrator": ALL, "Manager": ALL,
    "Pharmacist": {"rack.view", "rack.assign", "rack.bulk_move", "rack.history.view", "rack.report.view", "rack.snapshot.view"},
    "Sales Staff": {"rack.view"}, "Counter Manager": {"rack.view"},
    "Accountant": {"rack.view", "rack.history.view", "rack.report.view", "rack.snapshot.view"},
}
# SQLite cannot add a composite foreign key to an existing table without rebuilding it (and
# purchase_items is referenced by other tables): a trigger enforces the same rule there.
SQLITE_TRIGGERS = {
    "trg_purchase_items_box_rack_insert": """
        CREATE TRIGGER IF NOT EXISTS trg_purchase_items_box_rack_insert BEFORE INSERT ON purchase_items
        WHEN NEW.box_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM rack_boxes b WHERE b.id = NEW.box_id AND b.rack_id = NEW.rack_id)
        BEGIN SELECT RAISE(ABORT, 'The box does not belong to the rack'); END""",
    "trg_purchase_items_box_rack_update": """
        CREATE TRIGGER IF NOT EXISTS trg_purchase_items_box_rack_update BEFORE UPDATE OF rack_id, box_id ON purchase_items
        WHEN NEW.box_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM rack_boxes b WHERE b.id = NEW.box_id AND b.rack_id = NEW.rack_id)
        BEGIN SELECT RAISE(ABORT, 'The box does not belong to the rack'); END""",
}


def _audit_cols():
    return [
        sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column('created_by', sa.Integer(), sa.ForeignKey('users.id'), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column('updated_by', sa.Integer(), sa.ForeignKey('users.id'), nullable=True),
    ]


def upgrade() -> None:
    conn = op.get_bind()
    sqlite = conn.dialect.name == "sqlite"

    op.create_table(
        'racks',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('code', sa.String(30), nullable=False),
        sa.Column('name', sa.String(80), nullable=False, server_default=''),
        sa.Column('description', sa.Text(), nullable=False, server_default=''),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('sort_order', sa.Integer(), nullable=False, server_default='100'),
        sa.Column('zone', sa.String(40), nullable=False, server_default=''),
        sa.Column('shelf_count', sa.Integer(), nullable=True),
        sa.Column('capacity', sa.Integer(), nullable=True),
        *_audit_cols(),
        sa.UniqueConstraint('code', name='uq_racks_code'),
    )
    op.create_table(
        'rack_boxes',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('rack_id', sa.Integer(), sa.ForeignKey('racks.id', ondelete='RESTRICT'), nullable=False),
        sa.Column('code', sa.String(30), nullable=False),
        sa.Column('name', sa.String(80), nullable=False, server_default=''),
        sa.Column('description', sa.Text(), nullable=False, server_default=''),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('sort_order', sa.Integer(), nullable=False, server_default='100'),
        *_audit_cols(),
        sa.UniqueConstraint('rack_id', 'code', name='uq_rack_box_code'),
        sa.UniqueConstraint('rack_id', 'id', name='uq_rack_box_rack_id'),
    )
    op.create_index('ix_rack_boxes_rack_id', 'rack_boxes', ['rack_id'])
    op.create_table(
        'item_locations',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('item_id', sa.Integer(), sa.ForeignKey('items.id', ondelete='RESTRICT'), nullable=False),
        sa.Column('batch_id', sa.Integer(), sa.ForeignKey('batches.id', ondelete='RESTRICT'), nullable=True),
        sa.Column('rack_id', sa.Integer(), sa.ForeignKey('racks.id', ondelete='RESTRICT'), nullable=False),
        sa.Column('box_id', sa.Integer(), nullable=True),
        sa.Column('quantity', sa.Integer(), nullable=True),
        sa.Column('is_primary', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('valid_from', sa.DateTime(), nullable=False),
        sa.Column('valid_to', sa.DateTime(), nullable=True),
        sa.Column('created_by', sa.Integer(), sa.ForeignKey('users.id'), nullable=True),
        sa.Column('event_id', sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(['rack_id', 'box_id'], ['rack_boxes.rack_id', 'rack_boxes.id'],
                                name='fk_item_locations_rack_box', ondelete='RESTRICT'),
    )
    op.create_index('uq_item_location_open_product', 'item_locations', ['item_id'], unique=True,
                    sqlite_where=sa.text('valid_to IS NULL AND batch_id IS NULL'),
                    postgresql_where=sa.text('valid_to IS NULL AND batch_id IS NULL'))
    op.create_index('uq_item_location_open_batch', 'item_locations', ['item_id', 'batch_id'], unique=True,
                    sqlite_where=sa.text('valid_to IS NULL AND batch_id IS NOT NULL'),
                    postgresql_where=sa.text('valid_to IS NULL AND batch_id IS NOT NULL'))
    op.create_index('ix_item_locations_rack_open', 'item_locations', ['rack_id', 'valid_to'])
    op.create_index('ix_item_locations_box', 'item_locations', ['box_id'])
    op.create_index('ix_item_locations_item_time', 'item_locations', ['item_id', 'valid_from'])
    op.create_index('ix_item_locations_batch', 'item_locations', ['batch_id'])
    op.create_table(
        'location_events',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('operation_id', sa.String(36), nullable=False),
        sa.Column('event_type', sa.String(16), nullable=False),
        sa.Column('source', sa.String(16), nullable=False, server_default='MANUAL'),
        sa.Column('item_id', sa.Integer(), sa.ForeignKey('items.id', ondelete='RESTRICT'), nullable=False),
        sa.Column('batch_id', sa.Integer(), sa.ForeignKey('batches.id', ondelete='RESTRICT'), nullable=True),
        sa.Column('from_rack_id', sa.Integer(), sa.ForeignKey('racks.id', ondelete='RESTRICT'), nullable=True),
        sa.Column('from_box_id', sa.Integer(), sa.ForeignKey('rack_boxes.id', ondelete='RESTRICT'), nullable=True),
        sa.Column('to_rack_id', sa.Integer(), sa.ForeignKey('racks.id', ondelete='RESTRICT'), nullable=True),
        sa.Column('to_box_id', sa.Integer(), sa.ForeignKey('rack_boxes.id', ondelete='RESTRICT'), nullable=True),
        sa.Column('from_label', sa.String(200), nullable=False, server_default=''),
        sa.Column('to_label', sa.String(200), nullable=False, server_default=''),
        sa.Column('stock_snapshot', sa.Integer(), nullable=True),
        sa.Column('reason', sa.Text(), nullable=False, server_default=''),
        sa.Column('reference', sa.String(60), nullable=False, server_default=''),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=True),
        sa.Column('username', sa.String(60), nullable=False, server_default='system'),
        sa.Column('created_at', sa.DateTime(), nullable=False),
    )
    op.create_index('ix_location_events_operation_id', 'location_events', ['operation_id'])
    op.create_index('ix_location_events_item_time', 'location_events', ['item_id', 'created_at'])
    op.create_index('ix_location_events_from_rack', 'location_events', ['from_rack_id', 'created_at'])
    op.create_index('ix_location_events_to_rack', 'location_events', ['to_rack_id', 'created_at'])
    op.create_index('ix_location_events_time', 'location_events', ['created_at'])

    # preferred rack per category, confirmed location per purchase line (added in place: no table rebuilds)
    if sqlite:
        conn.execute(sa.text("ALTER TABLE categories ADD COLUMN default_rack_id INTEGER REFERENCES racks(id) ON DELETE SET NULL"))
        conn.execute(sa.text("ALTER TABLE purchase_items ADD COLUMN rack_id INTEGER REFERENCES racks(id)"))
        conn.execute(sa.text("ALTER TABLE purchase_items ADD COLUMN box_id INTEGER"))
        for ddl in SQLITE_TRIGGERS.values():
            conn.execute(sa.text(ddl))
    else:
        op.add_column('categories', sa.Column('default_rack_id', sa.Integer(), nullable=True))
        op.create_foreign_key('fk_categories_default_rack', 'categories', 'racks', ['default_rack_id'], ['id'], ondelete='SET NULL')
        op.add_column('purchase_items', sa.Column('rack_id', sa.Integer(), nullable=True))
        op.add_column('purchase_items', sa.Column('box_id', sa.Integer(), nullable=True))
        op.create_foreign_key('fk_purchase_items_rack', 'purchase_items', 'racks', ['rack_id'], ['id'])
        op.create_foreign_key('fk_purchase_items_rack_box', 'purchase_items', 'rack_boxes',
                              ['rack_id', 'box_id'], ['rack_id', 'id'])

    # permissions (existing installations; a fresh one is seeded from app/permissions.py)
    ignore = "INSERT OR IGNORE INTO" if sqlite else "INSERT INTO"
    tail = "" if sqlite else " ON CONFLICT DO NOTHING"
    for code, desc in PERMISSIONS.items():
        conn.execute(sa.text(f"{ignore} permissions (code, module, description) VALUES (:c, 'rack', :d){tail}"),
                     {"c": code, "d": desc})
    for role, codes in GRANTS.items():
        for code in codes:
            conn.execute(sa.text(
                f"{ignore} role_permissions (role_id, permission_id) SELECT r.id, p.id FROM roles r, permissions p "
                f"WHERE r.name = :r AND p.code = :c{tail}"), {"r": role, "c": code})

    # a legacy free-text rack is a location someone typed: keep it, as a real rack and an assignment
    legacy = conn.execute(sa.text(
        "SELECT id, UPPER(TRIM(rack)) FROM items WHERE TRIM(COALESCE(rack, '')) != ''")).all()
    if legacy:
        now = conn.execute(sa.text("SELECT CURRENT_TIMESTAMP")).scalar()
        codes = sorted({code[:30] for _, code in legacy})
        for code in codes:
            conn.execute(sa.text("INSERT INTO racks (code, name, description, is_active, sort_order, zone, created_at, updated_at) "
                                 "VALUES (:c, :c, 'Created from the product rack field', :t, 100, '', :now, :now)"),
                         {"c": code, "t": True, "now": now})
        rack_ids = dict(conn.execute(sa.text("SELECT code, id FROM racks")).all())
        for item_id, code in legacy:
            rid = rack_ids[code[:30]]
            event = conn.execute(sa.text(
                "INSERT INTO location_events (operation_id, event_type, source, item_id, to_rack_id, to_label, reason, username, created_at) "
                "VALUES ('migration-legacy-rack', 'ASSIGNED', 'MIGRATION', :i, :r, :l, 'Product rack field before the rack master', 'system', :now)"
                + ("" if sqlite else " RETURNING id")), {"i": item_id, "r": rid, "l": code[:30], "now": now})
            event_id = event.lastrowid if sqlite else event.scalar()
            conn.execute(sa.text("INSERT INTO item_locations (item_id, rack_id, is_primary, valid_from, event_id) "
                                 "VALUES (:i, :r, :t, :now, :e)"), {"i": item_id, "r": rid, "t": True, "now": now, "e": event_id})


def downgrade() -> None:
    conn = op.get_bind()
    sqlite = conn.dialect.name == "sqlite"
    codes = ", ".join(f"'{c}'" for c in PERMISSIONS)
    conn.execute(sa.text(f"DELETE FROM role_permissions WHERE permission_id IN (SELECT id FROM permissions WHERE code IN ({codes}))"))
    conn.execute(sa.text(f"DELETE FROM permissions WHERE code IN ({codes})"))
    if sqlite:
        for name in SQLITE_TRIGGERS:
            conn.execute(sa.text(f"DROP TRIGGER IF EXISTS {name}"))
        # rebuilding a table breaks triggers that name it (category integrity on items): set them
        # aside, rebuild, and put them back exactly as they were
        saved = conn.execute(sa.text(
            "SELECT name, sql FROM sqlite_master WHERE type = 'trigger' AND "
            "(sql LIKE '%categories%' OR sql LIKE '%purchase_items%')")).all()
        for name, _ in saved:
            conn.execute(sa.text(f'DROP TRIGGER IF EXISTS "{name}"'))
        with op.batch_alter_table('purchase_items') as t:
            t.drop_column('box_id')
            t.drop_column('rack_id')
        with op.batch_alter_table('categories') as t:
            t.drop_column('default_rack_id')
        for _, ddl in saved:
            conn.execute(sa.text(ddl))
    else:
        op.drop_constraint('fk_purchase_items_rack_box', 'purchase_items', type_='foreignkey')
        op.drop_constraint('fk_purchase_items_rack', 'purchase_items', type_='foreignkey')
        op.drop_column('purchase_items', 'box_id')
        op.drop_column('purchase_items', 'rack_id')
        op.drop_constraint('fk_categories_default_rack', 'categories', type_='foreignkey')
        op.drop_column('categories', 'default_rack_id')
    op.drop_table('location_events')
    op.drop_table('item_locations')
    op.drop_table('rack_boxes')
    op.drop_table('racks')
    # the legacy items.rack text was never touched, so the previous state is exactly restored
