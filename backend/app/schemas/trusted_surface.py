import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class PasskeyRegistrationOptionsOut(BaseModel):
    ceremony_id: uuid.UUID
    public_key: dict[str, Any]
    expires_at: datetime


class PasskeyRegistrationVerify(BaseModel):
    ceremony_id: uuid.UUID
    credential: dict[str, Any]
    label: str = Field(default="Passkey", min_length=1, max_length=80)


class PasskeyOut(BaseModel):
    id: uuid.UUID
    label: str
    device_type: str
    backed_up: bool
    created_at: datetime
    last_used_at: datetime | None


class PasskeyListOut(BaseModel):
    passkeys: list[PasskeyOut]


class PaymentInstrumentOut(BaseModel):
    id: uuid.UUID
    provider: str
    instrument_type: str
    alias: str
    network: str | None
    last4: str | None
    is_default: bool
    requires_provider_checkout: bool
    recurring_ready: bool = False
    recurring_status: str | None = None


class PaymentInstrumentListOut(BaseModel):
    payment_instruments: list[PaymentInstrumentOut]


class RazorpayRecurringAuthorizationSessionOut(BaseModel):
    scheduled_purchase_id: uuid.UUID
    payment_instrument_id: uuid.UUID
    key_id: str
    provider_order_id: str
    provider_customer_id: str
    amount_minor: int
    max_amount_minor: int
    currency: str
    mandate_expires_at: datetime
    merchant_name: str
    description: str
    customer_name: str
    customer_email: str
    customer_phone: str
    recurring: bool = True


class RazorpayRecurringAuthorizationVerify(BaseModel):
    razorpay_order_id: str = Field(min_length=8, max_length=120)
    razorpay_payment_id: str = Field(min_length=8, max_length=120)
    razorpay_signature: str = Field(min_length=32, max_length=256)


class RazorpayRecurringAuthorizationOut(BaseModel):
    scheduled_purchase_id: uuid.UUID
    payment_instrument_id: uuid.UUID
    status: str
    provider_payment_id: str
    token_confirmation_pending: bool
