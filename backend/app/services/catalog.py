from sqlalchemy import or_, select
from sqlalchemy.orm import Session, selectinload

from app.core.errors import NotFoundError
from app.db.models import InventoryItem, ModifierGroup, Product
from app.domain.enums import FulfillmentType, ProductStatus
from app.domain.fulfillment import supports_fulfillment
from app.schemas.catalog import (
    CatalogProductOut,
    CatalogResponse,
    ModifierGroupOut,
    ModifierOptionOut,
    ProductVariantOut,
)
from app.services.location import LocationService


class CatalogService:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.locations = LocationService(db)

    def search(
        self,
        merchant_slug: str,
        query: str | None = None,
        postal_code: str | None = None,
        fulfillment_type: FulfillmentType = FulfillmentType.LOCAL_DELIVERY,
    ) -> CatalogResponse:
        merchant = self.locations.merchant_by_slug(merchant_slug)
        location = None
        if postal_code:
            if fulfillment_type == FulfillmentType.PICKUP:
                pickup_locations = self.locations.list_locations(merchant)
                location = next(
                    (
                        candidate
                        for candidate in pickup_locations
                        if candidate.postal_code == postal_code
                    ),
                    None,
                )
                if location is None:
                    raise NotFoundError(
                        "pickup_location_not_found",
                        f"No pickup location is available for postal code {postal_code}",
                    )
            else:
                location, _ = self.locations.resolve_location(
                    merchant=merchant,
                    fulfillment_type=fulfillment_type,
                    postal_code=postal_code,
                    location_id=None,
                )

        statement = (
            select(Product)
            .where(
                Product.merchant_id == merchant.id,
                Product.status == ProductStatus.ACTIVE,
            )
            .options(
                selectinload(Product.variants),
                selectinload(Product.modifier_groups).selectinload(ModifierGroup.options),
            )
            .order_by(Product.name)
        )
        if query:
            pattern = f"%{query.strip()}%"
            statement = statement.where(
                or_(Product.name.ilike(pattern), Product.description.ilike(pattern))
            )

        products = list(self.db.scalars(statement).unique())
        availability: dict[object, int] = {}
        if location:
            rows = self.db.execute(
                select(
                    InventoryItem.variant_id,
                    InventoryItem.on_hand_quantity,
                    InventoryItem.reserved_quantity,
                ).where(InventoryItem.location_id == location.id)
            )
            availability = {
                variant_id: on_hand - reserved for variant_id, on_hand, reserved in rows
            }

        output: list[CatalogProductOut] = []
        for product in products:
            if not supports_fulfillment(product.attributes, fulfillment_type):
                continue
            variants = [
                ProductVariantOut(
                    id=variant.id,
                    sku=variant.sku,
                    name=variant.name,
                    price_minor=variant.price_minor,
                    currency=variant.currency,
                    size_label=variant.size_label,
                    weight_grams=variant.weight_grams,
                    preparation_minutes=variant.preparation_minutes,
                    available_quantity=(
                        availability.get(variant.id)
                        if variant.track_inventory and location
                        else None
                    ),
                    attributes=variant.attributes,
                )
                for variant in product.variants
                if variant.sellable
                and (
                    not variant.track_inventory
                    or location is None
                    or availability.get(variant.id, 0) > 0
                )
            ]
            if variants:
                output.append(
                    CatalogProductOut(
                        id=product.id,
                        slug=product.slug,
                        name=product.name,
                        description=product.description,
                        product_type=product.product_type,
                        attributes=product.attributes,
                        image_urls=product.image_urls,
                        variants=variants,
                        modifier_groups=[
                            ModifierGroupOut(
                                id=group.id,
                                name=group.name,
                                required=group.required,
                                minimum_selections=group.minimum_selections,
                                maximum_selections=group.maximum_selections,
                                options=[
                                    ModifierOptionOut.model_validate(option)
                                    for option in group.options
                                    if option.active
                                ],
                            )
                            for group in product.modifier_groups
                        ],
                    )
                )

        return CatalogResponse(
            merchant_slug=merchant.slug,
            location_id=location.id if location else None,
            postal_code=postal_code,
            currency=merchant.currency,
            products=output,
        )
