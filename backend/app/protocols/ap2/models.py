"""Typed AP2 v0.2 subset pinned to specification commit e1ea56db72a6385bce3e5c1112b3a56ce60acb43."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.checkout import CheckoutApprovalOut


class AP2Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Amount(AP2Model):
    amount: int = Field(ge=0)
    currency: str = Field(min_length=3, max_length=3)


class Merchant(AP2Model):
    id: str
    name: str
    website: str | None = None


class PaymentInstrument(AP2Model):
    id: str
    type: str
    description: str | None = None


class CheckoutMandate(AP2Model):
    vct: Literal["mandate.checkout.1"] = "mandate.checkout.1"
    checkout_jwt: str
    checkout_hash: str
    iat: int | None = None
    exp: int | None = None


class PaymentMandate(AP2Model):
    vct: Literal["mandate.payment.1"] = "mandate.payment.1"
    transaction_id: str
    payee: Merchant
    pisp: dict[str, Any] | None = None
    payment_amount: Amount
    payment_instrument: PaymentInstrument
    execution_date: str | None = None
    risk_data: dict[str, Any] | None = None
    iat: int | None = None
    exp: int | None = None


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


class AP2ApprovalCreate(BaseModel):
    challenge_id: uuid.UUID
    nonce: str = Field(min_length=20, max_length=128)
    checkout_hash: str = Field(min_length=20, max_length=128)
    display_sha256: str = Field(min_length=64, max_length=64)
    expected_total_minor: int = Field(ge=0)
    currency: str = Field(min_length=3, max_length=3)
    quote_version: int = Field(ge=1)


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
