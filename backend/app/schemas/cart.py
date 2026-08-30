import uuid

from pydantic import BaseModel, Field

from app.domain.enums import CartStatus


class CartItemCreateRequest(BaseModel):
    variant_id: uuid.UUID
    quantity: int = Field(default=1, ge=1, le=25)
    modifier_option_ids: list[uuid.UUID] = Field(default_factory=list, max_length=12)


class CartItemUpdateRequest(BaseModel):
    quantity: int = Field(ge=1, le=25)


class CartModifierResponse(BaseModel):
    id: uuid.UUID
    name: str
    price_delta_minor: int


class CartItemResponse(BaseModel):
    id: uuid.UUID
    variant_id: uuid.UUID
    product_slug: str
    product_name: str
    variant_name: str
    sku: str
    image_urls: list[str]
    quantity: int
    unit_price_minor: int
    modifiers: list[CartModifierResponse]
    line_total_minor: int
    available_quantity: int | None


class CartResponse(BaseModel):
    id: uuid.UUID
    merchant_slug: str
    status: CartStatus
    currency: str
    item_count: int
    subtotal_minor: int
    items: list[CartItemResponse]
