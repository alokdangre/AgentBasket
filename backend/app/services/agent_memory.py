from __future__ import annotations

import hashlib
import hmac
import json
import re
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import ConflictError, DomainError, NotFoundError
from app.db.models import (
    AgentMemoryFact,
    AgentMemorySetting,
    AuditEvent,
    Merchant,
    UserAccount,
)
from app.schemas.agent import (
    AgentMemoryFactOut,
    AgentMemoryListOut,
    AgentMemoryMutationOut,
)


def utc_now() -> datetime:
    return datetime.now(UTC)


def canonical_utc(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat(timespec="microseconds")


ALLOWED_MEMORY_KINDS = frozenset(
    {
        "brew_method",
        "milk_preference",
        "caffeine_preference",
        "flavor_preference",
        "disliked_flavor",
        "dietary_preference",
        "budget_minor",
    }
)

_KIND_ALIASES = {
    "brew method": "brew_method",
    "milk": "milk_preference",
    "milk preference": "milk_preference",
    "caffeine": "caffeine_preference",
    "caffeine preference": "caffeine_preference",
    "flavor": "flavor_preference",
    "flavour": "flavor_preference",
    "flavor preference": "flavor_preference",
    "flavour preference": "flavor_preference",
    "disliked flavor": "disliked_flavor",
    "disliked flavour": "disliked_flavor",
    "dietary preference": "dietary_preference",
    "diet": "dietary_preference",
    "budget": "budget_minor",
}
_REMEMBER_PATTERN = re.compile(
    r"^remember(?:\s+that)?\s+my\s+(?P<kind>[a-z ]{2,32})\s+is\s+(?P<value>.+?)[.!]?$",
    re.I,
)
_FORGET_PATTERN = re.compile(
    r"^forget(?:\s+that)?\s+my\s+(?P<kind>[a-z ]{2,32})[.!]?$",
    re.I,
)
_UNSAFE_VALUE_PATTERNS = (
    re.compile(r"\bignore\b.{0,30}\b(?:instruction|prompt|policy|rule)s?\b", re.I),
    re.compile(r"\b(?:system|developer|assistant)\s*(?:message|prompt|role)\b", re.I),
    re.compile(r"\b(?:password|api[_ -]?key|secret|bearer|access[_ -]?token)\b", re.I),
    re.compile(r"\b(?:call|invoke|execute|use)\b.{0,30}\b(?:tool|function|command)\b", re.I),
    re.compile(r"\b\d{12,19}\b"),
    re.compile(r"https?://", re.I),
)


class AgentMemoryService:
    """Validated, customer-and-merchant-scoped preference memory."""

    def __init__(self, db: Session) -> None:
        self.db = db
        self.settings = get_settings()
        self._integrity_key()

    def list(self, customer: UserAccount, merchant_slug: str) -> AgentMemoryListOut:
        with self.db.begin():
            merchant = self._merchant(merchant_slug)
            enabled = self._enabled(customer.id, merchant.id)
            facts = list(
                self.db.scalars(
                    select(AgentMemoryFact)
                    .where(
                        AgentMemoryFact.customer_id == customer.id,
                        AgentMemoryFact.merchant_id == merchant.id,
                        AgentMemoryFact.revoked_at.is_(None),
                        AgentMemoryFact.expires_at > utc_now(),
                    )
                    .order_by(AgentMemoryFact.kind, AgentMemoryFact.updated_at.desc())
                )
            )
            valid: list[AgentMemoryFact] = []
            for fact in facts:
                if hmac.compare_digest(fact.content_sha256, self._digest(fact)):
                    valid.append(fact)
                    continue
                fact.revoked_at = utc_now()
                self.db.add(
                    AuditEvent(
                        merchant_id=merchant.id,
                        actor_type="system",
                        actor_id=None,
                        event_type="agent.memory.integrity_rejected",
                        aggregate_type="agent_memory",
                        aggregate_id=str(fact.id),
                        payload={"kind": fact.kind, "reason_code": "integrity_mismatch"},
                    )
                )
            return AgentMemoryListOut(
                merchant_slug=merchant.slug,
                enabled=enabled,
                memories=[self._fact_out(fact) for fact in valid] if enabled else [],
            )

    def upsert(
        self,
        customer: UserAccount,
        merchant_slug: str,
        kind: str,
        value: str | int,
        *,
        expires_in_days: int | None = None,
        source_message_id: uuid.UUID | None = None,
        source: str = "memory_api",
    ) -> AgentMemoryMutationOut:
        normalized_kind = self._kind(kind)
        normalized_value = self._value(normalized_kind, value)
        with self.db.begin():
            merchant = self._merchant(merchant_slug)
            if not self._enabled(customer.id, merchant.id):
                raise ConflictError(
                    "agent_memory_disabled",
                    "Preference memory is disabled for this merchant.",
                )
            fact = self.db.scalar(
                select(AgentMemoryFact).where(
                    AgentMemoryFact.customer_id == customer.id,
                    AgentMemoryFact.merchant_id == merchant.id,
                    AgentMemoryFact.kind == normalized_kind,
                    AgentMemoryFact.normalized_key == normalized_kind,
                )
            )
            if fact is None:
                fact_count = self.db.scalar(
                    select(func.count())
                    .select_from(AgentMemoryFact)
                    .where(
                        AgentMemoryFact.customer_id == customer.id,
                        AgentMemoryFact.merchant_id == merchant.id,
                        AgentMemoryFact.revoked_at.is_(None),
                        AgentMemoryFact.expires_at > utc_now(),
                    )
                )
                if int(fact_count or 0) >= self.settings.agent_memory_max_facts:
                    raise DomainError(
                        "agent_memory_limit",
                        "Delete an existing preference before adding another.",
                        409,
                    )
                fact = AgentMemoryFact(
                    customer_id=customer.id,
                    merchant_id=merchant.id,
                    kind=normalized_kind,
                    normalized_key=normalized_kind,
                    value={"value": normalized_value, "source": source},
                    source_message_id=source_message_id,
                    confidence=Decimal("1.000"),
                    sensitivity="standard",
                    content_sha256="pending",
                    expires_at=utc_now()
                    + timedelta(
                        days=expires_in_days or self.settings.agent_memory_default_ttl_days
                    ),
                )
                self.db.add(fact)
            else:
                fact.value = {"value": normalized_value, "source": source}
                fact.source_message_id = source_message_id
                fact.confidence = Decimal("1.000")
                fact.expires_at = utc_now() + timedelta(
                    days=expires_in_days or self.settings.agent_memory_default_ttl_days
                )
                fact.revoked_at = None
            self.db.flush()
            fact.content_sha256 = self._digest(fact)
            self.db.add(
                AuditEvent(
                    merchant_id=merchant.id,
                    actor_type="customer",
                    actor_id=str(customer.id),
                    event_type="agent.memory.upserted",
                    aggregate_type="agent_memory",
                    aggregate_id=str(fact.id),
                    payload={
                        "kind": normalized_kind,
                        "content_sha256": fact.content_sha256,
                        "source": source,
                        "expires_at": fact.expires_at.isoformat(),
                    },
                )
            )
            self.db.flush()
            return AgentMemoryMutationOut(
                merchant_slug=merchant.slug,
                enabled=True,
                action="upserted",
                memory=self._fact_out(fact),
            )

    def revoke(
        self, customer: UserAccount, merchant_slug: str, kind: str
    ) -> AgentMemoryMutationOut:
        normalized_kind = self._kind(kind)
        with self.db.begin():
            merchant = self._merchant(merchant_slug)
            fact = self.db.scalar(
                select(AgentMemoryFact).where(
                    AgentMemoryFact.customer_id == customer.id,
                    AgentMemoryFact.merchant_id == merchant.id,
                    AgentMemoryFact.kind == normalized_kind,
                    AgentMemoryFact.normalized_key == normalized_kind,
                    AgentMemoryFact.revoked_at.is_(None),
                )
            )
            if fact is None:
                raise NotFoundError("agent_memory_not_found", "That preference was not found.")
            fact.revoked_at = utc_now()
            fact.content_sha256 = self._digest(fact)
            self.db.add(
                AuditEvent(
                    merchant_id=merchant.id,
                    actor_type="customer",
                    actor_id=str(customer.id),
                    event_type="agent.memory.revoked",
                    aggregate_type="agent_memory",
                    aggregate_id=str(fact.id),
                    payload={"kind": normalized_kind},
                )
            )
            return AgentMemoryMutationOut(
                merchant_slug=merchant.slug,
                enabled=self._enabled(customer.id, merchant.id),
                action="revoked",
            )

    def set_enabled(
        self, customer: UserAccount, merchant_slug: str, enabled: bool
    ) -> AgentMemoryMutationOut:
        with self.db.begin():
            merchant = self._merchant(merchant_slug)
            setting = self.db.scalar(
                select(AgentMemorySetting).where(
                    AgentMemorySetting.customer_id == customer.id,
                    AgentMemorySetting.merchant_id == merchant.id,
                )
            )
            if setting is None:
                setting = AgentMemorySetting(
                    customer_id=customer.id,
                    merchant_id=merchant.id,
                    enabled=enabled,
                )
                self.db.add(setting)
                self.db.flush()
            else:
                setting.enabled = enabled
            self.db.add(
                AuditEvent(
                    merchant_id=merchant.id,
                    actor_type="customer",
                    actor_id=str(customer.id),
                    event_type="agent.memory.setting_changed",
                    aggregate_type="agent_memory_setting",
                    aggregate_id=str(setting.id),
                    payload={"enabled": enabled},
                )
            )
            return AgentMemoryMutationOut(
                merchant_slug=merchant.slug,
                enabled=enabled,
                action="enabled" if enabled else "disabled",
            )

    def preferences_for_prompt(
        self, customer: UserAccount, merchant_slug: str
    ) -> list[dict[str, str | int]]:
        listing = self.list(customer, merchant_slug)
        return [
            {"kind": fact.kind, "value": fact.value}
            for fact in listing.memories[: self.settings.agent_memory_max_facts]
        ]

    def apply_explicit_message(
        self,
        customer: UserAccount,
        merchant_slug: str,
        message: str,
        source_message_id: uuid.UUID,
    ) -> dict[str, Any] | None:
        normalized = " ".join(message.strip().split())
        remember = _REMEMBER_PATTERN.fullmatch(normalized)
        if remember:
            alias = remember.group("kind").strip().lower()
            kind = _KIND_ALIASES.get(alias)
            if kind is None:
                return {
                    "status": "rejected",
                    "reason": "unsupported_preference_kind",
                    "allowed_kinds": sorted(ALLOWED_MEMORY_KINDS),
                }
            value: str | int = remember.group("value").strip()
            if kind == "budget_minor":
                digits = re.sub(r"[^0-9]", "", str(value))
                if not digits:
                    return {"status": "rejected", "reason": "invalid_budget"}
                value = int(digits) * 100
            try:
                result = self.upsert(
                    customer,
                    merchant_slug,
                    kind,
                    value,
                    source_message_id=source_message_id,
                    source="explicit_user_message",
                )
            except DomainError as error:
                return {"status": "rejected", "reason": error.code}
            return {
                "status": "saved",
                "kind": kind,
                "value": result.memory.value if result.memory else value,
            }

        forget = _FORGET_PATTERN.fullmatch(normalized)
        if forget:
            alias = forget.group("kind").strip().lower()
            kind = _KIND_ALIASES.get(alias)
            if kind is None:
                return {"status": "rejected", "reason": "unsupported_preference_kind"}
            try:
                self.revoke(customer, merchant_slug, kind)
            except NotFoundError:
                return {"status": "not_found", "kind": kind}
            return {"status": "forgotten", "kind": kind}
        return None

    def _enabled(self, customer_id: uuid.UUID, merchant_id: uuid.UUID) -> bool:
        setting = self.db.scalar(
            select(AgentMemorySetting).where(
                AgentMemorySetting.customer_id == customer_id,
                AgentMemorySetting.merchant_id == merchant_id,
            )
        )
        return setting is None or setting.enabled

    def _merchant(self, slug: str) -> Merchant:
        merchant = self.db.scalar(select(Merchant).where(Merchant.slug == slug))
        if merchant is None:
            raise NotFoundError("merchant_not_found", "Merchant was not found.")
        return merchant

    @staticmethod
    def _kind(kind: str) -> str:
        normalized = kind.strip().lower().replace("-", "_")
        if normalized not in ALLOWED_MEMORY_KINDS:
            raise DomainError(
                "unsupported_memory_kind",
                f"Preference kind must be one of: {', '.join(sorted(ALLOWED_MEMORY_KINDS))}.",
                422,
            )
        return normalized

    @staticmethod
    def _value(kind: str, value: str | int) -> str | int:
        if kind == "budget_minor":
            if isinstance(value, bool) or not isinstance(value, int):
                raise DomainError(
                    "invalid_memory_value",
                    "budget_minor must be an integer in the smallest currency unit.",
                    422,
                )
            if value < 0 or value > 10_000_000:
                raise DomainError(
                    "invalid_memory_value", "The saved budget is outside the allowed range.", 422
                )
            return value
        if not isinstance(value, str):
            raise DomainError(
                "invalid_memory_value", "This preference must be a short text value.", 422
            )
        normalized = " ".join(value.strip().split())
        if not normalized or len(normalized) > 120:
            raise DomainError(
                "invalid_memory_value", "Preference text must be between 1 and 120 characters.", 422
            )
        if any(pattern.search(normalized) for pattern in _UNSAFE_VALUE_PATTERNS):
            raise DomainError(
                "unsafe_memory_value",
                "That value looks like instructions or sensitive data and was not stored.",
                422,
            )
        return normalized

    def _digest(self, fact: AgentMemoryFact) -> str:
        key = self._integrity_key()
        canonical = json.dumps(
            {
                "id": str(fact.id),
                "customer_id": str(fact.customer_id),
                "merchant_id": str(fact.merchant_id),
                "kind": fact.kind,
                "normalized_key": fact.normalized_key,
                "value": fact.value,
                "source_message_id": (
                    str(fact.source_message_id) if fact.source_message_id is not None else None
                ),
                "confidence": str(fact.confidence),
                "sensitivity": fact.sensitivity,
                "expires_at": canonical_utc(fact.expires_at),
                "revoked_at": canonical_utc(fact.revoked_at) if fact.revoked_at else None,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        return hmac.new(key, canonical, hashlib.sha256).hexdigest()

    def _integrity_key(self) -> bytes:
        configured = self.settings.agent_memory_integrity_key
        if configured is not None and configured.get_secret_value():
            return configured.get_secret_value().encode()
        if self.settings.app_env.lower() == "production":
            raise DomainError(
                "agent_memory_not_configured",
                "Preference memory is unavailable until its integrity key is configured.",
                503,
            )
        return b"agentbasket-development-memory-integrity-key"

    @staticmethod
    def _fact_out(fact: AgentMemoryFact) -> AgentMemoryFactOut:
        return AgentMemoryFactOut(
            id=fact.id,
            kind=fact.kind,
            normalized_key=fact.normalized_key,
            value=fact.value["value"],
            sensitivity=fact.sensitivity,
            source=str(fact.value.get("source", "unknown")),
            expires_at=fact.expires_at,
            created_at=fact.created_at,
            updated_at=fact.updated_at,
        )
