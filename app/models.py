"""SQLAlchemy ORM models for the pharmacy management system.

Schema design notes:
- Every mutable business entity carries created_at/updated_at and, where a user
  action is involved, created_by/updated_by so the audit layer can attribute
  changes.
- Money is stored as Numeric(12, 2) to avoid float drift.
- Enumerated states are stored as short strings (SQLite friendly, no native enum
  migrations).
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    Column,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    JSON,
    Numeric,
    text,
    String,
    Table,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.utils import utcnow

# --------------------------------------------------------------------------- #
# Enumerated string constants
# --------------------------------------------------------------------------- #
PAYMENT_MODES = ("CASH", "UPI", "CARD", "SPLIT")
PAYMENT_STATUS = ("PAID", "PENDING", "CANCELLED")
CUSTOMER_TYPES = ("WALK_IN", "HOME_DELIVERY")
CUSTOMER_TYPE_LABELS = {"WALK_IN": "Walk-In", "HOME_DELIVERY": "Home Delivery"}
MATCH_STATUS = ("AUTO", "REVIEW", "CONFIRMED", "NEW_ITEM", "REJECTED")
REVIEW_STATUS = ("PENDING", "CONFIRMED", "NEW_ITEM", "REJECTED")
FOLLOWUP_STATUS = ("PENDING", "DONE", "SNOOZED", "CANCELLED")
EXPIRY_STATUS = ("ACTIVE", "SNOOZED", "SETTLED")
ADJUSTMENT_CATEGORIES = ("LOOSE", "DAMAGE", "EXPIRED", "COUNT", "SURPLUS", "OTHER")
NOTIFICATION_TYPES = ("FOLLOWUP", "EXPIRY", "SYSTEM")
PURCHASE_RETURN_STATUS = ("OPEN", "SETTLED")

DRAFT_STATUS = ("ACTIVE", "COMPLETED", "DISCARDED", "PARKED")
PARKED_STATUS = ("PARKED", "CLAIMED", "COMPLETED", "DISCARDED")
RETURN_REASONS = (
    "CUSTOMER_RETURN",
    "WRONG_ITEM",
    "DAMAGED_ITEM",
    "EXPIRY_ISSUE",
    "INCORRECT_QUANTITY",
    "INCORRECT_PRICE",
    "PRESCRIPTION_CHANGED",
    "DUPLICATE_BILLING",
    "OTHER",
)
RETURN_DISPOSITIONS = ("RESTOCK", "DAMAGE", "EXPIRED", "NO_RESTOCK")
REFUND_METHODS = ("CASH", "UPI", "CARD")
RETURN_STATUS = ("COMPLETED", "CANCELLED")
PARK_REASONS = (
    "CUSTOMER_WILL_RETURN",
    "PAYMENT_ISSUE",
    "WAITING_PRESCRIPTION",
    "WAITING_MEDICINE",
    "CUSTOMER_CHECKING_ITEMS",
    "OTHER",
)
DISCARD_REASONS = ("CUSTOMER_CANCELLED", "DUPLICATE", "OTHER")

# Audit actions
A_CREATE, A_UPDATE, A_DELETE = "CREATE", "UPDATE", "DELETE"
A_LOGIN, A_LOGOUT, A_LOGIN_FAILED = "LOGIN", "LOGOUT", "LOGIN_FAILED"
A_SETTLE, A_SNOOZE, A_EXPORT, A_PRINT, A_PERMISSION_CHANGE = (
    "SETTLE",
    "SNOOZE",
    "EXPORT",
    "PRINT",
    "PERMISSION_CHANGE",
)


role_permissions = Table(
    "role_permissions",
    Base.metadata,
    Column("role_id", ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True),
    Column(
        "permission_id", ForeignKey("permissions.id", ondelete="CASCADE"), primary_key=True
    ),
)


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")
    value_type: Mapped[str] = mapped_column(String(20), default="str")
    label: Mapped[str] = mapped_column(String(150), default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class Role(Base):
    __tablename__ = "roles"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(60), unique=True)
    description: Mapped[str] = mapped_column(String(255), default="")
    is_system: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    permissions: Mapped[list["Permission"]] = relationship(
        secondary=role_permissions, back_populates="roles", lazy="selectin"
    )
    users: Mapped[list["User"]] = relationship(back_populates="role")

    def permission_codes(self) -> set[str]:
        return {p.code for p in self.permissions}


class Permission(Base):
    __tablename__ = "permissions"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(80), unique=True)
    module: Mapped[str] = mapped_column(String(40))
    description: Mapped[str] = mapped_column(String(200), default="")

    roles: Mapped[list["Role"]] = relationship(
        secondary=role_permissions, back_populates="permissions"
    )


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    employee_id: Mapped[str] = mapped_column(String(20), unique=True, index=True)
    username: Mapped[str] = mapped_column(String(60), unique=True, index=True)
    full_name: Mapped[str] = mapped_column(String(120))
    password_hash: Mapped[str] = mapped_column(String(255))
    role_id: Mapped[int | None] = mapped_column(ForeignKey("roles.id"))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=True)
    phone: Mapped[str] = mapped_column(String(20), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    last_login: Mapped[datetime | None] = mapped_column(DateTime)
    # two-factor authentication (app/services/mfa.py): secret sealed with the installation key
    mfa_secret: Mapped[str] = mapped_column(Text, default="", server_default="")
    mfa_enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    mfa_enrolled_at: Mapped[datetime | None] = mapped_column(DateTime)
    mfa_last_step: Mapped[int | None] = mapped_column(Integer)           # replay guard
    mfa_recovery: Mapped[list | None] = mapped_column(JSON)              # sha-256 of unused recovery codes
    failed_logins: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    locked_until: Mapped[datetime | None] = mapped_column(DateTime)
    password_changed_at: Mapped[datetime | None] = mapped_column(DateTime)

    role: Mapped[Role | None] = relationship(back_populates="users", lazy="joined")
    sessions: Mapped[list["LoginSession"]] = relationship(back_populates="user")


class LoginSession(Base):
    __tablename__ = "login_sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    login_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    logout_at: Mapped[datetime | None] = mapped_column(DateTime)
    ip_address: Mapped[str] = mapped_column(String(60), default="")
    user_agent: Mapped[str] = mapped_column(String(255), default="")

    user: Mapped[User] = relationship(back_populates="sessions")


class AuditLog(Base):
    __tablename__ = "audit_logs"
    __table_args__ = (Index("ix_audit_entity", "entity_type", "entity_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    username: Mapped[str] = mapped_column(String(60), default="system")
    action: Mapped[str] = mapped_column(String(30))
    entity_type: Mapped[str] = mapped_column(String(60))
    entity_id: Mapped[str] = mapped_column(String(60), default="")
    before: Mapped[dict | None] = mapped_column(JSON)
    after: Mapped[dict | None] = mapped_column(JSON)
    details: Mapped[str] = mapped_column(Text, default="")
    ip_address: Mapped[str] = mapped_column(String(60), default="")


class NumberSequence(Base):
    __tablename__ = "number_sequences"

    key: Mapped[str] = mapped_column(String(40), primary_key=True)
    next_value: Mapped[int] = mapped_column(Integer, default=1)


class Category(Base):
    """Product category master (PHARMA, GENERIC, FMCG … GENERAL). Items and reports
    refer to categories by code; the list is data, not code."""

    __tablename__ = "categories"

    code: Mapped[str] = mapped_column(String(30), primary_key=True)
    name: Mapped[str] = mapped_column(String(60), default="")
    sort_order: Mapped[int] = mapped_column(Integer, default=100)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    # the rack this category's products usually go to: offered as a suggestion, never applied silently
    default_rack_id: Mapped[int | None] = mapped_column(ForeignKey("racks.id", ondelete="SET NULL"))


class Supplier(Base):
    """Supplier master (Purchases → Suppliers)."""

    __tablename__ = "suppliers"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str | None] = mapped_column(String(20), unique=True)
    name: Mapped[str] = mapped_column(String(150), index=True)
    contact: Mapped[str] = mapped_column(String(120), default="")
    phone: Mapped[str] = mapped_column(String(40), default="")
    email: Mapped[str] = mapped_column(String(120), default="")
    gst_number: Mapped[str] = mapped_column(String(30), default="")
    address: Mapped[str] = mapped_column(Text, default="")
    payment_terms: Mapped[str] = mapped_column(String(60), default="")
    credit_days: Mapped[int] = mapped_column(Integer, default=0)
    column_profile: Mapped[dict | None] = mapped_column(JSON)   # learned: supplier column header → field
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    batches: Mapped[list["Batch"]] = relationship(back_populates="supplier")


class SupplierProductMap(Base):
    """A confirmed link between how a supplier names a product and our product.

    Once a person confirms it, later imports from that supplier use it
    deterministically (never a fuzzy guess)."""

    __tablename__ = "supplier_product_maps"
    __table_args__ = (UniqueConstraint("supplier_id", "supplier_code", "description_key", name="uq_supplier_product_map"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    supplier_id: Mapped[int] = mapped_column(ForeignKey("suppliers.id", ondelete="CASCADE"), index=True)
    supplier_code: Mapped[str] = mapped_column(String(40), default="")
    description_key: Mapped[str] = mapped_column(String(250), default="")
    description_raw: Mapped[str] = mapped_column(String(250), default="")
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id"), index=True)
    confirmed_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    confirmed_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    uses: Mapped[int] = mapped_column(Integer, default=0)
    receipt_conventions: Mapped[dict | None] = mapped_column(JSON)
    # Mapping store: how far the alias is trusted. A person overruling it lowers trust and marks it
    # AMBIGUOUS, after which it is only suggested, never applied (history in mapping_history).
    trust: Mapped[Decimal | None] = mapped_column(Numeric(4, 3))
    corrections: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str | None] = mapped_column(String(12))          # ACTIVE · AMBIGUOUS
    source: Mapped[str | None] = mapped_column(String(20))          # CONFIRMED · POSTED · BOOTSTRAP · USER_CORRECTION
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime)

    item: Mapped["Item"] = relationship()


class Item(Base):
    """Item master. Article ID is the human/business key."""

    __tablename__ = "items"

    id: Mapped[int] = mapped_column(primary_key=True)
    article_id: Mapped[str] = mapped_column(String(30), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(250), index=True)
    generic_name: Mapped[str] = mapped_column(String(250), default="")
    manufacturer: Mapped[str] = mapped_column(String(150), default="")
    category: Mapped[str] = mapped_column(String(60), default="PHARMA", index=True)
    # Raw pack text exactly as supplied (``15S``, ``2X10S``, ``200ML``); the
    # structured packaging below is what stock and pricing use.
    pack_size: Mapped[str] = mapped_column(String(60), default="")
    strength: Mapped[str] = mapped_column(String(60), default="")
    unit: Mapped[str] = mapped_column(String(20), default="unit")
    dosage_form: Mapped[str] = mapped_column(String(20), default="")
    # Packaging (see app.services.units). Stock is counted in ``base_unit``;
    # one purchase pack (``pack_unit``) holds ``units_per_pack`` base units.
    base_unit: Mapped[str] = mapped_column(String(20), default="UNIT")
    pack_unit: Mapped[str] = mapped_column(String(20), default="PACK")
    units_per_pack: Mapped[int] = mapped_column(Integer, default=1)
    # derived: True whenever the sale unit is smaller than the purchase unit
    loose_sale: Mapped[bool] = mapped_column(Boolean, default=False)
    # DEFAULT = not yet configured · AUTO = set by UOM detection · MANUAL = corrected by a user
    packaging_source: Mapped[str] = mapped_column(String(10), default="DEFAULT", index=True)
    content_qty: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    content_unit: Mapped[str] = mapped_column(String(10), default="")
    reorder_level: Mapped[int] = mapped_column(Integer, default=0)  # base units; 0 = use the global threshold
    # legacy free-text rack (before the rack master). Read once by the location migration; the
    # product's location now lives in item_locations (app/services/location_service.py).
    rack: Mapped[str] = mapped_column(String(20), default="")
    hsn_code: Mapped[str] = mapped_column(String(20), default="")
    gst_rate: Mapped[Decimal] = mapped_column(Numeric(5, 2), default=Decimal("12"))
    mrp: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0"))
    barcode: Mapped[str] = mapped_column(String(60), default="", index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    batches: Mapped[list["Batch"]] = relationship(back_populates="item")

    @property
    def total_stock(self) -> int:
        return sum(b.quantity for b in self.batches if b.quantity > 0)


class Batch(Base):
    """A manufactured lot of one product.

    ``quantity`` is a projection of the inventory ledger (base units), updated
    only by :mod:`app.services.stock_ledger` in the same transaction as the
    movement. ``mrp`` / ``purchase_rate`` are per pack; ``unit_mrp`` /
    ``unit_cost`` are per base unit using the batch's own pack snapshot.
    """

    __tablename__ = "batches"
    __table_args__ = (
        Index("ix_batches_expiry", "expiry_date"),
        Index("ix_batches_item_stock", "item_id", "quantity", "expiry_date"),
        # the same batch number twice on one product is one batch (two
        # products may legitimately share a batch number)
        Index("uq_batch_item_normalized", "item_id", "batch_no_normalized", unique=True,
              sqlite_where=text("batch_no_normalized != ''"), postgresql_where=text("batch_no_normalized != ''")),
        UniqueConstraint("item_id", "batch_no", "expiry_date", name="uq_batch_item_no_exp"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id", ondelete="CASCADE"))
    # Batch numbers are text, always: the raw value is kept exactly as given.
    batch_no: Mapped[str] = mapped_column(String(60), default="")
    batch_no_normalized: Mapped[str] = mapped_column(String(60), default="", index=True)
    expiry_date: Mapped[date | None] = mapped_column(Date)
    mrp: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0"))
    purchase_rate: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0"))
    # INCL_GST / EXCL_GST: whether purchase_rate includes the GST paid to the supplier; "" = received before GST tracking
    rate_basis: Mapped[str] = mapped_column(String(12), default="", server_default="")
    selling_rate: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0"))
    units_per_pack: Mapped[int] = mapped_column(Integer, default=1)
    unit_mrp: Mapped[Decimal] = mapped_column(Numeric(12, 4), default=Decimal("0"))
    unit_cost: Mapped[Decimal] = mapped_column(Numeric(18, 6), default=Decimal("0"))
    cost_status: Mapped[str] = mapped_column(String(24), default="COST_MISSING")
    quantity: Mapped[int] = mapped_column(Integer, default=0)
    supplier_id: Mapped[int | None] = mapped_column(ForeignKey("suppliers.id"))
    purchase_id: Mapped[int | None] = mapped_column(ForeignKey("purchases.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    item: Mapped[Item] = relationship(back_populates="batches")
    supplier: Mapped[Supplier | None] = relationship(back_populates="batches")


class Customer(Base):
    __tablename__ = "customers"

    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[str] = mapped_column(String(30), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(150), index=True)
    mobile: Mapped[str] = mapped_column(String(20), default="", index=True)
    customer_type: Mapped[str] = mapped_column(String(20), default="WALK_IN", index=True)
    alternate_mobile: Mapped[str] = mapped_column(String(20), default="", index=True)
    reference: Mapped[str] = mapped_column(String(120), default="")          # optional document / reference
    email: Mapped[str] = mapped_column(String(150), default="")
    gender: Mapped[str] = mapped_column(String(20), default="")
    date_of_birth: Mapped[date | None] = mapped_column(Date)
    doctor_name: Mapped[str] = mapped_column(String(150), default="")
    address: Mapped[str] = mapped_column(Text, default="")
    city: Mapped[str] = mapped_column(String(80), default="")
    state: Mapped[str] = mapped_column(String(80), default="")
    pincode: Mapped[str] = mapped_column(String(12), default="")
    notes: Mapped[str] = mapped_column(Text, default="")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    updated_at: Mapped[datetime | None] = mapped_column(DateTime)

    sales: Mapped[list["Sale"]] = relationship(back_populates="customer")


FOLLOWUP_STATUS = ("OPEN", "COMPLETED", "CANCELLED", "RESCHEDULED")
FOLLOWUP_REASONS = {"REFILL": "Medicine refill", "AVAILABILITY": "Check availability", "REPEAT": "Repeat purchase",
                    "PAYMENT": "Payment / invoice query", "GENERAL": "General follow-up", "CUSTOM": "Custom"}


class CustomerFollowUp(Base):
    """One follow-up task. Inbox, Calendar and the customer's activity all read this record."""

    __tablename__ = "customer_followups"
    __table_args__ = (Index("ix_followups_due", "status", "due_date"), Index("ix_followups_customer", "customer_id", "due_date"))

    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"))
    source_sale_id: Mapped[int | None] = mapped_column(ForeignKey("sales.id"))
    due_date: Mapped[date] = mapped_column(Date)
    due_time: Mapped[str] = mapped_column(String(5), default="")
    reason: Mapped[str] = mapped_column(String(40), default="GENERAL")
    note: Mapped[str] = mapped_column(Text, default="")
    contact_method: Mapped[str] = mapped_column(String(20), default="")
    status: Mapped[str] = mapped_column(String(12), default="OPEN")
    reschedule_count: Mapped[int] = mapped_column(Integer, default=0)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    completed_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime)
    completion_note: Mapped[str] = mapped_column(Text, default="")

    customer: Mapped["Customer"] = relationship()
    source_sale: Mapped["Sale | None"] = relationship()


class Sale(Base):
    __tablename__ = "sales"
    __table_args__ = (Index("ix_sales_customer_date", "customer_id", "sale_date"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    invoice_no: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    customer_id: Mapped[int | None] = mapped_column(ForeignKey("customers.id"))
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    sale_date: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    subtotal: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0"))
    discount: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0"))
    voucher: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0"))
    round_off: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0"))
    total: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0"))
    payment_mode: Mapped[str] = mapped_column(String(10), default="CASH", index=True)
    payment_status: Mapped[str] = mapped_column(String(10), default="PAID")
    customer_type: Mapped[str] = mapped_column(String(20), default="WALK_IN", index=True)
    invoice_format: Mapped[str] = mapped_column(String(20), default="CLASSIC", index=True)
    # INVENTORY: lines come from stock (ledgered) · MANUAL: typed lines, no stock effect
    invoice_type: Mapped[str] = mapped_column(String(10), default="INVENTORY", index=True)
    notes: Mapped[str] = mapped_column(Text, default="")
    tendered_amount: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    change_amount: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    client_request_id: Mapped[str | None] = mapped_column(String(64), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    customer: Mapped[Customer | None] = relationship(back_populates="sales")
    user: Mapped[User | None] = relationship()
    items: Mapped[list["SaleItem"]] = relationship(     # explicit order: PostgreSQL returns rows in no particular order
        back_populates="sale", cascade="all, delete-orphan", order_by="SaleItem.id"
    )
    payments: Mapped[list["SalePayment"]] = relationship(
        back_populates="sale", cascade="all, delete-orphan", order_by="SalePayment.id"
    )


class SalePayment(Base):
    """How a bill was paid. One row per method; a split bill has several.

    ``Sale.payment_mode`` keeps the single method, or ``SPLIT``, for display
    and filtering; money-by-method reporting always reads these rows.
    """

    __tablename__ = "sale_payments"

    id: Mapped[int] = mapped_column(primary_key=True)
    sale_id: Mapped[int] = mapped_column(ForeignKey("sales.id", ondelete="CASCADE"), index=True)
    mode: Mapped[str] = mapped_column(String(10), index=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0"))
    reference: Mapped[str] = mapped_column(String(80), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    sale: Mapped[Sale] = relationship(back_populates="payments")


class SaleItem(Base):
    __tablename__ = "sale_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    sale_id: Mapped[int] = mapped_column(ForeignKey("sales.id", ondelete="CASCADE"))
    item_id: Mapped[int | None] = mapped_column(ForeignKey("items.id"))
    batch_id: Mapped[int | None] = mapped_column(ForeignKey("batches.id"))
    product_name: Mapped[str] = mapped_column(String(250))
    item_code: Mapped[str] = mapped_column(String(40), default="", server_default="")   # typed on manual-bill lines
    batch_no: Mapped[str] = mapped_column(String(60), default="")
    expiry_date: Mapped[date | None] = mapped_column(Date)
    # base units sold from this batch; ``mrp``/``rate`` are per base unit and
    # ``pack_mrp``/``units_per_pack``/``pack_size``/``base_unit`` snapshot the
    # packaging so the bill never changes when the master does.
    quantity: Mapped[int] = mapped_column(Integer, default=0)
    mrp: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0"))
    rate: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0"))
    cost_rate: Mapped[Decimal] = mapped_column(Numeric(18, 6), default=Decimal("0"))
    financial_status: Mapped[str] = mapped_column(String(24), default="LEGACY_UNCHECKED")
    financial_cost_source: Mapped[str] = mapped_column(String(32), default="")
    reconstructed_unit_cost: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    cost_reconstructed_at: Mapped[datetime | None] = mapped_column(DateTime)
    cost_reconstruction_version: Mapped[str] = mapped_column(String(24), default="")
    sale_uom: Mapped[str] = mapped_column(String(20), default="BASE")
    sale_uom_factor: Mapped[int] = mapped_column(Integer, default=1)
    sale_quantity: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    net_sale_value: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    line_cost: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    discount: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0"))
    line_total: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0"))
    pack_mrp: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0"))
    units_per_pack: Mapped[int] = mapped_column(Integer, default=1)
    pack_size: Mapped[str] = mapped_column(String(60), default="")
    base_unit: Mapped[str] = mapped_column(String(20), default="UNIT")
    # POS line this row belongs to: one cashier line split across batches
    # (FEFO) becomes several rows sharing a line number.
    line_no: Mapped[int] = mapped_column(Integer, default=0)

    sale: Mapped[Sale] = relationship(back_populates="items")
    item: Mapped[Item | None] = relationship()


class Purchase(Base):
    __tablename__ = "purchases"

    """A supplier invoice. DRAFT while it is staged and reviewed; only POSTED
    purchases have put stock in (atomically); CANCELLED drafts are kept."""

    __table_args__ = (
        Index("ix_purchases_supplier_invoice", "supplier_id", "invoice_no"),
        # the same supplier invoice can be received only once (fully or partly)
        Index("uq_purchases_posted_invoice", "supplier_id", "invoice_no", unique=True,
              sqlite_where=text("status IN ('POSTED', 'PARTIAL') AND invoice_no != ''"),
              postgresql_where=text("status IN ('POSTED', 'PARTIAL') AND invoice_no != ''")),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    reference_no: Mapped[str | None] = mapped_column(String(30), unique=True)   # PUR-000001 on posting
    supplier_id: Mapped[int | None] = mapped_column(ForeignKey("suppliers.id"))
    invoice_no: Mapped[str] = mapped_column(String(60), default="", index=True)
    invoice_date: Mapped[date | None] = mapped_column(Date)
    purchase_date: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    total: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0"))
    supplier_total: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))       # as printed by the supplier
    source_file: Mapped[str] = mapped_column(String(255), default="")
    source_sha256: Mapped[str] = mapped_column(String(64), default="", index=True)
    source_format: Mapped[str] = mapped_column(String(10), default="")            # CSV / XLSX / XLS / PDF / MANUAL
    document_type: Mapped[str] = mapped_column(String(30), default="")
    extraction_meta: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(20), default="DRAFT", index=True)
    notes: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    posted_at: Mapped[datetime | None] = mapped_column(DateTime)
    posted_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime)
    cancelled_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    cancel_reason: Mapped[str] = mapped_column(Text, default="")
    charges: Mapped[dict | None] = mapped_column(JSON)       # round_off, freight, adjust, credit, debit, bill_discount %
    supply_type: Mapped[str] = mapped_column(String(8), default="", server_default="")   # INTRA (CGST+SGST) · INTER (IGST)
    column_map: Mapped[list | None] = mapped_column(JSON)    # which supplier column fed which field
    difference_ack: Mapped[bool] = mapped_column(Boolean, default=False)   # supplier-total difference accepted

    supplier: Mapped[Supplier | None] = relationship()
    items: Mapped[list["PurchaseItem"]] = relationship(
        back_populates="purchase", cascade="all, delete-orphan", order_by="PurchaseItem.id"
    )


class PurchaseItem(Base):
    __tablename__ = "purchase_items"
    __table_args__ = (
        # a box on a line belongs to the line's rack (enforced by the database, not only the screen)
        ForeignKeyConstraint(["rack_id", "box_id"], ["rack_boxes.rack_id", "rack_boxes.id"],
                             name="fk_purchase_items_rack_box"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    purchase_id: Mapped[int] = mapped_column(ForeignKey("purchases.id", ondelete="CASCADE"))
    item_id: Mapped[int | None] = mapped_column(ForeignKey("items.id"))
    product_name: Mapped[str] = mapped_column(String(250))
    description_raw: Mapped[str] = mapped_column(String(250), default="")
    batch_no: Mapped[str] = mapped_column(String(60), default="")
    expiry_date: Mapped[date | None] = mapped_column(Date)
    quantity: Mapped[int] = mapped_column(Integer, default=0)
    quantity_free: Mapped[int] = mapped_column(Integer, default=0)
    quantity_raw: Mapped[str] = mapped_column(String(30), default="")
    hsn_code: Mapped[str] = mapped_column(String(20), default="")
    pack_size: Mapped[str] = mapped_column(String(60), default="")
    manufacturer: Mapped[str] = mapped_column(String(150), default="")
    source_serial: Mapped[int | None] = mapped_column(Integer)
    source_page: Mapped[int | None] = mapped_column(Integer)
    validation_status: Mapped[str] = mapped_column(String(20), default="")
    rate: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0"))
    mrp: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0"))
    line_total: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0"))
    match_status: Mapped[str] = mapped_column(String(20), default="REVIEW")
    match_confidence: Mapped[Decimal] = mapped_column(Numeric(5, 2), default=Decimal("0"))
    # staging / review (the evidence is never lost: raw values, corrections with who/when)
    line_no: Mapped[int] = mapped_column(Integer, default=0)
    raw: Mapped[dict | None] = mapped_column(JSON)
    corrections: Mapped[dict | None] = mapped_column(JSON)
    issues: Mapped[list | None] = mapped_column(JSON)
    receipt_decision: Mapped[dict | None] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(24), default="NEEDS_REVIEW", index=True)
    supplier_code: Mapped[str] = mapped_column(String(40), default="")
    expiry_raw: Mapped[str] = mapped_column(String(40), default="")
    gst_rate: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    discount: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0"))
    match_method: Mapped[str] = mapped_column(String(20), default="")
    # a product this line creates on posting (confirmed by a person first)
    new_product: Mapped[bool] = mapped_column(Boolean, default=False)
    category: Mapped[str] = mapped_column(String(30), default="")
    dosage_form: Mapped[str] = mapped_column(String(20), default="")
    base_unit: Mapped[str] = mapped_column(String(20), default="")
    pack_unit: Mapped[str] = mapped_column(String(20), default="")
    units_per_pack: Mapped[int | None] = mapped_column(Integer)
    batch_id: Mapped[int | None] = mapped_column(ForeignKey("batches.id"))   # batch received on posting
    # GST snapshot (app/services/gst.py): what this line cost including the GST paid to the supplier
    taxable_value: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    gst_amount: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    cgst_amount: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    sgst_amount: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    igst_amount: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    landed_total: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    landed_rate: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))       # per pack, incl. GST
    gst_source: Mapped[str] = mapped_column(String(20), default="", server_default="")   # FILE · DERIVED · PRODUCT · NONE
    # where the received stock goes, confirmed by a person (a suggestion is never stored here)
    rack_id: Mapped[int | None] = mapped_column(ForeignKey("racks.id"))
    box_id: Mapped[int | None] = mapped_column(Integer)

    purchase: Mapped[Purchase] = relationship(back_populates="items")
    item: Mapped[Item | None] = relationship()
    batch: Mapped["Batch | None"] = relationship(foreign_keys=[batch_id])   # the batch this line was received into


class PurchaseReturn(Base):
    __tablename__ = "purchase_returns"

    id: Mapped[int] = mapped_column(primary_key=True)
    reference_no: Mapped[str | None] = mapped_column(String(30), unique=True)   # PR-000001
    purchase_id: Mapped[int | None] = mapped_column(ForeignKey("purchases.id"))
    purchase_item_id: Mapped[int | None] = mapped_column(ForeignKey("purchase_items.id"))
    supplier_id: Mapped[int | None] = mapped_column(ForeignKey("suppliers.id"))
    item_id: Mapped[int | None] = mapped_column(ForeignKey("items.id"))
    batch_id: Mapped[int | None] = mapped_column(ForeignKey("batches.id"))
    product_name: Mapped[str] = mapped_column(String(250), default="")
    quantity: Mapped[int] = mapped_column(Integer, default=0)
    value: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0"))
    return_date: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    reason: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(20), default="OPEN")
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    supplier: Mapped[Supplier | None] = relationship()
    item: Mapped[Item | None] = relationship()


class InventoryMovement(Base):
    """Immutable stock ledger — the audit source of truth for every quantity.

    ``quantity`` is signed and in base units. Current stock for a batch is the
    sum of its movements; ``batches.quantity`` is the maintained projection.
    Rows are never updated or deleted: a mistake is undone by a new movement
    whose ``reversal_of_id`` points at the original.
    """

    __tablename__ = "inventory_movements"
    __table_args__ = (
        Index("ix_movements_item_time", "item_id", "created_at"),
        Index("ix_movements_batch", "batch_id", "id"),
        Index("ix_movements_reference", "reference_type", "reference_id"),
        Index("ix_movements_time", "created_at", "id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id"))
    batch_id: Mapped[int] = mapped_column(ForeignKey("batches.id"))
    movement_type: Mapped[str] = mapped_column(String(24), index=True)
    quantity: Mapped[int] = mapped_column(Integer)
    unit_cost_snapshot: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    cost_amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    financial_status: Mapped[str] = mapped_column(String(24), default="LEGACY_UNCHECKED")
    balance_after: Mapped[int] = mapped_column(Integer, default=0)
    # what the document said, before conversion (e.g. 5 STRIP → +75)
    txn_quantity: Mapped[int | None] = mapped_column(Integer)
    txn_unit: Mapped[str] = mapped_column(String(10), default="BASE")
    units_per_pack: Mapped[int] = mapped_column(Integer, default=1)
    reference_type: Mapped[str] = mapped_column(String(24), default="")
    reference_id: Mapped[int | None] = mapped_column(Integer)
    reference_no: Mapped[str] = mapped_column(String(60), default="")
    reason: Mapped[str] = mapped_column(Text, default="")
    reversal_of_id: Mapped[int | None] = mapped_column(ForeignKey("inventory_movements.id"), index=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    # The three packaging levels of a receipt (purchase → retail → base), kept beside the base
    # quantity above so a receipt is never flattened to one number. Null on older movements.
    purchase_quantity: Mapped[Decimal | None] = mapped_column(Numeric(14, 4))
    purchase_uom: Mapped[str | None] = mapped_column(String(20))
    retail_quantity: Mapped[Decimal | None] = mapped_column(Numeric(14, 4))
    retail_uom: Mapped[str | None] = mapped_column(String(20))
    base_uom: Mapped[str | None] = mapped_column(String(20))

    item: Mapped[Item] = relationship()
    batch: Mapped[Batch] = relationship()
    user: Mapped[User | None] = relationship()


class ExpiryAlert(Base):
    __tablename__ = "expiry_alerts"

    id: Mapped[int] = mapped_column(primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("batches.id", ondelete="CASCADE"))
    status: Mapped[str] = mapped_column(String(20), default="ACTIVE", index=True)
    snoozed_until: Mapped[datetime | None] = mapped_column(DateTime)
    settled_quantity: Mapped[int] = mapped_column(Integer, default=0)
    settled_value: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0"))
    settled_at: Mapped[datetime | None] = mapped_column(DateTime)
    settled_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    batch: Mapped[Batch] = relationship()


class StockAdjustment(Base):
    """Loose / damage write-offs and other manual stock corrections."""

    __tablename__ = "stock_adjustments"

    id: Mapped[int] = mapped_column(primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id"))
    batch_id: Mapped[int | None] = mapped_column(ForeignKey("batches.id"))
    reference_no: Mapped[str | None] = mapped_column(String(20), unique=True)     # ADJ-000001
    direction: Mapped[str] = mapped_column(String(3), default="OUT")              # IN (surplus) / OUT (loss)
    category: Mapped[str] = mapped_column(String(20), default="LOOSE", index=True)
    quantity: Mapped[int] = mapped_column(Integer, default=0)
    value: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0"))
    reason: Mapped[str] = mapped_column(Text, default="")
    adjustment_date: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    movement_id: Mapped[int | None] = mapped_column(ForeignKey("inventory_movements.id"))   # the ledger line it posted
    reversal_of_id: Mapped[int | None] = mapped_column(ForeignKey("stock_adjustments.id"))  # this document reverses that one
    reversed_at: Mapped[datetime | None] = mapped_column(DateTime)
    reversed_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))

    item: Mapped[Item] = relationship()
    batch: Mapped["Batch | None"] = relationship()


class Notification(Base):
    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(primary_key=True)
    type: Mapped[str] = mapped_column(String(20), index=True)
    title: Mapped[str] = mapped_column(String(200))
    message: Mapped[str] = mapped_column(Text, default="")
    related_type: Mapped[str] = mapped_column(String(40), default="")
    related_id: Mapped[int | None] = mapped_column(Integer)
    due_date: Mapped[datetime | None] = mapped_column(DateTime, index=True)
    status: Mapped[str] = mapped_column(String(20), default="UNREAD", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class SaleReturn(Base):
    """A controlled refund against a completed invoice. Never edits the sale."""

    __tablename__ = "sale_returns"

    id: Mapped[int] = mapped_column(primary_key=True)
    return_no: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    sale_id: Mapped[int] = mapped_column(ForeignKey("sales.id"), index=True)
    customer_id: Mapped[int | None] = mapped_column(ForeignKey("customers.id"))
    business_date: Mapped[date] = mapped_column(Date, index=True)
    total_refund: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0"))
    refund_method: Mapped[str] = mapped_column(String(10), default="CASH")
    reason_code: Mapped[str] = mapped_column(String(40), default="")
    reason_note: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(20), default="COMPLETED")
    processed_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    processed_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    sale: Mapped["Sale"] = relationship()
    items: Mapped[list["SaleReturnItem"]] = relationship(
        back_populates="sale_return", cascade="all, delete-orphan", order_by="SaleReturnItem.id"
    )


class SaleReturnItem(Base):
    __tablename__ = "sale_return_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    return_id: Mapped[int] = mapped_column(ForeignKey("sale_returns.id", ondelete="CASCADE"))
    sale_item_id: Mapped[int | None] = mapped_column(ForeignKey("sale_items.id"))
    item_id: Mapped[int | None] = mapped_column(ForeignKey("items.id"))
    batch_id: Mapped[int | None] = mapped_column(ForeignKey("batches.id"))
    product_name: Mapped[str] = mapped_column(String(250), default="")
    batch_no: Mapped[str] = mapped_column(String(60), default="")
    expiry_date: Mapped[date | None] = mapped_column(Date)
    quantity: Mapped[int] = mapped_column(Integer, default=0)
    refund_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0"))
    disposition: Mapped[str] = mapped_column(String(20), default="RESTOCK")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    sale_return: Mapped[SaleReturn] = relationship(back_populates="items")


class ParkedSale(Base):
    """An unfinished POS transaction parked for later resume.

    Not an invoice or payment — only completion creates a sale.
    """

    __tablename__ = "parked_sales"
    __table_args__ = (Index("ix_parked_sales_status_date", "status", "business_date"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    park_reference: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    status: Mapped[str] = mapped_column(String(20), default="PARKED", index=True)
    customer_id: Mapped[int | None] = mapped_column(ForeignKey("customers.id"))
    business_date: Mapped[date] = mapped_column(Date, index=True)
    parked_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    parked_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    reason_code: Mapped[str] = mapped_column(String(40), default="")
    note: Mapped[str] = mapped_column(Text, default="")
    resumed_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    resumed_at: Mapped[datetime | None] = mapped_column(DateTime)
    completed_sale_id: Mapped[int | None] = mapped_column(ForeignKey("sales.id"))
    discarded_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    discarded_at: Mapped[datetime | None] = mapped_column(DateTime)
    discard_reason: Mapped[str] = mapped_column(String(40), default="")
    payload: Mapped[dict | None] = mapped_column(JSON)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class ItemUom(Base):
    __tablename__ = "item_uoms"
    __table_args__ = (UniqueConstraint("item_id", "unit"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id", ondelete="CASCADE"))
    unit: Mapped[str] = mapped_column(String(20))
    parent_unit: Mapped[str] = mapped_column(String(20))
    factor: Mapped[int] = mapped_column(Integer)


class DeploymentLog(Base):
    """One row per upgrade, and per first start of a new app version (written by upgrade_service)."""

    __tablename__ = "deployment_log"
    id: Mapped[int] = mapped_column(primary_key=True)
    deployed_at: Mapped[str] = mapped_column(String(32))
    app_version: Mapped[str] = mapped_column(String(32), default="", server_default="")
    build: Mapped[str] = mapped_column(String(64), default="", server_default="")
    status: Mapped[str] = mapped_column(String(20), default="", server_default="")
    from_revision: Mapped[str] = mapped_column(String(200), default="", server_default="")
    to_revision: Mapped[str] = mapped_column(String(200), default="", server_default="")
    backup_name: Mapped[str] = mapped_column(String(200), default="", server_default="")
    detail: Mapped[str] = mapped_column(Text, default="", server_default="")


WHATSAPP_STATUS = ("QUEUED", "SENDING", "SENT", "FAILED")


class WhatsAppMessage(Base):
    """One customer-requested invoice sent on WhatsApp: the delivery record kept against the sale.
    A resend is a new row for the same sale and the same stored invoice PDF — never a new sale or invoice."""

    __tablename__ = "whatsapp_messages"
    __table_args__ = (Index("ix_whatsapp_due", "status", "next_attempt_at"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    sale_id: Mapped[int] = mapped_column(ForeignKey("sales.id"), index=True)       # the invoice
    invoice_no: Mapped[str] = mapped_column(String(40), default="")
    customer_phone: Mapped[str] = mapped_column(String(20))                         # 91XXXXXXXXXX
    status: Mapped[str] = mapped_column(String(10), default="QUEUED")               # QUEUED · SENDING · SENT · FAILED
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str] = mapped_column(Text, default="")
    is_resend: Mapped[bool] = mapped_column(Boolean, default=False)
    message_text: Mapped[str] = mapped_column(Text, default="")
    pdf_path: Mapped[str] = mapped_column(String(255), default="")
    provider: Mapped[str] = mapped_column(String(20), default="")
    provider_message_id: Mapped[str] = mapped_column(String(120), default="")
    queued_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))


class WorkspaceSnapshot(Base):
    """The open work of one user at one counter (tabs, unfinished bills), saved continuously so a
    crash or power cut never loses it: reopened at the next login."""
    __tablename__ = "workspace_snapshots"
    __table_args__ = (UniqueConstraint("user_id", "terminal", name="uq_workspace_user_terminal"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    terminal: Mapped[str] = mapped_column(String(64), default="")
    data: Mapped[str] = mapped_column(Text, default="{}")
    saved_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ItemForm(Base):
    """Item form master: how a kind of product is counted (Tablets: tablets in strips;
    Syrup bottles: whole bottles). Built-in forms ship with the ERP; the pharmacy adds its own.
    The list is data, not code."""

    __tablename__ = "item_forms"

    code: Mapped[str] = mapped_column(String(20), primary_key=True)      # also the product's dosage form
    name: Mapped[str] = mapped_column(String(60), default="")
    base_unit: Mapped[str] = mapped_column(String(20), default="UNIT")   # what stock is counted in
    pack_unit: Mapped[str] = mapped_column(String(20), default="PACK")   # the retail pack it comes in
    counted: Mapped[bool] = mapped_column(Boolean, default=False)        # True: N base units per pack (tablets per strip)
    content_unit: Mapped[str] = mapped_column(String(10), default="")    # ML / G when the container holds content
    sort_order: Mapped[int] = mapped_column(Integer, default=100)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    builtin: Mapped[bool] = mapped_column(Boolean, default=False)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ProductPackaging(Base):
    """A valid pack of a product: purchase unit → retail unit → base unit, with content.
    A product may have several (1X10, 10X10, 10X1X10); one is preferred. Raw supplier text is
    kept unchanged beside the normalised reading."""

    __tablename__ = "product_packagings"
    __table_args__ = (UniqueConstraint("item_id", "normalized_packing", "purchase_to_retail", name="uq_product_packaging"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id", ondelete="CASCADE"), index=True)
    dosage_form: Mapped[str] = mapped_column(String(20), default="")
    raw_supplier_packing: Mapped[str] = mapped_column(String(60), default="")
    normalized_packing: Mapped[str] = mapped_column(String(60), default="")
    purchase_unit: Mapped[str] = mapped_column(String(20), default="")
    retail_unit: Mapped[str] = mapped_column(String(20), default="")
    base_unit: Mapped[str] = mapped_column(String(20), default="")
    purchase_to_retail: Mapped[int] = mapped_column(Integer, default=1)
    retail_to_base: Mapped[int] = mapped_column(Integer, default=1)
    container_size: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    container_size_unit: Mapped[str] = mapped_column(String(10), default="")
    allow_loose_sale: Mapped[bool] = mapped_column(Boolean, default=False)
    is_preferred: Mapped[bool] = mapped_column(Boolean, default=False)
    source: Mapped[str] = mapped_column(String(20), default="")          # PRODUCT_MASTER · USER_CORRECTION · PURCHASE
    confidence: Mapped[str] = mapped_column(String(12), default="")      # HIGH · MEDIUM · LOW · UNRESOLVED
    verified_by_user: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class SupplierPackagingAlias(Base):
    """What one supplier's printed pack text means: supplier + raw pack (+ product) → units one
    invoice Qty counts. Saved when a person confirms it; the next invoice resolves without review."""

    __tablename__ = "supplier_packaging_aliases"
    __table_args__ = (UniqueConstraint("supplier_id", "pack_key", "item_id", name="uq_supplier_packaging_alias"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    supplier_id: Mapped[int] = mapped_column(ForeignKey("suppliers.id", ondelete="CASCADE"), index=True)
    pack_key: Mapped[str] = mapped_column(String(60), default="")       # normalised pack text
    raw_pack: Mapped[str] = mapped_column(String(60), default="")
    item_id: Mapped[int | None] = mapped_column(ForeignKey("items.id", ondelete="CASCADE"))
    base_unit: Mapped[str] = mapped_column(String(20), default="")
    units_per_invoice_unit: Mapped[int] = mapped_column(Integer, default=1)
    retail_units: Mapped[int | None] = mapped_column(Integer)           # base units in the retail pack at the time
    mrp_basis: Mapped[str] = mapped_column(String(12), default="MASTER_PACK")
    occurrences: Mapped[int] = mapped_column(Integer, default=0)
    corrections: Mapped[int] = mapped_column(Integer, default=0)
    trust: Mapped[Decimal] = mapped_column(Numeric(4, 3), default=Decimal("1"))
    status: Mapped[str] = mapped_column(String(12), default="ACTIVE")    # ACTIVE · AMBIGUOUS
    source: Mapped[str] = mapped_column(String(20), default="USER_CORRECTION")
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class SupplierInvoiceProfile(Base):
    """A supplier's invoice layout as learned from confirmed imports: header tokens, column roles,
    invoice-number shape and the parser that worked. Recognises the supplier when the file carries
    no GSTIN, and is applied first next time."""

    __tablename__ = "supplier_invoice_profiles"
    __table_args__ = (UniqueConstraint("supplier_id", "layout_key", name="uq_supplier_invoice_profile"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    supplier_id: Mapped[int] = mapped_column(ForeignKey("suppliers.id", ondelete="CASCADE"), index=True)
    layout_key: Mapped[str] = mapped_column(String(64), default="", index=True)   # hash of the header tokens
    source_format: Mapped[str] = mapped_column(String(10), default="")
    header_tokens: Mapped[list | None] = mapped_column(JSON)
    column_roles: Mapped[list | None] = mapped_column(JSON)
    invoice_patterns: Mapped[list | None] = mapped_column(JSON)          # shapes such as "NR#####"
    parser: Mapped[str] = mapped_column(String(30), default="")
    invoices: Mapped[int] = mapped_column(Integer, default=0)
    confidence: Mapped[Decimal] = mapped_column(Numeric(4, 3), default=Decimal("0.9"))
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class MappingHistory(Base):
    """Every change to the mapping store (product aliases, packaging aliases, column roles,
    packaging definitions, supplier recognitions), so a bad mapping can be traced and undone."""

    __tablename__ = "mapping_history"
    __table_args__ = (Index("ix_mapping_history_kind_key", "kind", "supplier_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(24))          # PRODUCT_ALIAS · PACKAGING_ALIAS · COLUMN_ROLE · PACKAGING · SUPPLIER
    action: Mapped[str] = mapped_column(String(16))        # CREATE · CONFIRM · OVERRULE · DISABLE · BOOTSTRAP
    supplier_id: Mapped[int | None] = mapped_column(ForeignKey("suppliers.id", ondelete="SET NULL"))
    key: Mapped[str] = mapped_column(String(250), default="")
    before: Mapped[dict | None] = mapped_column(JSON)
    after: Mapped[dict | None] = mapped_column(JSON)
    purchase_id: Mapped[int | None] = mapped_column(Integer)
    line_id: Mapped[int | None] = mapped_column(Integer)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class ImportMetric(Base):
    """One row per processed supplier invoice: how much went straight through."""

    __tablename__ = "import_metrics"

    id: Mapped[int] = mapped_column(primary_key=True)
    purchase_id: Mapped[int | None] = mapped_column(Integer, index=True)
    supplier_id: Mapped[int | None] = mapped_column(Integer, index=True)
    source_format: Mapped[str] = mapped_column(String(10), default="")
    route: Mapped[str] = mapped_column(String(20), default="")          # STRUCTURED · PDF_TEXT · OCR
    lines: Mapped[int] = mapped_column(Integer, default=0)
    auto_accepted: Mapped[int] = mapped_column(Integer, default=0)
    with_warning: Mapped[int] = mapped_column(Integer, default=0)
    review: Mapped[int] = mapped_column(Integer, default=0)
    blocked: Mapped[int] = mapped_column(Integer, default=0)
    corrected: Mapped[int] = mapped_column(Integer, default=0)
    packaging_resolved: Mapped[int] = mapped_column(Integer, default=0)
    ocr_confidence: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    processing_ms: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


# --------------------------------------------------------------------------- #
# Physical locations: racks, boxes, where each product is, and every move
# (app/services/location_service.py; DECISIONS.md D25-D30)
# --------------------------------------------------------------------------- #
class Rack(Base):
    """A physical rack. Products refer to its id, never its code or name, so a rename or a
    new code never breaks a relationship. Never deleted once used: it is deactivated."""

    __tablename__ = "racks"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(30), unique=True)          # R-A01 (upper case, unique)
    name: Mapped[str] = mapped_column(String(80), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=100)
    # room for later (not used yet): zone, shelf count, capacity
    zone: Mapped[str] = mapped_column(String(40), default="", server_default="")
    shelf_count: Mapped[int | None] = mapped_column(Integer)
    capacity: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    updated_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))

    boxes: Mapped[list["RackBox"]] = relationship(back_populates="rack", order_by="RackBox.sort_order, RackBox.code")


class RackBox(Base):
    """An optional box / bin inside one rack. The code is unique within its rack only."""

    __tablename__ = "rack_boxes"
    __table_args__ = (
        UniqueConstraint("rack_id", "code", name="uq_rack_box_code"),
        # target of the composite (rack_id, box_id) keys: a box can only be used with its own rack
        UniqueConstraint("rack_id", "id", name="uq_rack_box_rack_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    rack_id: Mapped[int] = mapped_column(ForeignKey("racks.id", ondelete="RESTRICT"), index=True)
    code: Mapped[str] = mapped_column(String(30))
    name: Mapped[str] = mapped_column(String(80), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=100)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    updated_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))

    rack: Mapped[Rack] = relationship(back_populates="boxes")


class ItemLocation(Base):
    """Where a product (or, later, one of its batches) is kept, over time.

    A row is valid from ``valid_from`` until ``valid_to`` (open = current). A move closes the
    current row and opens a new one, so the location on any past date is a query, not a guess.
    ``batch_id`` NULL = the product's location (today's screens); a batch-level row is the
    extension point for stock kept in several places. At most one open row per scope.
    """

    __tablename__ = "item_locations"
    __table_args__ = (
        ForeignKeyConstraint(["rack_id", "box_id"], ["rack_boxes.rack_id", "rack_boxes.id"],
                             name="fk_item_locations_rack_box", ondelete="RESTRICT"),
        Index("uq_item_location_open_product", "item_id", unique=True,
              sqlite_where=text("valid_to IS NULL AND batch_id IS NULL"),
              postgresql_where=text("valid_to IS NULL AND batch_id IS NULL")),
        Index("uq_item_location_open_batch", "item_id", "batch_id", unique=True,
              sqlite_where=text("valid_to IS NULL AND batch_id IS NOT NULL"),
              postgresql_where=text("valid_to IS NULL AND batch_id IS NOT NULL")),
        Index("ix_item_locations_rack_open", "rack_id", "valid_to"),
        Index("ix_item_locations_box", "box_id"),
        Index("ix_item_locations_item_time", "item_id", "valid_from"),
        Index("ix_item_locations_batch", "batch_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id", ondelete="RESTRICT"))
    batch_id: Mapped[int | None] = mapped_column(ForeignKey("batches.id", ondelete="RESTRICT"))
    rack_id: Mapped[int] = mapped_column(ForeignKey("racks.id", ondelete="RESTRICT"))
    box_id: Mapped[int | None] = mapped_column(Integer)
    quantity: Mapped[int | None] = mapped_column(Integer)        # NULL: all of the scope's stock (split stock later)
    is_primary: Mapped[bool] = mapped_column(Boolean, default=True)
    valid_from: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    valid_to: Mapped[datetime | None] = mapped_column(DateTime)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    event_id: Mapped[int | None] = mapped_column(Integer)        # the location event that opened it


class LocationEvent(Base):
    """Append-only history of every location change. Codes and names are copied at the time
    of the move, so a later rename never rewrites what history says. One operation (a bulk
    move of 500 products) shares one ``operation_id``."""

    __tablename__ = "location_events"
    __table_args__ = (
        Index("ix_location_events_item_time", "item_id", "created_at"),
        Index("ix_location_events_from_rack", "from_rack_id", "created_at"),
        Index("ix_location_events_to_rack", "to_rack_id", "created_at"),
        Index("ix_location_events_time", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    operation_id: Mapped[str] = mapped_column(String(36), index=True)
    event_type: Mapped[str] = mapped_column(String(16))        # ASSIGNED · MOVED · BOX_CHANGED · UNASSIGNED
    source: Mapped[str] = mapped_column(String(16), default="MANUAL")   # MANUAL · BULK · PURCHASE · IMPORT · RACK · MIGRATION
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id", ondelete="RESTRICT"))
    batch_id: Mapped[int | None] = mapped_column(ForeignKey("batches.id", ondelete="RESTRICT"))
    from_rack_id: Mapped[int | None] = mapped_column(ForeignKey("racks.id", ondelete="RESTRICT"))
    from_box_id: Mapped[int | None] = mapped_column(ForeignKey("rack_boxes.id", ondelete="RESTRICT"))
    to_rack_id: Mapped[int | None] = mapped_column(ForeignKey("racks.id", ondelete="RESTRICT"))
    to_box_id: Mapped[int | None] = mapped_column(ForeignKey("rack_boxes.id", ondelete="RESTRICT"))
    from_label: Mapped[str] = mapped_column(String(200), default="")    # "R-A01 Antibiotics / B02 Middle" at the time
    to_label: Mapped[str] = mapped_column(String(200), default="")
    stock_snapshot: Mapped[int | None] = mapped_column(Integer)          # stock on hand when moved (information only)
    reason: Mapped[str] = mapped_column(Text, default="")
    reference: Mapped[str] = mapped_column(String(60), default="")      # PUR-000123, import file …
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    username: Mapped[str] = mapped_column(String(60), default="system")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
