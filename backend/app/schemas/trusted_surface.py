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


class PaymentInstrumentListOut(BaseModel):
    payment_instruments: list[PaymentInstrumentOut]
