"""secure agent graph, preference memory, and tamper-evident audit

Revision ID: e9a1b2c3d4e5
Revises: c7e8f9a0b1c2
Create Date: 2026-09-05 00:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "e9a1b2c3d4e5"
down_revision: str | None = "c7e8f9a0b1c2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "agent_runs",
        sa.Column(
            "graph_version",
            sa.String(length=80),
            server_default="ask-ember-graph-2",
            nullable=False,
        ),
    )
    op.add_column(
        "agent_runs",
        sa.Column(
            "policy_version",
            sa.String(length=80),
            server_default="agent-action-policy-1",
            nullable=False,
        ),
    )
    op.add_column(
        "agent_runs",
        sa.Column("intent", sa.String(length=40), server_default="answer", nullable=False),
    )
    op.add_column(
        "agent_runs",
        sa.Column("risk_level", sa.String(length=24), server_default="low", nullable=False),
    )
    op.add_column(
        "agent_runs",
        sa.Column("allowed_tools", sa.JSON(), server_default=sa.text("'[]'"), nullable=False),
    )
    op.add_column(
        "agent_runs",
        sa.Column("checkpoint_state", sa.JSON(), server_default=sa.text("'{}'"), nullable=False),
    )
    op.add_column(
        "agent_runs",
        sa.Column("tool_call_count", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column(
        "agent_runs",
        sa.Column("mutation_count", sa.Integer(), server_default="0", nullable=False),
    )
    op.create_index("ix_agent_runs_intent", "agent_runs", ["intent"])
    op.create_index("ix_agent_runs_risk_level", "agent_runs", ["risk_level"])

    op.create_table(
        "agent_memory_settings",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("customer_id", sa.Uuid(), nullable=False),
        sa.Column("merchant_id", sa.Uuid(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["customer_id"], ["user_accounts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["merchant_id"], ["merchants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "customer_id",
            "merchant_id",
            name="uq_agent_memory_setting_customer_merchant",
        ),
    )
    op.create_index(
        "ix_agent_memory_settings_customer_id", "agent_memory_settings", ["customer_id"]
    )
    op.create_index(
        "ix_agent_memory_settings_merchant_id", "agent_memory_settings", ["merchant_id"]
    )

    op.create_table(
        "agent_memory_facts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("customer_id", sa.Uuid(), nullable=False),
        sa.Column("merchant_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=48), nullable=False),
        sa.Column("normalized_key", sa.String(length=80), nullable=False),
        sa.Column("value", sa.JSON(), nullable=False),
        sa.Column("source_message_id", sa.Uuid(), nullable=True),
        sa.Column("confidence", sa.Numeric(precision=4, scale=3), nullable=False),
        sa.Column("sensitivity", sa.String(length=24), nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "confidence >= 0 AND confidence <= 1", name="ck_agent_memory_confidence_range"
        ),
        sa.ForeignKeyConstraint(["customer_id"], ["user_accounts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["merchant_id"], ["merchants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_message_id"], ["agent_messages.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "customer_id",
            "merchant_id",
            "kind",
            "normalized_key",
            name="uq_agent_memory_fact_scope_key",
        ),
    )
    op.create_index("ix_agent_memory_facts_customer_id", "agent_memory_facts", ["customer_id"])
    op.create_index("ix_agent_memory_facts_merchant_id", "agent_memory_facts", ["merchant_id"])
    op.create_index("ix_agent_memory_facts_kind", "agent_memory_facts", ["kind"])
    op.create_index(
        "ix_agent_memory_facts_source_message_id",
        "agent_memory_facts",
        ["source_message_id"],
    )
    op.create_index("ix_agent_memory_facts_expires_at", "agent_memory_facts", ["expires_at"])
    op.create_index("ix_agent_memory_facts_revoked_at", "agent_memory_facts", ["revoked_at"])

    op.add_column(
        "audit_events",
        sa.Column("schema_version", sa.String(length=16), server_default="1", nullable=False),
    )
    op.add_column(
        "audit_events",
        sa.Column("severity", sa.String(length=16), server_default="info", nullable=False),
    )
    op.add_column(
        "audit_events",
        sa.Column(
            "source_component",
            sa.String(length=80),
            server_default="commerce-core",
            nullable=False,
        ),
    )
    op.add_column(
        "audit_events",
        sa.Column("correlation_id", sa.String(length=160), server_default="legacy", nullable=False),
    )
    op.add_column(
        "audit_events",
        sa.Column("sequence", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column("audit_events", sa.Column("previous_hash", sa.String(length=64)))
    op.add_column(
        "audit_events",
        sa.Column("current_hash", sa.String(length=64), server_default="legacy", nullable=False),
    )
    op.add_column(
        "audit_events",
        sa.Column("hash_algorithm", sa.String(length=16), server_default="SHA-256", nullable=False),
    )
    op.create_index("ix_audit_events_severity", "audit_events", ["severity"])
    op.create_index("ix_audit_events_correlation_id", "audit_events", ["correlation_id"])
    op.create_index(
        "uq_audit_stream_sequence",
        "audit_events",
        ["merchant_id", "aggregate_type", "aggregate_id", "sequence"],
        unique=True,
        postgresql_where=sa.text("sequence > 0"),
        sqlite_where=sa.text("sequence > 0"),
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION agentbasket_reject_audit_mutation()
        RETURNS trigger AS $$
        BEGIN
          RAISE EXCEPTION 'authoritative audit events are append-only';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER audit_events_append_only
        BEFORE UPDATE OR DELETE ON audit_events
        FOR EACH ROW EXECUTE FUNCTION agentbasket_reject_audit_mutation()
        """
    )
    op.execute("REVOKE UPDATE, DELETE ON audit_events FROM PUBLIC")


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS audit_events_append_only ON audit_events")
    op.execute("DROP FUNCTION IF EXISTS agentbasket_reject_audit_mutation()")
    op.drop_index("uq_audit_stream_sequence", table_name="audit_events")
    op.drop_index("ix_audit_events_correlation_id", table_name="audit_events")
    op.drop_index("ix_audit_events_severity", table_name="audit_events")
    op.drop_column("audit_events", "hash_algorithm")
    op.drop_column("audit_events", "current_hash")
    op.drop_column("audit_events", "previous_hash")
    op.drop_column("audit_events", "sequence")
    op.drop_column("audit_events", "correlation_id")
    op.drop_column("audit_events", "source_component")
    op.drop_column("audit_events", "severity")
    op.drop_column("audit_events", "schema_version")

    op.drop_table("agent_memory_facts")
    op.drop_table("agent_memory_settings")
    op.drop_index("ix_agent_runs_risk_level", table_name="agent_runs")
    op.drop_index("ix_agent_runs_intent", table_name="agent_runs")
    op.drop_column("agent_runs", "mutation_count")
    op.drop_column("agent_runs", "tool_call_count")
    op.drop_column("agent_runs", "checkpoint_state")
    op.drop_column("agent_runs", "allowed_tools")
    op.drop_column("agent_runs", "risk_level")
    op.drop_column("agent_runs", "intent")
    op.drop_column("agent_runs", "policy_version")
    op.drop_column("agent_runs", "graph_version")
