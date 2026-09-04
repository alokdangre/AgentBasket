from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from app.domain.enums import FulfillmentType, PurchaseIntentStatus, ScheduledRunStatus

ScheduleFrequency = Literal["once", "daily", "weekly", "monthly"]


class ScheduledPurchaseItemCreate(BaseModel):
    acceptable_variant_ids: list[uuid.UUID] = Field(min_length=1, max_length=5)
    quantity: int = Field(ge=1, le=20)
    modifier_option_ids: list[uuid.UUID] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def identifiers_are_unique(self) -> ScheduledPurchaseItemCreate:
        if len(self.acceptable_variant_ids) != len(set(self.acceptable_variant_ids)):
            raise ValueError("acceptable_variant_ids must be unique")
        if len(self.modifier_option_ids) != len(set(self.modifier_option_ids)):
            raise ValueError("modifier_option_ids must be unique")
        return self


class ScheduledPurchaseDraftCreate(BaseModel):
    merchant_slug: str = Field(default="ember-and-leaf", min_length=1, max_length=80)
    fulfillment_type: FulfillmentType = FulfillmentType.LOCAL_DELIVERY
    address_id: uuid.UUID | None = None
    location_id: uuid.UUID | None = None
    payment_instrument_id: uuid.UUID
    items: list[ScheduledPurchaseItemCreate] = Field(min_length=1, max_length=10)
    frequency: ScheduleFrequency
    interval_count: int = Field(default=1, ge=1, le=12)
    timezone: str = Field(default="Asia/Kolkata", min_length=1, max_length=64)
    first_run_at: datetime
    expires_at: datetime
    max_occurrences: int = Field(ge=1, le=365)
    max_amount_minor: int = Field(ge=100, le=10_000_000)
    max_total_minor: int = Field(ge=100, le=100_000_000)
    currency: str = Field(default="INR", min_length=3, max_length=3)

    @model_validator(mode="after")
    def validate_bounds(self) -> ScheduledPurchaseDraftCreate:
        if self.fulfillment_type == FulfillmentType.PICKUP:
            if self.location_id is None or self.address_id is not None:
                raise ValueError("pickup requires location_id and no address_id")
        elif self.address_id is None or self.location_id is not None:
            raise ValueError("delivery requires address_id and no location_id")
        if self.frequency == "once" and self.max_occurrences != 1:
            raise ValueError("a once schedule must have max_occurrences=1")
        if self.max_total_minor < self.max_amount_minor:
            raise ValueError("max_total_minor must be at least max_amount_minor")
        if self.expires_at <= self.first_run_at:
            raise ValueError("expires_at must be after first_run_at")
        if self.first_run_at.tzinfo is None or self.expires_at.tzinfo is None:
            raise ValueError("schedule timestamps must include a timezone offset")
        return self


class ScheduledPurchaseAuthorizationCreate(BaseModel):
    nonce: str = Field(min_length=20, max_length=128)
    display_sha256: str = Field(min_length=64, max_length=64)
    webauthn_credential: dict[str, Any]


class ScheduledPurchaseChallengeOut(BaseModel):
    intent_id: uuid.UUID
    nonce: str
    display_sha256: str
    display: dict[str, Any]
    expires_at: datetime
    agent_public_jwk: dict[str, Any]
    webauthn_options: dict[str, Any]


class ScheduledPurchaseRunOut(BaseModel):
    id: uuid.UUID
    scheduled_for: datetime
    status: ScheduledRunStatus
    attempt_count: int
    checkout_id: uuid.UUID | None
    order_id: uuid.UUID | None
    payment_id: uuid.UUID | None
    amount_minor: int
    currency: str
    provider_payment_after: datetime | None
    provider_order_id: str | None
    provider_payment_id: str | None
    failure_code: str | None
    failure_message: str | None
    evidence: dict[str, Any]
    created_at: datetime
    updated_at: datetime


class ScheduledPurchaseOut(BaseModel):
    id: uuid.UUID
    merchant_slug: str
    status: PurchaseIntentStatus
    fulfillment_type: FulfillmentType
    address_id: uuid.UUID | None
    location_id: uuid.UUID | None
    payment_instrument_id: uuid.UUID
    payment_instrument_alias: str
    constraints: dict[str, Any]
    frequency: ScheduleFrequency
    interval_count: int
    timezone: str
    next_run_at: datetime | None
    next_execution_at: datetime | None
    expires_at: datetime
    max_occurrences: int
    successful_occurrences: int
    max_amount_minor: int
    max_total_minor: int
    spent_minor: int
    currency: str
    display: dict[str, Any] | None
    display_sha256: str | None
    open_checkout_hash: str | None
    authorization_reference: str | None
    provider_ready: bool
    authorized_at: datetime | None
    provider_authorized_at: datetime | None
    paused_at: datetime | None
    revoked_at: datetime | None
    last_failure_code: str | None
    last_failure_message: str | None
    created_at: datetime
    updated_at: datetime
    runs: list[ScheduledPurchaseRunOut] = Field(default_factory=list)


class ScheduledPurchaseListOut(BaseModel):
    scheduled_purchases: list[ScheduledPurchaseOut]


class ScheduledPurchaseAuthorizationOut(BaseModel):
    scheduled_purchase: ScheduledPurchaseOut
    open_checkout_mandate: str
    open_payment_mandate: str


class ScheduledPurchaseActionOut(BaseModel):
    id: uuid.UUID
    status: PurchaseIntentStatus
    next_run_at: datetime | None


class ScheduledWorkerResultOut(BaseModel):
    claimed: int
    processed: int
    succeeded: int
    requires_human_action: int
    failed: int
