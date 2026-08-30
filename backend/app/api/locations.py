from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.domain.enums import FulfillmentType
from app.schemas.location import FulfillmentPreviewOut, LocationOut, LocationsResponse
from app.services.location import LocationService

router = APIRouter(prefix="/merchants/{merchant_slug}/locations", tags=["locations"])


@router.get("", response_model=LocationsResponse)
def list_locations(
    merchant_slug: str,
    postal_code: str | None = Query(default=None, min_length=3, max_length=20),
    db: Session = Depends(get_db),
) -> LocationsResponse:
    service = LocationService(db)
    merchant = service.merchant_by_slug(merchant_slug)
    locations = service.list_locations(merchant, postal_code)
    return LocationsResponse(
        merchant_slug=merchant.slug,
        postal_code=postal_code,
        locations=[
            LocationOut(
                id=location.id,
                slug=location.slug,
                name=location.name,
                kind=location.kind,
                postal_code=location.postal_code,
                latitude=location.latitude,
                longitude=location.longitude,
                preparation_minutes=location.preparation_minutes,
                fulfillment=[
                    FulfillmentPreviewOut(
                        service_zone_id=zone.id,
                        fulfillment_type=zone.fulfillment_type,
                        title=_fulfillment_title(zone.fulfillment_type),
                        fee_minor=zone.fee_minor,
                        free_above_minor=zone.free_above_minor,
                        eta_min_minutes=zone.eta_min_minutes,
                        eta_max_minutes=zone.eta_max_minutes,
                    )
                    for zone in location.service_zones
                    if zone.active and (postal_code is None or postal_code in zone.postal_codes)
                ],
            )
            for location in locations
        ],
    )


def _fulfillment_title(fulfillment_type: FulfillmentType) -> str:
    return {
        FulfillmentType.PICKUP: "Pickup",
        FulfillmentType.LOCAL_DELIVERY: "Local delivery",
        FulfillmentType.SHIPPING: "Standard shipping",
    }[fulfillment_type]
