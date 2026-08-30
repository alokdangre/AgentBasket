"""payment approvals and razorpay boundary

Revision ID: c4b52a7316d2
Revises: 9e2ad91fd7c1
Create Date: 2026-08-30 14:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c4b52a7316d2"
down_revision: str | None = "9e2ad91fd7c1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("uq_checkout_idempotency", "checkouts", type_="unique")
    op.create_unique_constraint(
        "uq_checkout_customer_idempotency",
        "checkouts",
        ["merchant_id", "customer_id", "idempotency_key"],
    )
    op.create_table(
        "checkout_approvals",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("checkout_id", sa.Uuid(), nullable=False),
        sa.Column("customer_id", sa.Uuid(), nullable=False),
        sa.Column("quote_version", sa.Integer(), nullable=False),
        sa.Column("approved_total_minor", sa.Integer(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("evidence_sha256", sa.String(length=64), nullable=False),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "approved_total_minor >= 0", name="ck_checkout_approval_total_nonnegative"
        ),
        sa.ForeignKeyConstraint(["checkout_id"], ["checkouts.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["customer_id"], ["user_accounts.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("checkout_id", name="uq_checkout_approval_checkout"),
        sa.UniqueConstraint("evidence_sha256"),
    )
    op.create_index("ix_checkout_approvals_approved_at", "checkout_approvals", ["approved_at"])
    op.create_index("ix_checkout_approvals_checkout_id", "checkout_approvals", ["checkout_id"])
    op.create_index("ix_checkout_approvals_customer_id", "checkout_approvals", ["customer_id"])

    op.add_column("payments", sa.Column("approval_id", sa.Uuid(), nullable=True))
    op.add_column("payments", sa.Column("provider_receipt", sa.String(length=40), nullable=True))
    op.create_foreign_key(
        "fk_payments_approval_id_checkout_approvals",
        "payments",
        "checkout_approvals",
        ["approval_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index("ix_payments_approval_id", "payments", ["approval_id"])
    op.create_index("ix_payments_provider_receipt", "payments", ["provider_receipt"], unique=True)
    op.create_unique_constraint("uq_payment_order_provider", "payments", ["order_id", "provider"])


def downgrade() -> None:
    op.drop_constraint("uq_payment_order_provider", "payments", type_="unique")
    op.drop_index("ix_payments_provider_receipt", table_name="payments")
    op.drop_index("ix_payments_approval_id", table_name="payments")
    op.drop_constraint("fk_payments_approval_id_checkout_approvals", "payments", type_="foreignkey")
    op.drop_column("payments", "provider_receipt")
    op.drop_column("payments", "approval_id")
    op.drop_table("checkout_approvals")
    op.drop_constraint("uq_checkout_customer_idempotency", "checkouts", type_="unique")
    op.create_unique_constraint(
        "uq_checkout_idempotency", "checkouts", ["merchant_id", "idempotency_key"]
    )
