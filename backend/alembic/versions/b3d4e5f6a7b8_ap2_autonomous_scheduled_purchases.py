"""AP2 autonomous scheduled purchases

Revision ID: b3d4e5f6a7b8
Revises: a2c3d4e5f6a7
Create Date: 2026-09-01 16:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b3d4e5f6a7b8"
down_revision: str | None = "a2c3d4e5f6a7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "scheduled_purchase_intents",
        "status",
        existing_type=sa.String(length=9),
        type_=sa.String(length=32),
        existing_nullable=False,
    )
    columns = (
        sa.Column("customer_id", sa.Uuid(), nullable=True),
        sa.Column("address_id", sa.Uuid(), nullable=True),
        sa.Column("payment_instrument_id", sa.Uuid(), nullable=True),
        sa.Column("location_id", sa.Uuid(), nullable=True),
        sa.Column("idempotency_key", sa.String(length=128), nullable=True),
        sa.Column("request_sha256", sa.String(length=64), nullable=True),
        sa.Column("fulfillment_type", sa.String(length=32), nullable=True),
        sa.Column("frequency", sa.String(length=16), nullable=False, server_default="once"),
        sa.Column("interval_count", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("timezone", sa.String(length=64), nullable=False, server_default="Asia/Kolkata"),
        sa.Column("max_occurrences", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("successful_occurrences", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_amount_minor", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_total_minor", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("spent_minor", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("currency", sa.String(length=3), nullable=False, server_default="INR"),
        sa.Column("address_snapshot", sa.JSON(), nullable=True),
        sa.Column("address_sha256", sa.String(length=64), nullable=True),
        sa.Column("display_payload", sa.JSON(), nullable=True),
        sa.Column("display_sha256", sa.String(length=64), nullable=True),
        sa.Column("authorization_nonce", sa.String(length=128), nullable=True),
        sa.Column(
            "authorization_challenge_idempotency_key",
            sa.String(length=128),
            nullable=True,
        ),
        sa.Column(
            "authorization_challenge_request_sha256",
            sa.String(length=64),
            nullable=True,
        ),
        sa.Column("approval_idempotency_key", sa.String(length=128), nullable=True),
        sa.Column("approval_request_sha256", sa.String(length=64), nullable=True),
        sa.Column("webauthn_challenge", sa.String(length=256), nullable=True),
        sa.Column("authorization_challenge_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("agent_key_id", sa.String(length=120), nullable=True),
        sa.Column("agent_public_jwk", sa.JSON(), nullable=True),
        sa.Column("open_checkout_mandate", sa.Text(), nullable=True),
        sa.Column("open_payment_mandate", sa.Text(), nullable=True),
        sa.Column("open_checkout_hash", sa.String(length=128), nullable=True),
        sa.Column("authorized_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("provider_authorized_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_execution_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("paused_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_failure_code", sa.String(length=120), nullable=True),
        sa.Column("last_failure_message", sa.Text(), nullable=True),
    )
    for column in columns:
        op.add_column("scheduled_purchase_intents", column)

    # Legacy scheduler rows lack the identity, instrument, fulfilment, and
    # signed-policy evidence required by the AP2 executor. Retain them for
    # history, but make them impossible for the new worker to execute.
    op.execute(
        sa.text(
            "UPDATE scheduled_purchase_intents "
            "SET status = 'expired', next_run_at = NULL "
            "WHERE customer_id IS NULL "
            "OR payment_instrument_id IS NULL "
            "OR location_id IS NULL"
        )
    )

    for name, target, ondelete in (
        ("customer_id", "user_accounts", "RESTRICT"),
        ("address_id", "customer_addresses", "RESTRICT"),
        ("payment_instrument_id", "payment_instruments", "RESTRICT"),
        ("location_id", "locations", "RESTRICT"),
    ):
        op.create_foreign_key(
            f"fk_scheduled_purchase_intents_{name}",
            "scheduled_purchase_intents",
            target,
            [name],
            ["id"],
            ondelete=ondelete,
        )
        op.create_index(
            f"ix_scheduled_purchase_intents_{name}",
            "scheduled_purchase_intents",
            [name],
        )

    op.create_index(
        "ix_scheduled_purchase_intents_status", "scheduled_purchase_intents", ["status"]
    )
    op.create_index(
        "ix_scheduled_purchase_intents_display_sha256",
        "scheduled_purchase_intents",
        ["display_sha256"],
    )
    op.create_index(
        "ix_scheduled_purchase_intents_open_checkout_hash",
        "scheduled_purchase_intents",
        ["open_checkout_hash"],
    )
    op.create_index(
        "ix_scheduled_purchase_intents_next_execution_at",
        "scheduled_purchase_intents",
        ["next_execution_at"],
    )
    op.create_unique_constraint(
        "uq_scheduled_purchase_intents_authorization_nonce",
        "scheduled_purchase_intents",
        ["authorization_nonce"],
    )
    op.create_unique_constraint(
        "uq_scheduled_intent_customer_idempotency",
        "scheduled_purchase_intents",
        ["customer_id", "idempotency_key"],
    )
    op.create_unique_constraint(
        "uq_scheduled_intent_customer_challenge_idempotency",
        "scheduled_purchase_intents",
        ["customer_id", "authorization_challenge_idempotency_key"],
    )
    op.create_unique_constraint(
        "uq_scheduled_purchase_intents_webauthn_challenge",
        "scheduled_purchase_intents",
        ["webauthn_challenge"],
    )
    op.create_index(
        "ix_scheduled_purchase_due",
        "scheduled_purchase_intents",
        ["status", "next_run_at"],
    )
    for constraint in (
        sa.CheckConstraint("interval_count > 0", name="ck_scheduled_intent_interval_positive"),
        sa.CheckConstraint("max_occurrences > 0", name="ck_scheduled_intent_occurrences_positive"),
        sa.CheckConstraint(
            "successful_occurrences >= 0", name="ck_scheduled_intent_successes_nonnegative"
        ),
        sa.CheckConstraint("max_amount_minor >= 0", name="ck_scheduled_intent_order_cap"),
        sa.CheckConstraint("max_total_minor >= 0", name="ck_scheduled_intent_total_cap"),
        sa.CheckConstraint(
            "max_total_minor >= max_amount_minor",
            name="ck_scheduled_intent_total_covers_order",
        ),
        sa.CheckConstraint("spent_minor >= 0", name="ck_scheduled_intent_spent_nonnegative"),
        sa.CheckConstraint(
            "spent_minor <= max_total_minor",
            name="ck_scheduled_intent_spent_within_budget",
        ),
        sa.CheckConstraint(
            "successful_occurrences <= max_occurrences",
            name="ck_scheduled_intent_successes_within_limit",
        ),
    ):
        op.create_check_constraint(
            constraint.name,
            "scheduled_purchase_intents",
            str(constraint.sqltext),
        )

    op.create_table(
        "scheduled_purchase_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("intent_id", sa.Uuid(), nullable=False),
        sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("checkout_id", sa.Uuid(), nullable=True),
        sa.Column("order_id", sa.Uuid(), nullable=True),
        sa.Column("payment_id", sa.Uuid(), nullable=True),
        sa.Column("amount_minor", sa.Integer(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("merchant_checkout_jwt", sa.Text(), nullable=True),
        sa.Column("merchant_checkout_hash", sa.String(length=128), nullable=True),
        sa.Column("closed_checkout_mandate", sa.Text(), nullable=True),
        sa.Column("closed_payment_mandate", sa.Text(), nullable=True),
        sa.Column("provider_notification_id", sa.String(length=120), nullable=True),
        sa.Column("provider_payment_after", sa.DateTime(timezone=True), nullable=True),
        sa.Column("provider_order_id", sa.String(length=120), nullable=True),
        sa.Column("provider_payment_id", sa.String(length=120), nullable=True),
        sa.Column("failure_code", sa.String(length=120), nullable=True),
        sa.Column("failure_message", sa.Text(), nullable=True),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("attempt_count >= 0", name="ck_scheduled_run_attempt_nonnegative"),
        sa.CheckConstraint("amount_minor >= 0", name="ck_scheduled_run_amount_nonnegative"),
        sa.ForeignKeyConstraint(
            ["intent_id"], ["scheduled_purchase_intents.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["checkout_id"], ["checkouts.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["payment_id"], ["payments.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("checkout_id"),
        sa.UniqueConstraint("order_id"),
        sa.UniqueConstraint("payment_id"),
        sa.UniqueConstraint("idempotency_key", name="uq_scheduled_run_idempotency"),
        sa.UniqueConstraint(
            "intent_id", "scheduled_for", name="uq_scheduled_run_intent_occurrence"
        ),
    )
    for column in (
        "intent_id",
        "scheduled_for",
        "status",
        "checkout_id",
        "order_id",
        "payment_id",
        "merchant_checkout_hash",
        "provider_notification_id",
        "provider_payment_after",
        "provider_order_id",
        "provider_payment_id",
        "failure_code",
    ):
        op.create_index(f"ix_scheduled_purchase_runs_{column}", "scheduled_purchase_runs", [column])


def downgrade() -> None:
    op.drop_table("scheduled_purchase_runs")
    for name in (
        "ck_scheduled_intent_interval_positive",
        "ck_scheduled_intent_occurrences_positive",
        "ck_scheduled_intent_successes_nonnegative",
        "ck_scheduled_intent_order_cap",
        "ck_scheduled_intent_total_cap",
        "ck_scheduled_intent_total_covers_order",
        "ck_scheduled_intent_spent_nonnegative",
        "ck_scheduled_intent_spent_within_budget",
        "ck_scheduled_intent_successes_within_limit",
    ):
        op.drop_constraint(name, "scheduled_purchase_intents", type_="check")
    op.drop_index("ix_scheduled_purchase_due", table_name="scheduled_purchase_intents")
    op.drop_constraint(
        "uq_scheduled_purchase_intents_webauthn_challenge",
        "scheduled_purchase_intents",
        type_="unique",
    )
    op.drop_constraint(
        "uq_scheduled_purchase_intents_authorization_nonce",
        "scheduled_purchase_intents",
        type_="unique",
    )
    op.drop_constraint(
        "uq_scheduled_intent_customer_idempotency",
        "scheduled_purchase_intents",
        type_="unique",
    )
    op.drop_constraint(
        "uq_scheduled_intent_customer_challenge_idempotency",
        "scheduled_purchase_intents",
        type_="unique",
    )
    for column in ("next_execution_at", "open_checkout_hash", "display_sha256", "status"):
        op.drop_index(
            f"ix_scheduled_purchase_intents_{column}", table_name="scheduled_purchase_intents"
        )
    for name in ("location_id", "payment_instrument_id", "address_id", "customer_id"):
        op.drop_index(
            f"ix_scheduled_purchase_intents_{name}", table_name="scheduled_purchase_intents"
        )
        op.drop_constraint(
            f"fk_scheduled_purchase_intents_{name}",
            "scheduled_purchase_intents",
            type_="foreignkey",
        )
    for name in (
        "last_failure_message",
        "last_failure_code",
        "revoked_at",
        "paused_at",
        "provider_authorized_at",
        "next_execution_at",
        "authorized_at",
        "open_checkout_hash",
        "open_payment_mandate",
        "open_checkout_mandate",
        "agent_public_jwk",
        "agent_key_id",
        "authorization_challenge_expires_at",
        "webauthn_challenge",
        "authorization_nonce",
        "approval_request_sha256",
        "approval_idempotency_key",
        "authorization_challenge_request_sha256",
        "authorization_challenge_idempotency_key",
        "display_sha256",
        "display_payload",
        "address_sha256",
        "address_snapshot",
        "currency",
        "spent_minor",
        "max_total_minor",
        "max_amount_minor",
        "successful_occurrences",
        "max_occurrences",
        "timezone",
        "interval_count",
        "frequency",
        "fulfillment_type",
        "request_sha256",
        "idempotency_key",
        "location_id",
        "payment_instrument_id",
        "address_id",
        "customer_id",
    ):
        op.drop_column("scheduled_purchase_intents", name)
    op.alter_column(
        "scheduled_purchase_intents",
        "status",
        existing_type=sa.String(length=32),
        type_=sa.String(length=9),
        existing_nullable=False,
    )
