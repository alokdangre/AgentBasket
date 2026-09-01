"""AP2 Trusted Surface passkeys and Credentials Provider grants

Revision ID: a2c3d4e5f6a7
Revises: f1a2b3c4d5e6
Create Date: 2026-09-01 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a2c3d4e5f6a7"
down_revision: str | None = "f1a2b3c4d5e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "webauthn_credentials",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("credential_id", sa.String(length=512), nullable=False),
        sa.Column("public_key", sa.LargeBinary(), nullable=False),
        sa.Column("sign_count", sa.Integer(), nullable=False),
        sa.Column("transports", sa.JSON(), nullable=False),
        sa.Column("device_type", sa.String(length=32), nullable=False),
        sa.Column("backed_up", sa.Boolean(), nullable=False),
        sa.Column("label", sa.String(length=80), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["user_accounts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("credential_id"),
    )
    op.create_index("ix_webauthn_credentials_user_id", "webauthn_credentials", ["user_id"])
    op.create_index(
        "ix_webauthn_credentials_credential_id",
        "webauthn_credentials",
        ["credential_id"],
        unique=True,
    )

    op.create_table(
        "webauthn_ceremonies",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("purpose", sa.String(length=32), nullable=False),
        sa.Column("challenge", sa.String(length=256), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["user_accounts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("challenge"),
    )
    op.create_index("ix_webauthn_ceremonies_user_id", "webauthn_ceremonies", ["user_id"])
    op.create_index("ix_webauthn_ceremonies_purpose", "webauthn_ceremonies", ["purpose"])
    op.create_index("ix_webauthn_ceremonies_status", "webauthn_ceremonies", ["status"])
    op.create_index("ix_webauthn_ceremonies_expires_at", "webauthn_ceremonies", ["expires_at"])

    op.create_table(
        "payment_instruments",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("provider", sa.String(length=40), nullable=False),
        sa.Column("instrument_type", sa.String(length=120), nullable=False),
        sa.Column("alias", sa.String(length=160), nullable=False),
        sa.Column("provider_customer_id", sa.String(length=120), nullable=True),
        sa.Column("provider_token_reference", sa.String(length=240), nullable=True),
        sa.Column("network", sa.String(length=40), nullable=True),
        sa.Column("last4", sa.String(length=4), nullable=True),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("is_default", sa.Boolean(), nullable=False),
        sa.Column("instrument_metadata", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["user_accounts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id",
            "provider",
            "instrument_type",
            name="uq_payment_instrument_provider_type",
        ),
    )
    op.create_index("ix_payment_instruments_user_id", "payment_instruments", ["user_id"])
    op.create_index("ix_payment_instruments_provider", "payment_instruments", ["provider"])
    op.create_index("ix_payment_instruments_status", "payment_instruments", ["status"])
    op.create_index("ix_payment_instruments_is_default", "payment_instruments", ["is_default"])

    op.add_column(
        "ap2_consent_challenges", sa.Column("payment_instrument_id", sa.Uuid(), nullable=True)
    )
    op.add_column(
        "ap2_consent_challenges",
        sa.Column("webauthn_challenge", sa.String(length=256), nullable=True),
    )
    op.create_foreign_key(
        "fk_ap2_challenge_payment_instrument",
        "ap2_consent_challenges",
        "payment_instruments",
        ["payment_instrument_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_ap2_consent_challenges_payment_instrument_id",
        "ap2_consent_challenges",
        ["payment_instrument_id"],
    )
    op.create_unique_constraint(
        "uq_ap2_consent_challenges_webauthn_challenge",
        "ap2_consent_challenges",
        ["webauthn_challenge"],
    )

    op.create_table(
        "payment_credential_grants",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("checkout_id", sa.Uuid(), nullable=False),
        sa.Column("payment_mandate_id", sa.Uuid(), nullable=False),
        sa.Column("payment_instrument_id", sa.Uuid(), nullable=False),
        sa.Column("token_sha256", sa.String(length=64), nullable=False),
        sa.Column("signed_credential", sa.Text(), nullable=False),
        sa.Column("credential_kind", sa.String(length=80), nullable=False),
        sa.Column("checkout_hash", sa.String(length=64), nullable=False),
        sa.Column("amount_minor", sa.Integer(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("presented_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("payment_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["checkout_id"], ["checkouts.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["payment_id"], ["payments.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["payment_instrument_id"], ["payment_instruments.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["payment_mandate_id"], ["ap2_mandates.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["user_id"], ["user_accounts.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("checkout_id"),
        sa.UniqueConstraint("payment_id"),
        sa.UniqueConstraint("payment_mandate_id"),
        sa.UniqueConstraint("token_sha256"),
    )
    for column in (
        "user_id",
        "checkout_id",
        "payment_instrument_id",
        "checkout_hash",
        "status",
        "expires_at",
    ):
        op.create_index(
            f"ix_payment_credential_grants_{column}", "payment_credential_grants", [column]
        )


def downgrade() -> None:
    op.drop_table("payment_credential_grants")
    op.drop_constraint(
        "uq_ap2_consent_challenges_webauthn_challenge",
        "ap2_consent_challenges",
        type_="unique",
    )
    op.drop_index(
        "ix_ap2_consent_challenges_payment_instrument_id",
        table_name="ap2_consent_challenges",
    )
    op.drop_constraint(
        "fk_ap2_challenge_payment_instrument",
        "ap2_consent_challenges",
        type_="foreignkey",
    )
    op.drop_column("ap2_consent_challenges", "webauthn_challenge")
    op.drop_column("ap2_consent_challenges", "payment_instrument_id")
    op.drop_table("payment_instruments")
    op.drop_table("webauthn_ceremonies")
    op.drop_table("webauthn_credentials")
