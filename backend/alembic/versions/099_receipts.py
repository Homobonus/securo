"""store receipts: loyalty connections, receipts, their lines, shopping products

  - `retail_connections` is one loyalty account (Lidl Plus, Moja
    Biedronka) e-receipts are read from. Credentials are a Fernet
    ciphertext; nothing here creates accounts or transactions.
  - `receipts` / `receipt_items` are purchases as the store issued them,
    optionally pointing at the bank transaction that paid.
  - `shopping_products` / `shopping_product_aliases` group printed names
    into the things a person buys, for price history and consumption.

All new tables; no existing row changes.

Revision ID: 099
Revises: 098
"""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "099"
down_revision = "098"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "retail_connections",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("provider", sa.String(length=50), nullable=False),
        sa.Column("display_name", sa.String(length=255), nullable=True),
        sa.Column("credentials", sa.Text(), nullable=True),
        sa.Column("settings", sa.JSON(), nullable=True),
        sa.Column("status", sa.String(length=30), server_default="pending", nullable=False),
        sa.Column("last_error", sa.String(length=500), nullable=True),
        sa.Column("last_sync_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "status IN ('pending', 'active', 'reauth_required', 'error')",
            name="ck_retail_connections_status",
        ),
    )
    op.create_index("ix_retail_connections_workspace_id", "retail_connections", ["workspace_id"])

    op.create_table(
        "shopping_products",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("category_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("track_consumption", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("notes", sa.String(length=1000), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["category_id"], ["categories.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_shopping_products_workspace_id", "shopping_products", ["workspace_id"])
    op.create_index(
        "ix_shopping_products_workspace_name", "shopping_products", ["workspace_id", "name"]
    )

    op.create_table(
        "shopping_product_aliases",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("product_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source", sa.String(length=50), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("normalized_name", sa.String(length=255), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["product_id"], ["shopping_products.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "workspace_id", "source", "normalized_name", name="uq_shopping_product_aliases_name"
        ),
    )
    op.create_index(
        "ix_shopping_product_aliases_workspace_id", "shopping_product_aliases", ["workspace_id"]
    )
    op.create_index(
        "ix_shopping_product_aliases_product_id", "shopping_product_aliases", ["product_id"]
    )

    op.create_table(
        "receipts",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("connection_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("source", sa.String(length=50), nullable=False),
        sa.Column("external_id", sa.String(length=255), nullable=False),
        sa.Column("purchased_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("store_name", sa.String(length=255), nullable=True),
        sa.Column("store_address", sa.String(length=500), nullable=True),
        sa.Column("total", sa.Numeric(precision=15, scale=2), nullable=False),
        sa.Column(
            "discount_total", sa.Numeric(precision=15, scale=2), server_default="0", nullable=False
        ),
        sa.Column("currency", sa.String(length=3), server_default="PLN", nullable=False),
        sa.Column("transaction_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("match_method", sa.String(length=10), nullable=True),
        sa.Column("raw_data", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["connection_id"], ["retail_connections.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["transaction_id"], ["transactions.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "workspace_id", "source", "external_id", name="uq_receipts_workspace_external"
        ),
        sa.CheckConstraint(
            "match_method IS NULL OR match_method IN ('auto', 'manual')",
            name="ck_receipts_match_method",
        ),
    )
    op.create_index("ix_receipts_workspace_id", "receipts", ["workspace_id"])
    op.create_index("ix_receipts_workspace_date", "receipts", ["workspace_id", "date"])
    op.create_index("ix_receipts_transaction_id", "receipts", ["transaction_id"])

    op.create_table(
        "receipt_items",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("receipt_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("code", sa.String(length=64), nullable=True),
        sa.Column("quantity", sa.Numeric(precision=12, scale=3), nullable=False),
        sa.Column("unit", sa.String(length=10), nullable=True),
        sa.Column("unit_price", sa.Numeric(precision=15, scale=2), nullable=False),
        sa.Column("total", sa.Numeric(precision=15, scale=2), nullable=False),
        sa.Column("discount", sa.Numeric(precision=15, scale=2), server_default="0", nullable=False),
        sa.Column("product_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.ForeignKeyConstraint(["receipt_id"], ["receipts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["product_id"], ["shopping_products.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_receipt_items_receipt_id", "receipt_items", ["receipt_id"])
    op.create_index("ix_receipt_items_workspace_id", "receipt_items", ["workspace_id"])
    op.create_index("ix_receipt_items_product", "receipt_items", ["product_id"])


def downgrade() -> None:
    op.drop_table("receipt_items")
    op.drop_table("receipts")
    op.drop_table("shopping_product_aliases")
    op.drop_table("shopping_products")
    op.drop_table("retail_connections")
