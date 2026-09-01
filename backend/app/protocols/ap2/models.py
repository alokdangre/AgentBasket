"""Typed AP2 v0.2 subset pinned to specification commit e1ea56db72a6385bce3e5c1112b3a56ce60acb43."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

# These payloads are the generated models from the official AP2 SDK pinned in
# pyproject.toml. Keeping response wrappers local does not redefine mandates.
from ap2.sdk.generated.checkout_mandate import CheckoutMandate
from ap2.sdk.generated.payment_mandate import PaymentMandate
from pydantic import BaseModel, ConfigDict, Field

from app.schemas.checkout import CheckoutApprovalOut


class AP2Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CheckoutReceiptSuccess(AP2Model):
    status: Literal["Success"] = "Success"
    iss: str
    iat: int
    reference: str
    error: None = None
    error_description: None = None
    order_id: str


class PaymentReceiptSuccess(AP2Model):
    status: Literal["Success"] = "Success"
    iss: str
    iat: int
    reference: str
    error: None = None
    error_description: None = None
    payment_id: str
    psp_confirmation_id: str
    network_confirmation_id: str


class AP2ChallengeOut(BaseModel):
    id: uuid.UUID
    checkout_id: uuid.UUID
    nonce: str
    checkout_hash: str
    display_sha256: str
    expires_at: datetime
    display: dict[str, Any]
    checkout_mandate: CheckoutMandate
    payment_mandate: PaymentMandate
    webauthn_options: dict[str, Any]


class AP2ChallengeCreate(BaseModel):
    payment_instrument_id: uuid.UUID


class AP2ApprovalCreate(BaseModel):
    challenge_id: uuid.UUID
    nonce: str = Field(min_length=20, max_length=128)
    checkout_hash: str = Field(min_length=20, max_length=128)
    display_sha256: str = Field(min_length=64, max_length=64)
    expected_total_minor: int = Field(ge=0)
    currency: str = Field(min_length=3, max_length=3)
    quote_version: int = Field(ge=1)
    webauthn_credential: dict[str, Any]


class PaymentCredentialGrantOut(BaseModel):
    id: uuid.UUID
    credential_kind: str
    instrument_alias: str
    status: str
    expires_at: datetime


class AP2MandateOut(BaseModel):
    id: uuid.UUID
    mandate_type: str
    vct: str
    issuer: str
    key_id: str
    checkout_hash: str
    verification_status: str
    signed_jwt: str


class AP2ApprovalOut(BaseModel):
    challenge_id: uuid.UUID
    checkout_hash: str
    approval: CheckoutApprovalOut
    mandates: list[AP2MandateOut]
    credential_grant: PaymentCredentialGrantOut


class AP2ReceiptOut(BaseModel):
    id: uuid.UUID
    order_id: uuid.UUID
    receipt_type: str
    status: str
    issuer: str
    key_id: str
    reference: str
    signed_jwt: str
    created_at: datetime


class AP2EvidenceOut(BaseModel):
    checkout_id: uuid.UUID
    challenge_id: uuid.UUID
    checkout_hash: str
    status: str
    mandates: list[AP2MandateOut]
    receipts: list[AP2ReceiptOut]
