"""accounts carts and operations

Revision ID: 9e2ad91fd7c1
Revises: 6c88e7c6b354
Create Date: 2026-08-30 10:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "9e2ad91fd7c1"
down_revision: str | None = "6c88e7c6b354"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "user_accounts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("password_hash", sa.String(length=512), nullable=False),
        sa.Column("full_name", sa.String(length=160), nullable=False),
        sa.Column("phone", sa.String(length=32), nullable=True),
        sa.Column(
            "role",
            sa.Enum(
                "CUSTOMER",
                "MERCHANT_ADMIN",
                "MERCHANT_STAFF",
                name="userrole",
                native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column("merchant_id", sa.Uuid(), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["merchant_id"], ["merchants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_user_accounts_active", "user_accounts", ["active"])
    op.create_index("ix_user_accounts_email", "user_accounts", ["email"], unique=True)
    op.create_index("ix_user_accounts_merchant_id", "user_accounts", ["merchant_id"])
    op.create_index("ix_user_accounts_role", "user_accounts", ["role"])

    op.create_table(
        "auth_sessions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("token_sha256", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["user_accounts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_auth_sessions_expires_at", "auth_sessions", ["expires_at"])
    op.create_index("ix_auth_sessions_revoked_at", "auth_sessions", ["revoked_at"])
    op.create_index("ix_auth_sessions_token_sha256", "auth_sessions", ["token_sha256"], unique=True)
    op.create_index("ix_auth_sessions_user_id", "auth_sessions", ["user_id"])

    op.create_table(
        "customer_addresses",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("label", sa.String(length=80), nullable=False),
        sa.Column("recipient_name", sa.String(length=160), nullable=False),
        sa.Column("phone", sa.String(length=32), nullable=False),
        sa.Column("line_one", sa.String(length=200), nullable=False),
        sa.Column("line_two", sa.String(length=200), nullable=True),
        sa.Column("landmark", sa.String(length=160), nullable=True),
        sa.Column("city", sa.String(length=120), nullable=False),
        sa.Column("region", sa.String(length=120), nullable=False),
        sa.Column("postal_code", sa.String(length=20), nullable=False),
        sa.Column("country_code", sa.String(length=2), nullable=False),
        sa.Column("is_default", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["user_accounts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_customer_addresses_is_default", "customer_addresses", ["is_default"])
    op.create_index("ix_customer_addresses_postal_code", "customer_addresses", ["postal_code"])
    op.create_index("ix_customer_addresses_user_id", "customer_addresses", ["user_id"])

    op.create_table(
        "carts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("merchant_id", sa.Uuid(), nullable=False),
        sa.Column(
            "status",
            sa.Enum("ACTIVE", "CONVERTED", "ABANDONED", name="cartstatus", native_enum=False),
            nullable=False,
        ),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["merchant_id"], ["merchants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["user_accounts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "merchant_id", name="uq_cart_user_merchant"),
    )
    op.create_index("ix_carts_merchant_id", "carts", ["merchant_id"])
    op.create_index("ix_carts_status", "carts", ["status"])
    op.create_index("ix_carts_user_id", "carts", ["user_id"])

    op.create_table(
        "cart_items",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("cart_id", sa.Uuid(), nullable=False),
        sa.Column("variant_id", sa.Uuid(), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("modifier_option_ids", sa.JSON(), nullable=False),
        sa.Column("modifier_signature", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("quantity > 0", name="ck_cart_item_quantity_positive"),
        sa.ForeignKeyConstraint(["cart_id"], ["carts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["variant_id"], ["product_variants.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "cart_id", "variant_id", "modifier_signature", name="uq_cart_item_configuration"
        ),
    )
    op.create_index("ix_cart_items_cart_id", "cart_items", ["cart_id"])
    op.create_index("ix_cart_items_variant_id", "cart_items", ["variant_id"])

    op.add_column("checkouts", sa.Column("customer_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_checkouts_customer_id_user_accounts",
        "checkouts",
        "user_accounts",
        ["customer_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_checkouts_customer_id", "checkouts", ["customer_id"])

    op.add_column("orders", sa.Column("customer_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_orders_customer_id_user_accounts",
        "orders",
        "user_accounts",
        ["customer_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_orders_customer_id", "orders", ["customer_id"])


def downgrade() -> None:
    op.drop_index("ix_orders_customer_id", table_name="orders")
    op.drop_constraint("fk_orders_customer_id_user_accounts", "orders", type_="foreignkey")
    op.drop_column("orders", "customer_id")
    op.drop_index("ix_checkouts_customer_id", table_name="checkouts")
    op.drop_constraint("fk_checkouts_customer_id_user_accounts", "checkouts", type_="foreignkey")
    op.drop_column("checkouts", "customer_id")
    op.drop_table("cart_items")
    op.drop_table("carts")
    op.drop_table("customer_addresses")
    op.drop_table("auth_sessions")
    op.drop_table("user_accounts")
