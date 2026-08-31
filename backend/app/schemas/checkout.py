import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.domain.enums import CheckoutStatus, FulfillmentType


class CheckoutItemCreate(BaseModel):
    variant_id: uuid.UUID
    quantity: int = Field(ge=1, le=20)
    modifier_option_ids: list[uuid.UUID] = Field(default_factory=list)


class CheckoutCreate(BaseModel):
    merchant_slug: str
    fulfillment_type: FulfillmentType
    postal_code: str | None = None
    location_id: uuid.UUID | None = None
    delivery_address: dict[str, Any] | None = None
    items: list[CheckoutItemCreate] = Field(min_length=1, max_length=30)

    @model_validator(mode="after")
    def validate_fulfillment_input(self) -> "CheckoutCreate":
        if self.fulfillment_type == FulfillmentType.PICKUP and self.location_id is None:
            raise ValueError("location_id is required for pickup")
        if self.fulfillment_type != FulfillmentType.PICKUP and not self.postal_code:
            raise ValueError("postal_code is required for delivery")
        return self


class CheckoutFromCartCreate(BaseModel):
    merchant_slug: str = Field(default="ember-and-leaf", min_length=1, max_length=80)
    fulfillment_type: FulfillmentType = FulfillmentType.LOCAL_DELIVERY
    address_id: uuid.UUID | None = None
    location_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def validate_destination(self) -> "CheckoutFromCartCreate":
        if self.fulfillment_type == FulfillmentType.PICKUP and self.location_id is None:
            raise ValueError("location_id is required for pickup")
        if self.fulfillment_type != FulfillmentType.PICKUP and self.address_id is None:
            raise ValueError("address_id is required for delivery")
        return self


class CheckoutApprovalCreate(BaseModel):
    expected_total_minor: int = Field(ge=0)
    quote_version: int = Field(ge=1)


class CheckoutApprovalOut(BaseModel):
    id: uuid.UUID
    checkout_id: uuid.UUID
    quote_version: int
    approved_total_minor: int
    currency: str
    evidence_sha256: str
    approved_at: datetime


class CheckoutModifierOut(BaseModel):
    id: uuid.UUID
    name: str
    price_delta_minor: int

    model_config = ConfigDict(from_attributes=True)


class CheckoutLineOut(BaseModel):
    id: uuid.UUID
    variant_id: uuid.UUID
    product_name: str
    variant_name: str
    quantity: int
    unit_price_minor: int
    modifier_total_minor: int
    line_total_minor: int
    modifiers: list[CheckoutModifierOut]


class CheckoutFulfillmentOut(BaseModel):
    id: uuid.UUID
    fulfillment_type: FulfillmentType
    title: str
    fee_minor: int
    eta_min_minutes: int
    eta_max_minutes: int
    selected: bool

    model_config = ConfigDict(from_attributes=True)


class CheckoutOut(BaseModel):
    id: uuid.UUID
    merchant_id: uuid.UUID
    customer_id: uuid.UUID | None
    location_id: uuid.UUID
    status: CheckoutStatus
    currency: str
    fulfillment_type: FulfillmentType
    postal_code: str | None
    lines: list[CheckoutLineOut]
    fulfillment_options: list[CheckoutFulfillmentOut]
    subtotal_minor: int
    delivery_minor: int
    discount_minor: int
    tax_minor: int
    tax_included: bool
    total_minor: int
    quote_version: int
    expires_at: datetime
    source: str


class CheckoutCancelOut(BaseModel):
    id: uuid.UUID
    status: CheckoutStatus
