import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.domain.enums import CheckoutStatus, OrderStatus, PaymentStatus


class RazorpaySessionOut(BaseModel):
    checkout_id: uuid.UUID
    order_id: uuid.UUID
    public_number: str
    key_id: str
    provider_order_id: str
    amount_minor: int
    currency: str
    merchant_name: str
    description: str
    customer_name: str
    customer_email: str
    customer_phone: str | None


class RazorpayVerifyRequest(BaseModel):
    checkout_id: uuid.UUID
    razorpay_order_id: str = Field(min_length=1, max_length=120, pattern=r"^order_[A-Za-z0-9_]+$")
    razorpay_payment_id: str = Field(min_length=1, max_length=120, pattern=r"^pay_[A-Za-z0-9_]+$")
    razorpay_signature: str = Field(min_length=1, max_length=256)


class PaymentReceiptOut(BaseModel):
    status: PaymentStatus
    provider: str
    provider_order_id: str | None
    provider_payment_id: str | None
    amount_minor: int
    currency: str
    captured_at: datetime | None


class OrderTimelineEventOut(BaseModel):
    event_type: str
    actor_type: str
    occurred_at: datetime
    payload: dict[str, Any]


class OrderReceiptOut(BaseModel):
    id: uuid.UUID
    public_number: str
    checkout_id: uuid.UUID
    checkout_status: CheckoutStatus
    status: OrderStatus
    total_minor: int
    currency: str
    fulfillment: dict[str, Any]
    created_at: datetime
    payment: PaymentReceiptOut
    timeline: list[OrderTimelineEventOut]


class OrderSummaryOut(BaseModel):
    id: uuid.UUID
    public_number: str
    status: OrderStatus
    total_minor: int
    currency: str
    created_at: datetime


class WebhookResultOut(BaseModel):
    status: str
    event_id: str
    event_type: str
