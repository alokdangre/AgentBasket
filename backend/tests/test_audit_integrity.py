import uuid
from typing import Any

import httpx
import pytest
from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from app.agents.policy import POLICY_VERSION
from app.agents.runtime import AgentRuntimeResult, get_agent_runtime
from app.agents.tools import AgentToolbox
from app.db.models import AgentRun, AuditEvent
from app.main import app
from app.services.audit import AuditIntegrityService


class AuditedRuntime:
    model_name = "audit-test-model"

    async def run(self, *, toolbox: AgentToolbox, **_: Any) -> AgentRuntimeResult:
        toolbox.search_catalog("tea")
        return AgentRuntimeResult(text="Audited result.", model=self.model_name)


def headers(seeded: dict[str, object], key: str | None = None) -> dict[str, str]:
    result = {"Authorization": f"Bearer {seeded['customer_token']}"}
    if key:
        result["Idempotency-Key"] = key
    return result


async def create_audited_run(
    client: httpx.AsyncClient, seeded: dict[str, object]
) -> tuple[str, str]:
    runtime = AuditedRuntime()
    app.dependency_overrides[get_agent_runtime] = lambda: runtime
    current = await client.post(
        "/api/v1/agent/conversations/current",
        headers=headers(seeded),
        json={"merchant_slug": "ember-and-leaf"},
    )
    conversation_id = current.json()["id"]
    response = await client.post(
        f"/api/v1/agent/conversations/{conversation_id}/messages",
        headers=headers(seeded, "audit-chain-message-1"),
        json={"content": "Show me tea."},
    )
    assert response.status_code == 200
    return conversation_id, response.json()["run_id"]


@pytest.mark.anyio
async def test_agent_audit_stream_is_versioned_correlated_and_hash_chained(
    client: httpx.AsyncClient,
    seeded: dict[str, object],
    session_factory: sessionmaker[Session],
) -> None:
    conversation_id, run_id = await create_audited_run(client, seeded)
    with session_factory() as db:
        events = list(
            db.scalars(
                select(AuditEvent)
                .where(
                    AuditEvent.aggregate_type == "agent_conversation",
                    AuditEvent.aggregate_id == conversation_id,
                )
                .order_by(AuditEvent.sequence)
            )
        )
        assert len(events) >= 4
        assert [item.sequence for item in events] == list(range(1, len(events) + 1))
        assert events[0].previous_hash is None
        assert all(item.schema_version == "1" for item in events)
        assert all(item.current_hash != "legacy" for item in events)
        assert any(item.correlation_id == run_id for item in events)
        verification = AuditIntegrityService(db).verify_stream(
            seeded["merchant_id"], "agent_conversation", conversation_id
        )
        assert verification.valid is True
        assert verification.event_count == len(events)


@pytest.mark.anyio
async def test_orm_cannot_update_or_delete_authoritative_audit_event(
    client: httpx.AsyncClient,
    seeded: dict[str, object],
    session_factory: sessionmaker[Session],
) -> None:
    await create_audited_run(client, seeded)
    with session_factory() as db:
        item = db.scalar(select(AuditEvent).limit(1))
        assert item is not None
        item.payload = {"tampered": True}
        with pytest.raises(ValueError, match="append-only"):
            db.commit()
        db.rollback()

        item = db.scalar(select(AuditEvent).limit(1))
        assert item is not None
        db.delete(item)
        with pytest.raises(ValueError, match="append-only"):
            db.commit()


@pytest.mark.anyio
async def test_hash_verifier_detects_out_of_band_tampering(
    client: httpx.AsyncClient,
    seeded: dict[str, object],
    session_factory: sessionmaker[Session],
) -> None:
    conversation_id, _ = await create_audited_run(client, seeded)
    with session_factory() as db, db.begin():
        first_id = db.scalar(
            select(AuditEvent.id)
            .where(
                AuditEvent.aggregate_type == "agent_conversation",
                AuditEvent.aggregate_id == conversation_id,
            )
            .order_by(AuditEvent.sequence)
            .limit(1)
        )
        assert first_id is not None
        db.execute(
            update(AuditEvent)
            .where(AuditEvent.id == first_id)
            .values(payload={"out_of_band": "tamper"})
        )

    with session_factory() as db:
        verification = AuditIntegrityService(db).verify_stream(
            seeded["merchant_id"], "agent_conversation", conversation_id
        )
        assert verification.valid is False
        assert verification.first_invalid_event_id == first_id
        assert verification.reason == "event_hash_mismatch"


@pytest.mark.anyio
async def test_agent_run_records_policy_graph_and_checkpoint_versions(
    client: httpx.AsyncClient,
    seeded: dict[str, object],
    session_factory: sessionmaker[Session],
) -> None:
    _, run_id = await create_audited_run(client, seeded)
    with session_factory() as db:
        run = db.get(AgentRun, uuid.UUID(run_id))
        assert run is not None
        assert run.graph_version == "ask-ember-graph-2"
        assert run.policy_version == POLICY_VERSION
        assert run.intent == "discover"
        assert run.risk_level == "low"
        assert run.checkpoint_state["phase"] == "completed"
