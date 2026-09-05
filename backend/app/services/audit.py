from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.audit import audit_event_digest
from app.db.models import AuditEvent


@dataclass(frozen=True)
class AuditVerificationResult:
    valid: bool
    event_count: int
    first_invalid_event_id: uuid.UUID | None = None
    reason: str | None = None


class AuditIntegrityService:
    def __init__(self, db: Session) -> None:
        self.db = db

    def verify_stream(
        self,
        merchant_id: uuid.UUID,
        aggregate_type: str,
        aggregate_id: str,
    ) -> AuditVerificationResult:
        events = list(
            self.db.scalars(
                select(AuditEvent)
                .where(
                    AuditEvent.merchant_id == merchant_id,
                    AuditEvent.aggregate_type == aggregate_type,
                    AuditEvent.aggregate_id == aggregate_id,
                    AuditEvent.sequence > 0,
                )
                .order_by(AuditEvent.sequence)
            )
        )
        previous_hash: str | None = None
        for expected_sequence, item in enumerate(events, start=1):
            if item.sequence != expected_sequence:
                return AuditVerificationResult(
                    False,
                    len(events),
                    item.id,
                    "sequence_gap",
                )
            if item.previous_hash != previous_hash:
                return AuditVerificationResult(
                    False,
                    len(events),
                    item.id,
                    "previous_hash_mismatch",
                )
            if item.hash_algorithm != "SHA-256":
                return AuditVerificationResult(
                    False,
                    len(events),
                    item.id,
                    "unsupported_hash_algorithm",
                )
            if item.current_hash != audit_event_digest(item):
                return AuditVerificationResult(
                    False,
                    len(events),
                    item.id,
                    "event_hash_mismatch",
                )
            previous_hash = item.current_hash
        return AuditVerificationResult(True, len(events))
