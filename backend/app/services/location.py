import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.core.errors import NotFoundError
from app.db.models import Location, Merchant, ServiceZone
from app.domain.enums import FulfillmentType, MerchantStatus


class LocationService:
    def __init__(self, db: Session) -> None:
        self.db = db

    def merchant_by_slug(self, merchant_slug: str) -> Merchant:
        merchant = self.db.scalar(
            select(Merchant).where(
                Merchant.slug == merchant_slug,
                Merchant.status == MerchantStatus.ACTIVE,
            )
        )
        if merchant is None:
            raise NotFoundError("merchant_not_found", "Active merchant was not found")
        return merchant

    def list_locations(self, merchant: Merchant, postal_code: str | None = None) -> list[Location]:
        locations = list(
            self.db.scalars(
                select(Location)
                .where(Location.merchant_id == merchant.id, Location.active.is_(True))
                .options(selectinload(Location.service_zones))
                .order_by(Location.name)
            ).unique()
        )
        if postal_code is None:
            return locations
        return [
            location
            for location in locations
            if any(
                zone.active and postal_code in zone.postal_codes for zone in location.service_zones
            )
        ]

    def resolve_location(
        self,
        merchant: Merchant,
        fulfillment_type: FulfillmentType,
        postal_code: str | None,
        location_id: uuid.UUID | None,
    ) -> tuple[Location, ServiceZone | None]:
        if fulfillment_type == FulfillmentType.PICKUP:
            location = self.db.scalar(
                select(Location)
                .where(
                    Location.id == location_id,
                    Location.merchant_id == merchant.id,
                    Location.active.is_(True),
                )
                .options(selectinload(Location.service_zones))
            )
            if location is None:
                raise NotFoundError("pickup_location_not_found", "Pickup location is unavailable")
            return location, None

        candidates = self.list_locations(merchant, postal_code)
        matching: list[tuple[Location, ServiceZone]] = []
        for location in candidates:
            for zone in location.service_zones:
                if (
                    zone.active
                    and zone.fulfillment_type == fulfillment_type
                    and postal_code in zone.postal_codes
                ):
                    matching.append((location, zone))

        if not matching:
            raise NotFoundError(
                "address_not_serviceable",
                f"No {fulfillment_type.value} service is available for postal code {postal_code}",
            )
        return min(matching, key=lambda candidate: candidate[1].eta_max_minutes)
