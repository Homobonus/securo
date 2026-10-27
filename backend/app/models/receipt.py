"""Store receipts: what was bought, where, and for how much.

Bank sync knows that 104.37 left the account at a Lidl. A loyalty account
knows what those 104.37 were: twelve lines of coffee, detergent and
bread. This module keeps the second half and ties it to the first.

  - A **retail connection** is one loyalty account (Lidl Plus, Moja
    Biedronka) the workspace reads e-receipts from. It is not a bank
    connection: it creates no accounts and no transactions, so it lives
    in its own table and never touches the ledger.
  - A **receipt** is one purchase, as the store issued it. It may point
    at the bank transaction that paid for it; the pairing is what makes a
    single "LIDL SP. Z O.O." line explainable.
  - A **receipt item** is one line of that receipt, kept as printed.
  - A **shopping product** is what the person actually buys, across
    receipts and stores. Items reach it through **aliases**, one per
    printed name, so "Kawa ziarn. 1kg" bought twelve times is one product
    with twelve prices, and two spellings merged by hand stay merged on
    the next sync.

Deliberately separate from `products`: that catalog is what a workspace
*sells* and fills invoice lines. Nothing here is ever invoiced.
"""
import uuid
from datetime import date as _date, datetime, timezone
from decimal import Decimal
from typing import TYPE_CHECKING, Optional

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base

if TYPE_CHECKING:
    from app.models.category import Category
    from app.models.transaction import Transaction

#: Lifecycle of a loyalty-account link. `pending` holds a PKCE verifier
#: and waits for the person to sign in; `reauth_required` means the
#: store revoked the refresh token and only a new sign-in helps; `error`
#: is anything transient, retried on the next scheduled sync.
RETAIL_CONNECTION_STATUSES = ("pending", "active", "reauth_required", "error")

#: How a receipt came to point at a transaction. `auto` was decided by
#: the matcher and may be undone by it never again; `manual` was chosen
#: by a person and is never touched by the matcher.
RECEIPT_MATCH_METHODS = ("auto", "manual")


def _now() -> datetime:
    return datetime.now(timezone.utc)


class RetailConnection(Base):
    """One loyalty account the workspace reads e-receipts from."""

    __tablename__ = "retail_connections"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'active', 'reauth_required', 'error')",
            name="ck_retail_connections_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    provider: Mapped[str] = mapped_column(String(50))
    display_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    # Fernet ciphertext of a JSON object: tokens once active, the PKCE
    # verifier while pending. Never returned by the API.
    credentials: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Plain options: `match_account_id` restricts pairing to one account.
    settings: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String(30), default="pending", server_default="pending")
    last_error: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    last_sync_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Receipt(Base):
    """One purchase as the store issued it."""

    __tablename__ = "receipts"
    __table_args__ = (
        # A re-sync converges on the same row: the store's id is the identity.
        UniqueConstraint("workspace_id", "source", "external_id", name="uq_receipts_workspace_external"),
        Index("ix_receipts_workspace_date", "workspace_id", "date"),
        CheckConstraint(
            "match_method IS NULL OR match_method IN ('auto', 'manual')",
            name="ck_receipts_match_method",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    # SET NULL: removing the loyalty link keeps the shopping history.
    connection_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("retail_connections.id", ondelete="SET NULL"), nullable=True
    )
    # The provider name (`lidl_plus`, `biedronka`), also the store chain.
    source: Mapped[str] = mapped_column(String(50))
    external_id: Mapped[str] = mapped_column(String(255))
    purchased_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    date: Mapped[_date] = mapped_column(Date)
    store_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    store_address: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    total: Mapped[Decimal] = mapped_column(Numeric(precision=15, scale=2))
    # Sum of the discounts the receipt prints, positive. Zero when none.
    discount_total: Mapped[Decimal] = mapped_column(
        Numeric(precision=15, scale=2), default=Decimal("0"), server_default="0"
    )
    currency: Mapped[str] = mapped_column(String(3), default="PLN", server_default="PLN")
    transaction_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("transactions.id", ondelete="SET NULL"), nullable=True, index=True
    )
    match_method: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)
    # The store's own payload, so a better parser can re-read old receipts
    # without asking the store again.
    raw_data: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    items: Mapped[list["ReceiptItem"]] = relationship(
        back_populates="receipt",
        cascade="all, delete-orphan",
        order_by="ReceiptItem.position",
        lazy="selectin",
    )
    transaction: Mapped[Optional["Transaction"]] = relationship()


class ReceiptItem(Base):
    """One printed line of a receipt."""

    __tablename__ = "receipt_items"
    __table_args__ = (Index("ix_receipt_items_product", "product_id"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    receipt_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("receipts.id", ondelete="CASCADE"), index=True
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    position: Mapped[int] = mapped_column(Integer, default=0)
    name: Mapped[str] = mapped_column(String(255))
    # The store's article code or EAN when the receipt carries one.
    code: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    quantity: Mapped[Decimal] = mapped_column(Numeric(precision=12, scale=3), default=Decimal("1"))
    # `szt` or `kg`, as printed or inferred from a fractional quantity.
    unit: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)
    unit_price: Mapped[Decimal] = mapped_column(Numeric(precision=15, scale=2))
    # Shelf value of the line (quantity x unit price), before discounts.
    total: Mapped[Decimal] = mapped_column(Numeric(precision=15, scale=2))
    discount: Mapped[Decimal] = mapped_column(
        Numeric(precision=15, scale=2), default=Decimal("0"), server_default="0"
    )
    product_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("shopping_products.id", ondelete="SET NULL"), nullable=True
    )

    receipt: Mapped["Receipt"] = relationship(back_populates="items")
    product: Mapped[Optional["ShoppingProduct"]] = relationship(back_populates="items")


class ShoppingProduct(Base):
    """Something the person buys, across receipts and stores."""

    __tablename__ = "shopping_products"
    __table_args__ = (Index("ix_shopping_products_workspace_name", "workspace_id", "name"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(255))
    category_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("categories.id", ondelete="SET NULL"), nullable=True
    )
    # Opt-in: only consumables the person cares to forecast (coffee,
    # dishwasher tablets) get "how long does it last" on the list.
    track_consumption: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    notes: Mapped[Optional[str]] = mapped_column(String(1000), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    category: Mapped[Optional["Category"]] = relationship()
    aliases: Mapped[list["ShoppingProductAlias"]] = relationship(
        back_populates="product", cascade="all, delete-orphan", lazy="selectin"
    )
    items: Mapped[list["ReceiptItem"]] = relationship(back_populates="product")


class ShoppingProductAlias(Base):
    """One printed name, at one store, that means a shopping product."""

    __tablename__ = "shopping_product_aliases"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "source", "normalized_name", name="uq_shopping_product_aliases_name"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    product_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("shopping_products.id", ondelete="CASCADE"), index=True
    )
    source: Mapped[str] = mapped_column(String(50))
    name: Mapped[str] = mapped_column(String(255))
    normalized_name: Mapped[str] = mapped_column(String(255))

    product: Mapped["ShoppingProduct"] = relationship(back_populates="aliases")
