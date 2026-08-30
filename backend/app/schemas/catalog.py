import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict

from app.domain.enums import ProductType


class ModifierOptionOut(BaseModel):
    id: uuid.UUID
    name: str
    price_delta_minor: int
    attributes: dict[str, Any]

    model_config = ConfigDict(from_attributes=True)


class ModifierGroupOut(BaseModel):
    id: uuid.UUID
    name: str
    required: bool
    minimum_selections: int
    maximum_selections: int
    options: list[ModifierOptionOut]

    model_config = ConfigDict(from_attributes=True)


class ProductVariantOut(BaseModel):
    id: uuid.UUID
    sku: str
    name: str
    price_minor: int
    currency: str
    size_label: str | None
    weight_grams: int | None
    preparation_minutes: int
    available_quantity: int | None
    attributes: dict[str, Any]


class CatalogProductOut(BaseModel):
    id: uuid.UUID
    slug: str
    name: str
    description: str
    product_type: ProductType
    attributes: dict[str, Any]
    image_urls: list[str]
    variants: list[ProductVariantOut]
    modifier_groups: list[ModifierGroupOut]


class CatalogResponse(BaseModel):
    merchant_slug: str
    location_id: uuid.UUID | None
    postal_code: str | None
    currency: str
    products: list[CatalogProductOut]
