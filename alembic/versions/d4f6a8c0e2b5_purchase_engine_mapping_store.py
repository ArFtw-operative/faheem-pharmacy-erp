"""purchase processing engine: item forms, product packaging, mapping store, import metrics

Additive only: new tables and new nullable columns. No existing column is dropped, renamed
or rewritten; old invoices, batches and ledger rows render exactly as before.

Revision ID: d4f6a8c0e2b5
Revises: c9e2a5b8d1f4
Create Date: 2026-10-03 12:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d4f6a8c0e2b5"
down_revision: Union[str, Sequence[str], None] = "c9e2a5b8d1f4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# frozen copy of app.services.form_service.BUILTIN at this revision
BUILTIN = (
    ('TABLET', 'Tablets', 'TABLET', 'STRIP', True, ''),
    ('CAPSULE', 'Capsules', 'CAPSULE', 'STRIP', True, ''),
    ('SOFTGEL', 'Softgel capsules', 'CAPSULE', 'STRIP', True, ''),
    ('SYRUP', 'Syrup bottles', 'BOTTLE', 'BOTTLE', False, 'ML'),
    ('SUSPENSION', 'Suspension bottles', 'BOTTLE', 'BOTTLE', False, 'ML'),
    ('DROPS', 'Drop bottles (eye / ear / nasal / oral)', 'BOTTLE', 'BOTTLE', False, 'ML'),
    ('LOTION', 'Lotion / shampoo / liquid bottles', 'BOTTLE', 'BOTTLE', False, 'ML'),
    ('SPRAY', 'Sprays', 'BOTTLE', 'BOTTLE', False, 'ML'),
    ('CREAM', 'Cream tubes', 'TUBE', 'TUBE', False, 'G'),
    ('OINTMENT', 'Ointment tubes', 'TUBE', 'TUBE', False, 'G'),
    ('GEL', 'Gel tubes', 'TUBE', 'TUBE', False, 'G'),
    ('POWDER', 'Powder packs / jars', 'PACK', 'PACK', False, 'G'),
    ('SACHET', 'Sachets', 'SACHET', 'SACHET', False, 'G'),
    ('INJECTION', 'Injection vials', 'VIAL', 'VIAL', False, 'ML'),
    ('VIAL', 'Vials (box of vials)', 'VIAL', 'BOX', True, 'ML'),
    ('AMPOULE', 'Ampoules (box of ampoules)', 'AMPOULE', 'BOX', True, 'ML'),
    ('IV_FLUID', 'IV fluid bottles / bags', 'BOTTLE', 'BOTTLE', False, 'ML'),
    ('INHALER', 'Inhalers', 'PIECE', 'PIECE', False, ''),
    ('ROTACAP', 'Rotacaps / inhalation capsules', 'CAPSULE', 'STRIP', True, ''),
    ('RESPULE', 'Respules (pack of respules)', 'PIECE', 'PACK', True, 'ML'),
    ('SUPPOSITORY', 'Suppositories', 'PIECE', 'STRIP', True, ''),
    ('SOAP', 'Soap bars', 'PIECE', 'PIECE', False, 'G'),
    ('SYRINGE', 'Syringes', 'PIECE', 'BOX', True, ''),
    ('NEEDLE', 'Needles', 'PIECE', 'BOX', True, ''),
    ('CANNULA', 'Cannulas / IV sets', 'PIECE', 'BOX', True, ''),
    ('DEVICE', 'Pieces / devices', 'PIECE', 'PIECE', False, ''),
    ('TEST_STRIP', 'Test strips (box of strips)', 'PIECE', 'BOX', True, ''),
    ('BANDAGE', 'Bandages / dressings', 'PIECE', 'PIECE', False, ''),
    ('KIT', 'Kits', 'KIT', 'KIT', False, ''),
    ('PAIR', 'Pairs (gloves, supports)', 'PAIR', 'PACK', False, ''),
    ('JAR', 'Jars', 'JAR', 'JAR', False, 'G'),
    ('BOX', 'Whole boxes', 'BOX', 'BOX', False, ''),
    ('PACK', 'Whole packs', 'PACK', 'PACK', False, ''),
    ('BOTTLE', 'Bottles', 'BOTTLE', 'BOTTLE', False, 'ML'),
    ('TUBE', 'Tubes', 'TUBE', 'TUBE', False, 'G'),
    ('UNIT', 'Other counted units', 'UNIT', 'PACK', True, ''),
)


def upgrade() -> None:
    op.create_table(
        "item_forms",
        sa.Column("code", sa.String(20), primary_key=True),
        sa.Column("name", sa.String(60), nullable=False, server_default=""),
        sa.Column("base_unit", sa.String(20), nullable=False, server_default="UNIT"),
        sa.Column("pack_unit", sa.String(20), nullable=False, server_default="PACK"),
        sa.Column("counted", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("content_unit", sa.String(10), nullable=False, server_default=""),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="100"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("builtin", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.current_timestamp()),
    )
    op.create_table(
        "product_packagings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("item_id", sa.Integer(), sa.ForeignKey("items.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("dosage_form", sa.String(20), nullable=False, server_default=""),
        sa.Column("raw_supplier_packing", sa.String(60), nullable=False, server_default=""),
        sa.Column("normalized_packing", sa.String(60), nullable=False, server_default=""),
        sa.Column("purchase_unit", sa.String(20), nullable=False, server_default=""),
        sa.Column("retail_unit", sa.String(20), nullable=False, server_default=""),
        sa.Column("base_unit", sa.String(20), nullable=False, server_default=""),
        sa.Column("purchase_to_retail", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("retail_to_base", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("container_size", sa.Numeric(10, 2), nullable=True),
        sa.Column("container_size_unit", sa.String(10), nullable=False, server_default=""),
        sa.Column("allow_loose_sale", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("is_preferred", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("source", sa.String(20), nullable=False, server_default=""),
        sa.Column("confidence", sa.String(12), nullable=False, server_default=""),
        sa.Column("verified_by_user", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.current_timestamp()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.current_timestamp()),
        sa.UniqueConstraint("item_id", "normalized_packing", "purchase_to_retail", name="uq_product_packaging"),
    )
    op.create_table(
        "supplier_packaging_aliases",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("supplier_id", sa.Integer(), sa.ForeignKey("suppliers.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("pack_key", sa.String(60), nullable=False, server_default=""),
        sa.Column("raw_pack", sa.String(60), nullable=False, server_default=""),
        sa.Column("item_id", sa.Integer(), sa.ForeignKey("items.id", ondelete="CASCADE"), nullable=True),
        sa.Column("base_unit", sa.String(20), nullable=False, server_default=""),
        sa.Column("units_per_invoice_unit", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("retail_units", sa.Integer(), nullable=True),
        sa.Column("mrp_basis", sa.String(12), nullable=False, server_default="MASTER_PACK"),
        sa.Column("occurrences", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("corrections", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("trust", sa.Numeric(4, 3), nullable=False, server_default="1"),
        sa.Column("status", sa.String(12), nullable=False, server_default="ACTIVE"),
        sa.Column("source", sa.String(20), nullable=False, server_default="USER_CORRECTION"),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.current_timestamp()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.current_timestamp()),
        sa.UniqueConstraint("supplier_id", "pack_key", "item_id", name="uq_supplier_packaging_alias"),
    )
    op.create_table(
        "supplier_invoice_profiles",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("supplier_id", sa.Integer(), sa.ForeignKey("suppliers.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("layout_key", sa.String(64), nullable=False, server_default="", index=True),
        sa.Column("source_format", sa.String(10), nullable=False, server_default=""),
        sa.Column("header_tokens", sa.JSON(), nullable=True),
        sa.Column("column_roles", sa.JSON(), nullable=True),
        sa.Column("invoice_patterns", sa.JSON(), nullable=True),
        sa.Column("parser", sa.String(30), nullable=False, server_default=""),
        sa.Column("invoices", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("confidence", sa.Numeric(4, 3), nullable=False, server_default="0.9"),
        sa.Column("last_seen_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.current_timestamp()),
        sa.UniqueConstraint("supplier_id", "layout_key", name="uq_supplier_invoice_profile"),
    )
    op.create_table(
        "mapping_history",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("kind", sa.String(24), nullable=False),
        sa.Column("action", sa.String(16), nullable=False),
        sa.Column("supplier_id", sa.Integer(), sa.ForeignKey("suppliers.id", ondelete="SET NULL"), nullable=True),
        sa.Column("key", sa.String(250), nullable=False, server_default=""),
        sa.Column("before", sa.JSON(), nullable=True),
        sa.Column("after", sa.JSON(), nullable=True),
        sa.Column("purchase_id", sa.Integer(), nullable=True),
        sa.Column("line_id", sa.Integer(), nullable=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.current_timestamp(), index=True),
    )
    op.create_index("ix_mapping_history_kind_key", "mapping_history", ["kind", "supplier_id"])
    op.create_table(
        "import_metrics",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("purchase_id", sa.Integer(), nullable=True, index=True),
        sa.Column("supplier_id", sa.Integer(), nullable=True, index=True),
        sa.Column("source_format", sa.String(10), nullable=False, server_default=""),
        sa.Column("route", sa.String(20), nullable=False, server_default=""),
        sa.Column("lines", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("auto_accepted", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("with_warning", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("review", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("blocked", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("corrected", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("packaging_resolved", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("ocr_confidence", sa.Numeric(5, 2), nullable=True),
        sa.Column("processing_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.current_timestamp(), index=True),
    )
    with op.batch_alter_table("supplier_product_maps") as b:
        b.add_column(sa.Column("trust", sa.Numeric(4, 3), nullable=True))
        b.add_column(sa.Column("corrections", sa.Integer(), nullable=True))
        b.add_column(sa.Column("status", sa.String(12), nullable=True))
        b.add_column(sa.Column("source", sa.String(20), nullable=True))
        b.add_column(sa.Column("last_used_at", sa.DateTime(), nullable=True))
    with op.batch_alter_table("inventory_movements") as b:
        b.add_column(sa.Column("purchase_quantity", sa.Numeric(14, 4), nullable=True))
        b.add_column(sa.Column("purchase_uom", sa.String(20), nullable=True))
        b.add_column(sa.Column("retail_quantity", sa.Numeric(14, 4), nullable=True))
        b.add_column(sa.Column("retail_uom", sa.String(20), nullable=True))
        b.add_column(sa.Column("base_uom", sa.String(20), nullable=True))
    # built-in item forms as of this revision (the application also adds any missing ones at start-up)
    forms = sa.table("item_forms", sa.column("code"), sa.column("name"), sa.column("base_unit"), sa.column("pack_unit"),
                     sa.column("counted", sa.Boolean), sa.column("content_unit"), sa.column("sort_order"),
                     sa.column("is_active", sa.Boolean), sa.column("builtin", sa.Boolean))
    op.bulk_insert(forms, [{"code": c, "name": n, "base_unit": b, "pack_unit": p, "counted": k, "content_unit": u,
                            "sort_order": (i + 1) * 10, "is_active": True, "builtin": True}
                           for i, (c, n, b, p, k, u) in enumerate(BUILTIN)])


def downgrade() -> None:
    with op.batch_alter_table("inventory_movements") as b:
        for col in ("base_uom", "retail_uom", "retail_quantity", "purchase_uom", "purchase_quantity"):
            b.drop_column(col)
    with op.batch_alter_table("supplier_product_maps") as b:
        for col in ("last_used_at", "source", "status", "corrections", "trust"):
            b.drop_column(col)
    op.drop_table("import_metrics")
    op.drop_index("ix_mapping_history_kind_key", table_name="mapping_history")
    op.drop_table("mapping_history")
    op.drop_table("supplier_invoice_profiles")
    op.drop_table("supplier_packaging_aliases")
    op.drop_table("product_packagings")
    op.drop_table("item_forms")
