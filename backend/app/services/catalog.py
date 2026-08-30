from sqlalchemy import or_, select
from sqlalchemy.orm import Session, selectinload

from app.db.models import InventoryItem, ModifierGroup, Product
from app.domain.enums import ProductStatus
from app.schemas.catalog import (
    CatalogProductOut,
    CatalogResponse,
    ModifierGroupOut,
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
    ) -> CatalogResponse:
        merchant = self.locations.merchant_by_slug(merchant_slug)
        location = None
        if postal_code:
            serviceable = self.locations.list_locations(merchant, postal_code)
            location = serviceable[0] if serviceable else None

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
                            ModifierGroupOut.model_validate(group)
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
