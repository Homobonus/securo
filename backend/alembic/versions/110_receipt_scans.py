"""receipt scans: photos of paper receipts waiting for review

One new table; no existing row changes.

Revision ID: 110
Revises: 109

First shipped as 100; see 109_receipts for the renumbering.
"""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "110"
down_revision = "109"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if sa.inspect(op.get_bind()).has_table("receipt_scans"):
        return  # created under this fork's old number (100)
    op.create_table(
        "receipt_scans",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("status", sa.String(length=20), server_default="processing", nullable=False),
        sa.Column("storage_key", sa.String(length=500), nullable=False),
        sa.Column("content_type", sa.String(length=100), nullable=False),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.Column("error", sa.String(length=500), nullable=True),
        sa.Column("model", sa.String(length=100), nullable=True),
        sa.Column("receipt_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["receipt_id"], ["receipts.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "status IN ('processing', 'ready', 'failed', 'accepted')",
            name="ck_receipt_scans_status",
        ),
    )
    op.create_index("ix_receipt_scans_workspace_id", "receipt_scans", ["workspace_id"])


def downgrade() -> None:
    op.drop_table("receipt_scans")
