"""UCP checkout handoffs

Revision ID: c7e8f9a0b1c2
Revises: b3d4e5f6a7b8
Create Date: 2026-09-04 11:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c7e8f9a0b1c2"
down_revision: str | None = "b3d4e5f6a7b8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ucp_checkout_sessions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("merchant_id", sa.Uuid(), nullable=False),
        sa.Column("agent_profile_url", sa.String(length=1000), nullable=False),
        sa.Column("protocol_version", sa.String(length=32), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("line_items", sa.JSON(), nullable=False),
        sa.Column("subtotal_minor", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("claimed_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claimed_result", sa.JSON(), nullable=True),
        sa.Column("canceled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "subtotal_minor >= 0",
            name="ck_ucp_checkout_subtotal_nonnegative",
        ),
        sa.ForeignKeyConstraint(
            ["claimed_by_user_id"],
            ["user_accounts.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["merchant_id"],
            ["merchants.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "merchant_id",
            "agent_profile_url",
            "idempotency_key",
            name="uq_ucp_checkout_agent_idempotency",
        ),
    )
    op.create_index(
        "ix_ucp_checkout_sessions_merchant_id",
        "ucp_checkout_sessions",
        ["merchant_id"],
    )
    op.create_index(
        "ix_ucp_checkout_sessions_status",
        "ucp_checkout_sessions",
        ["status"],
    )
    op.create_index(
        "ix_ucp_checkout_sessions_expires_at",
        "ucp_checkout_sessions",
        ["expires_at"],
    )
    op.create_index(
        "ix_ucp_checkout_sessions_claimed_by_user_id",
        "ucp_checkout_sessions",
        ["claimed_by_user_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_ucp_checkout_sessions_claimed_by_user_id",
        table_name="ucp_checkout_sessions",
    )
    op.drop_index(
        "ix_ucp_checkout_sessions_expires_at",
        table_name="ucp_checkout_sessions",
    )
    op.drop_index("ix_ucp_checkout_sessions_status", table_name="ucp_checkout_sessions")
    op.drop_index(
        "ix_ucp_checkout_sessions_merchant_id",
        table_name="ucp_checkout_sessions",
    )
    op.drop_table("ucp_checkout_sessions")
