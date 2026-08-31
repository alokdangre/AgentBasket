"""LangGraph checkout context and AP2 evidence

Revision ID: f1a2b3c4d5e6
Revises: d8f41c9a7e20
Create Date: 2026-08-31 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "f1a2b3c4d5e6"
down_revision: str | None = "d8f41c9a7e20"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "checkouts",
        sa.Column("source", sa.String(length=24), server_default="storefront", nullable=False),
    )
    op.alter_column("checkouts", "source", server_default=None)
    op.add_column("checkouts", sa.Column("agent_conversation_id", sa.Uuid(), nullable=True))
    op.add_column("checkouts", sa.Column("agent_run_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_checkouts_agent_conversation_id",
        "checkouts",
        "agent_conversations",
        ["agent_conversation_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_checkouts_agent_run_id",
        "checkouts",
        "agent_runs",
        ["agent_run_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_checkouts_source", "checkouts", ["source"])
    op.create_index("ix_checkouts_agent_conversation_id", "checkouts", ["agent_conversation_id"])
    op.create_index("ix_checkouts_agent_run_id", "checkouts", ["agent_run_id"])

    op.create_table(
        "ap2_trusted_issuers",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("issuer", sa.String(length=240), nullable=False),
        sa.Column("key_id", sa.String(length=120), nullable=False),
        sa.Column("role", sa.String(length=40), nullable=False),
        sa.Column("algorithm", sa.String(length=16), nullable=False),
        sa.Column("public_key_pem", sa.Text(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("issuer", "key_id", name="uq_ap2_trusted_issuer_key"),
    )
    op.create_index("ix_ap2_trusted_issuers_issuer", "ap2_trusted_issuers", ["issuer"])
    op.create_index("ix_ap2_trusted_issuers_active", "ap2_trusted_issuers", ["active"])

    op.create_table(
        "ap2_consent_challenges",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("checkout_id", sa.Uuid(), nullable=False),
        sa.Column("customer_id", sa.Uuid(), nullable=False),
        sa.Column("approval_id", sa.Uuid(), nullable=True),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("request_sha256", sa.String(length=64), nullable=False),
        sa.Column("nonce", sa.String(length=128), nullable=False),
        sa.Column("checkout_jwt", sa.Text(), nullable=False),
        sa.Column("checkout_hash", sa.String(length=64), nullable=False),
        sa.Column("display_sha256", sa.String(length=64), nullable=False),
        sa.Column("display_payload", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("approval_idempotency_key", sa.String(length=128), nullable=True),
        sa.Column("approval_request_sha256", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["approval_id"], ["checkout_approvals.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["checkout_id"], ["checkouts.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["customer_id"], ["user_accounts.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("approval_id"),
        sa.UniqueConstraint("nonce"),
        sa.UniqueConstraint("customer_id", "idempotency_key", name="uq_ap2_challenge_idempotency"),
    )
    op.create_index(
        "ix_ap2_consent_challenges_checkout_id", "ap2_consent_challenges", ["checkout_id"]
    )
    op.create_index(
        "ix_ap2_consent_challenges_customer_id", "ap2_consent_challenges", ["customer_id"]
    )
    op.create_index(
        "ix_ap2_consent_challenges_checkout_hash", "ap2_consent_challenges", ["checkout_hash"]
    )
    op.create_index("ix_ap2_consent_challenges_status", "ap2_consent_challenges", ["status"])
    op.create_index(
        "ix_ap2_consent_challenges_expires_at", "ap2_consent_challenges", ["expires_at"]
    )

    op.create_table(
        "ap2_mandates",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("challenge_id", sa.Uuid(), nullable=False),
        sa.Column("checkout_id", sa.Uuid(), nullable=False),
        sa.Column("customer_id", sa.Uuid(), nullable=False),
        sa.Column("mandate_type", sa.String(length=24), nullable=False),
        sa.Column("vct", sa.String(length=64), nullable=False),
        sa.Column("issuer", sa.String(length=240), nullable=False),
        sa.Column("key_id", sa.String(length=120), nullable=False),
        sa.Column("signed_jwt", sa.Text(), nullable=False),
        sa.Column("payload_sha256", sa.String(length=64), nullable=False),
        sa.Column("checkout_hash", sa.String(length=64), nullable=False),
        sa.Column("verification_status", sa.String(length=24), nullable=False),
        sa.Column("rejection_code", sa.String(length=120), nullable=True),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["challenge_id"], ["ap2_consent_challenges.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["checkout_id"], ["checkouts.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["customer_id"], ["user_accounts.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("payload_sha256"),
        sa.UniqueConstraint("challenge_id", "mandate_type", name="uq_ap2_mandate_type"),
    )
    op.create_index("ix_ap2_mandates_challenge_id", "ap2_mandates", ["challenge_id"])
    op.create_index("ix_ap2_mandates_checkout_id", "ap2_mandates", ["checkout_id"])
    op.create_index("ix_ap2_mandates_customer_id", "ap2_mandates", ["customer_id"])
    op.create_index("ix_ap2_mandates_mandate_type", "ap2_mandates", ["mandate_type"])
    op.create_index("ix_ap2_mandates_checkout_hash", "ap2_mandates", ["checkout_hash"])
    op.create_index("ix_ap2_mandates_verification_status", "ap2_mandates", ["verification_status"])
    op.create_index("ix_ap2_mandates_created_at", "ap2_mandates", ["created_at"])

    op.create_table(
        "ap2_receipts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("checkout_id", sa.Uuid(), nullable=False),
        sa.Column("order_id", sa.Uuid(), nullable=False),
        sa.Column("payment_id", sa.Uuid(), nullable=True),
        sa.Column("receipt_type", sa.String(length=24), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("issuer", sa.String(length=240), nullable=False),
        sa.Column("key_id", sa.String(length=120), nullable=False),
        sa.Column("reference", sa.String(length=64), nullable=False),
        sa.Column("signed_jwt", sa.Text(), nullable=False),
        sa.Column("payload_sha256", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["checkout_id"], ["checkouts.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["payment_id"], ["payments.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("payload_sha256"),
        sa.UniqueConstraint("checkout_id", "receipt_type", name="uq_ap2_checkout_receipt_type"),
    )
    op.create_index("ix_ap2_receipts_checkout_id", "ap2_receipts", ["checkout_id"])
    op.create_index("ix_ap2_receipts_order_id", "ap2_receipts", ["order_id"])
    op.create_index("ix_ap2_receipts_payment_id", "ap2_receipts", ["payment_id"])
    op.create_index("ix_ap2_receipts_receipt_type", "ap2_receipts", ["receipt_type"])
    op.create_index("ix_ap2_receipts_reference", "ap2_receipts", ["reference"])
    op.create_index("ix_ap2_receipts_created_at", "ap2_receipts", ["created_at"])


def downgrade() -> None:
    op.drop_table("ap2_receipts")
    op.drop_table("ap2_mandates")
    op.drop_table("ap2_consent_challenges")
    op.drop_table("ap2_trusted_issuers")
    op.drop_index("ix_checkouts_agent_run_id", table_name="checkouts")
    op.drop_index("ix_checkouts_agent_conversation_id", table_name="checkouts")
    op.drop_index("ix_checkouts_source", table_name="checkouts")
    op.drop_constraint("fk_checkouts_agent_run_id", "checkouts", type_="foreignkey")
    op.drop_constraint("fk_checkouts_agent_conversation_id", "checkouts", type_="foreignkey")
    op.drop_column("checkouts", "agent_run_id")
    op.drop_column("checkouts", "agent_conversation_id")
    op.drop_column("checkouts", "source")
