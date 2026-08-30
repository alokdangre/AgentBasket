import uuid
from decimal import Decimal

from pydantic import BaseModel

from app.domain.enums import FulfillmentType, LocationKind


class FulfillmentPreviewOut(BaseModel):
    service_zone_id: uuid.UUID | None
    fulfillment_type: FulfillmentType
    title: str
    fee_minor: int
    free_above_minor: int | None
    eta_min_minutes: int
    eta_max_minutes: int


class LocationOut(BaseModel):
    id: uuid.UUID
    slug: str
    name: str
    kind: LocationKind
    postal_code: str
    latitude: Decimal | None
    longitude: Decimal | None
    preparation_minutes: int
    fulfillment: list[FulfillmentPreviewOut]


class LocationsResponse(BaseModel):
    merchant_slug: str
    postal_code: str | None
    locations: list[LocationOut]
