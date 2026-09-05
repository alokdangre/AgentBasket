from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import event, select
from sqlalchemy.orm import Session

from app.db.models import AuditEvent


def _utc_iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def audit_event_digest(
    item: AuditEvent,
    *,
    sequence: int | None = None,
    previous_hash: str | None = None,
) -> str:
    canonical = json.dumps(
        {
            "schema_version": item.schema_version,
            "event_id": str(item.id),
            "occurred_at": _utc_iso(item.occurred_at),
            "event_type": item.event_type,
            "severity": item.severity,
            "source_component": item.source_component,
            "correlation_id": item.correlation_id,
            "merchant_id": str(item.merchant_id),
            "actor_type": item.actor_type,
            "actor_id": item.actor_id,
            "aggregate_type": item.aggregate_type,
            "aggregate_id": item.aggregate_id,
            "payload": item.payload or {},
            "sequence": sequence if sequence is not None else item.sequence,
            "previous_hash": previous_hash if previous_hash is not None else item.previous_hash,
            "hash_algorithm": item.hash_algorithm,
        },
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode()
    return hashlib.sha256(canonical).hexdigest()


def _source_component(event_type: str) -> str:
    prefix = event_type.split(".", 1)[0]
    return {
        "agent": "agent-runtime",
        "ap2": "ap2-verifier",
        "payment": "payment-module",
        "checkout": "checkout-module",
        "order": "order-module",
        "ucp": "ucp-adapter",
        "schedule": "scheduled-purchase-module",
        "scheduled": "scheduled-purchase-module",
        "inventory": "inventory-module",
    }.get(prefix, "commerce-core")


def _severity(event_type: str) -> str:
    lowered = event_type.lower()
    if any(part in lowered for part in ("tamper", "integrity_rejected", "signature_rejected")):
        return "critical"
    if any(part in lowered for part in ("denied", "rejected", "failed", "expired", "revoked")):
        return "warning"
    return "info"


def _correlation_id(item: AuditEvent) -> str:
    payload: dict[str, Any] = item.payload or {}
    for key in (
        "correlation_id",
        "run_id",
        "checkout_id",
        "payment_id",
        "order_id",
        "scheduled_purchase_id",
        "tool_call_id",
    ):
        value = payload.get(key)
        if value:
            return str(value)[:160]
    return f"{item.aggregate_type}:{item.aggregate_id}"[:160]


@event.listens_for(Session, "before_flush")
def prepare_append_only_audit_events(
    session: Session, flush_context: object, instances: object
) -> None:
    del flush_context, instances
    for item in session.deleted:
        if isinstance(item, AuditEvent):
            raise ValueError("Authoritative audit events are append-only and cannot be deleted.")
    for item in session.dirty:
        if isinstance(item, AuditEvent) and session.is_modified(item, include_collections=False):
            raise ValueError("Authoritative audit events are append-only and cannot be updated.")

    stream_heads: dict[tuple[uuid.UUID, str, str], tuple[int, str | None]] = {}
    for item in session.new:
        if not isinstance(item, AuditEvent):
            continue
        if item.id is None:
            item.id = uuid.uuid4()
        if item.occurred_at is None:
            item.occurred_at = datetime.now(UTC)
        item.schema_version = item.schema_version or "1"
        item.hash_algorithm = "SHA-256"
        item.source_component = item.source_component or _source_component(item.event_type)
        if item.source_component == "commerce-core":
            item.source_component = _source_component(item.event_type)
        item.severity = item.severity or _severity(item.event_type)
        if item.severity == "info":
            item.severity = _severity(item.event_type)
        item.correlation_id = item.correlation_id or _correlation_id(item)

        stream = (item.merchant_id, item.aggregate_type, item.aggregate_id)
        head = stream_heads.get(stream)
        if head is None:
            row = session.execute(
                select(AuditEvent.sequence, AuditEvent.current_hash)
                .where(
                    AuditEvent.merchant_id == item.merchant_id,
                    AuditEvent.aggregate_type == item.aggregate_type,
                    AuditEvent.aggregate_id == item.aggregate_id,
                    AuditEvent.sequence > 0,
                )
                .order_by(AuditEvent.sequence.desc())
                .limit(1)
            ).first()
            head = (int(row.sequence), str(row.current_hash)) if row else (0, None)
        item.sequence = head[0] + 1
        item.previous_hash = head[1]
        item.current_hash = audit_event_digest(
            item,
            sequence=item.sequence,
            previous_hash=item.previous_hash,
        )
        stream_heads[stream] = (item.sequence, item.current_hash)
