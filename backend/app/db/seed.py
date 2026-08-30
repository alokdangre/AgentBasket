from datetime import date, time, timedelta
from decimal import Decimal

from sqlalchemy import select

from app.core.config import get_settings
from app.core.database import SessionLocal
from app.core.security import hash_password
from app.db.models import (
    InventoryBatch,
    InventoryItem,
    Location,
    LocationHours,
    Merchant,
    ModifierGroup,
    ModifierOption,
    Product,
    ProductVariant,
    ServiceZone,
    UserAccount,
)
from app.domain.enums import (
    FulfillmentType,
    LocationKind,
    ProductStatus,
    ProductType,
    UserRole,
)


def seed_merchant_admin(db, merchant: Merchant) -> None:
    settings = get_settings()
    if not settings.merchant_admin_email or not settings.merchant_admin_password:
        return
    email = settings.merchant_admin_email.strip().casefold()
    if db.scalar(select(UserAccount.id).where(UserAccount.email == email)):
        return
    db.add(
        UserAccount(
            email=email,
            password_hash=hash_password(settings.merchant_admin_password),
            full_name=settings.merchant_admin_name,
            role=UserRole.MERCHANT_ADMIN,
            merchant_id=merchant.id,
        )
    )


def seed_database() -> None:
    with SessionLocal() as db, db.begin():
        existing = db.scalar(select(Merchant).where(Merchant.slug == "ember-and-leaf"))
        if existing:
            seed_merchant_admin(db, existing)
            return

        merchant = Merchant(
            slug="ember-and-leaf",
            name="Ember & Leaf",
            currency="INR",
            timezone="Asia/Kolkata",
            settings={
                "prices_include_tax": True,
                "default_checkout_ttl_minutes": 10,
                "prepared_beverage_delivery": True,
            },
        )
        db.add(merchant)
        db.flush()

        indiranagar = Location(
            merchant_id=merchant.id,
            slug="indiranagar-cafe",
            name="Indiranagar Café",
            kind=LocationKind.CAFE,
            latitude=Decimal("12.978369"),
            longitude=Decimal("77.640835"),
            postal_code="560038",
            preparation_minutes=12,
            address={
                "line_one": "100 Feet Road",
                "city": "Bengaluru",
                "region": "Karnataka",
                "country": "IN",
                "postal_code": "560038",
            },
        )
        central = Location(
            merchant_id=merchant.id,
            slug="central-roastery",
            name="Central Roastery",
            kind=LocationKind.ROASTERY,
            latitude=Decimal("12.950690"),
            longitude=Decimal("77.600090"),
            postal_code="560027",
            preparation_minutes=25,
            address={
                "line_one": "Lalbagh Road",
                "city": "Bengaluru",
                "region": "Karnataka",
                "country": "IN",
                "postal_code": "560027",
            },
        )
        db.add_all([indiranagar, central])
        db.flush()

        for location in (indiranagar, central):
            db.add_all(
                [
                    LocationHours(
                        location_id=location.id,
                        weekday=weekday,
                        opens_at=time(7, 0),
                        closes_at=time(22, 0),
                    )
                    for weekday in range(7)
                ]
            )

        db.add_all(
            [
                ServiceZone(
                    location_id=indiranagar.id,
                    name="Indiranagar express",
                    fulfillment_type=FulfillmentType.LOCAL_DELIVERY,
                    postal_codes=["560008", "560038", "560071", "560075"],
                    radius_km=Decimal("7.00"),
                    minimum_order_minor=20000,
                    fee_minor=4900,
                    free_above_minor=100000,
                    eta_min_minutes=45,
                    eta_max_minutes=90,
                    cutoff_time=time(20, 30),
                ),
                ServiceZone(
                    location_id=central.id,
                    name="Central same-day",
                    fulfillment_type=FulfillmentType.LOCAL_DELIVERY,
                    postal_codes=["560001", "560002", "560025", "560027", "560029"],
                    radius_km=Decimal("12.00"),
                    minimum_order_minor=30000,
                    fee_minor=7900,
                    free_above_minor=150000,
                    eta_min_minutes=120,
                    eta_max_minutes=240,
                    cutoff_time=time(15, 0),
                ),
                ServiceZone(
                    location_id=central.id,
                    name="Bengaluru next-day",
                    fulfillment_type=FulfillmentType.SHIPPING,
                    postal_codes=[
                        "560001",
                        "560008",
                        "560025",
                        "560027",
                        "560029",
                        "560038",
                        "560071",
                        "560075",
                    ],
                    minimum_order_minor=50000,
                    fee_minor=9900,
                    free_above_minor=200000,
                    eta_min_minutes=720,
                    eta_max_minutes=1440,
                    cutoff_time=time(14, 0),
                ),
            ]
        )

        cold_brew = Product(
            merchant_id=merchant.id,
            slug="house-cold-brew",
            name="House Cold Brew",
            description="Slow-steeped for a smooth cocoa finish.",
            product_type=ProductType.PREPARED_BEVERAGE,
            status=ProductStatus.ACTIVE,
            attributes={"temperature": "iced", "caffeine": "regular", "dietary": ["vegan"]},
            image_urls=["/images/catalog/house-cold-brew.webp"],
        )
        citrus_bloom = Product(
            merchant_id=merchant.id,
            slug="citrus-bloom-coffee",
            name="Citrus Bloom Single-Origin Coffee",
            description="A medium roast with orange blossom, peach and caramel notes.",
            product_type=ProductType.PACKAGED_COFFEE,
            status=ProductStatus.ACTIVE,
            attributes={
                "origin": "Chikmagalur, India",
                "roast": "medium",
                "tasting_notes": ["orange blossom", "peach", "caramel"],
                "brew_methods": ["v60", "aeropress", "french_press"],
            },
            image_urls=["/images/catalog/citrus-bloom.webp"],
        )
        masala_chai = Product(
            merchant_id=merchant.id,
            slug="masala-cloud-chai",
            name="Masala Cloud Chai",
            description="Assam tea, ginger and warm spices steamed to order.",
            product_type=ProductType.PREPARED_BEVERAGE,
            status=ProductStatus.ACTIVE,
            attributes={"temperature_options": ["hot", "iced"], "caffeine": "regular"},
            image_urls=["/images/catalog/masala-cloud-chai.webp"],
        )
        darjeeling = Product(
            merchant_id=merchant.id,
            slug="darjeeling-first-flush",
            name="Darjeeling First Flush",
            description="A floral loose-leaf tea with muscatel sweetness.",
            product_type=ProductType.PACKAGED_TEA,
            status=ProductStatus.ACTIVE,
            attributes={
                "origin": "Darjeeling, India",
                "tea_type": "black",
                "tasting_notes": ["floral", "muscatel"],
            },
            image_urls=["/images/catalog/darjeeling-first-flush.webp"],
        )
        filters = Product(
            merchant_id=merchant.id,
            slug="v60-filter-papers",
            name="V60 Filter Papers",
            description="Oxygen-bleached size 02 paper filters, pack of 100.",
            product_type=ProductType.ACCESSORY,
            status=ProductStatus.ACTIVE,
            attributes={"compatible_with": ["v60-02"], "count": 100},
            image_urls=["/images/catalog/v60-filters.webp"],
        )
        db.add_all([cold_brew, citrus_bloom, masala_chai, darjeeling, filters])
        db.flush()

        variants = [
            ProductVariant(
                merchant_id=merchant.id,
                product_id=cold_brew.id,
                sku="DRINK-CB-REG",
                name="Regular",
                size_label="300 ml",
                price_minor=22000,
                preparation_minutes=8,
                track_inventory=False,
                attributes={"volume_ml": 300},
            ),
            ProductVariant(
                merchant_id=merchant.id,
                product_id=cold_brew.id,
                sku="DRINK-CB-LRG",
                name="Large",
                size_label="450 ml",
                price_minor=28000,
                preparation_minutes=8,
                track_inventory=False,
                attributes={"volume_ml": 450},
            ),
            ProductVariant(
                merchant_id=merchant.id,
                product_id=citrus_bloom.id,
                sku="BEAN-CITRUS-250",
                name="250 g",
                size_label="250 g",
                weight_grams=250,
                price_minor=65000,
                attributes={"replenishment_days": 14},
            ),
            ProductVariant(
                merchant_id=merchant.id,
                product_id=citrus_bloom.id,
                sku="BEAN-CITRUS-500",
                name="500 g",
                size_label="500 g",
                weight_grams=500,
                price_minor=119000,
                attributes={"replenishment_days": 28},
            ),
            ProductVariant(
                merchant_id=merchant.id,
                product_id=masala_chai.id,
                sku="DRINK-CHAI-REG",
                name="Regular",
                size_label="250 ml",
                price_minor=18000,
                preparation_minutes=10,
                track_inventory=False,
                attributes={"volume_ml": 250},
            ),
            ProductVariant(
                merchant_id=merchant.id,
                product_id=masala_chai.id,
                sku="DRINK-CHAI-LRG",
                name="Large",
                size_label="350 ml",
                price_minor=23000,
                preparation_minutes=10,
                track_inventory=False,
                attributes={"volume_ml": 350},
            ),
            ProductVariant(
                merchant_id=merchant.id,
                product_id=darjeeling.id,
                sku="TEA-DARJ-100",
                name="100 g pouch",
                size_label="100 g",
                weight_grams=100,
                price_minor=78000,
                attributes={"replenishment_days": 30},
            ),
            ProductVariant(
                merchant_id=merchant.id,
                product_id=filters.id,
                sku="GEAR-V60-FILTER-100",
                name="Pack of 100",
                size_label="100 filters",
                price_minor=35000,
                attributes={},
            ),
        ]
        db.add_all(variants)
        db.flush()

        milk = ModifierGroup(
            product_id=cold_brew.id,
            name="Milk",
            minimum_selections=0,
            maximum_selections=1,
            position=1,
        )
        sweetness = ModifierGroup(
            product_id=cold_brew.id,
            name="Sweetness",
            required=True,
            minimum_selections=1,
            maximum_selections=1,
            position=2,
        )
        grind = ModifierGroup(
            product_id=citrus_bloom.id,
            name="Grind",
            required=True,
            minimum_selections=1,
            maximum_selections=1,
            position=1,
        )
        chai_milk = ModifierGroup(
            product_id=masala_chai.id,
            name="Milk",
            required=True,
            minimum_selections=1,
            maximum_selections=1,
            position=1,
        )
        db.add_all([milk, sweetness, grind, chai_milk])
        db.flush()
        db.add_all(
            [
                ModifierOption(group_id=milk.id, name="No milk", price_delta_minor=0, position=1),
                ModifierOption(
                    group_id=milk.id, name="Oat milk", price_delta_minor=4000, position=2
                ),
                ModifierOption(
                    group_id=milk.id, name="Almond milk", price_delta_minor=5000, position=3
                ),
                ModifierOption(
                    group_id=sweetness.id, name="Unsweetened", price_delta_minor=0, position=1
                ),
                ModifierOption(
                    group_id=sweetness.id, name="Lightly sweet", price_delta_minor=0, position=2
                ),
                ModifierOption(
                    group_id=sweetness.id, name="Regular sweet", price_delta_minor=0, position=3
                ),
                ModifierOption(
                    group_id=grind.id, name="Whole bean", price_delta_minor=0, position=1
                ),
                ModifierOption(group_id=grind.id, name="V60", price_delta_minor=0, position=2),
                ModifierOption(group_id=grind.id, name="Espresso", price_delta_minor=0, position=3),
                ModifierOption(
                    group_id=grind.id, name="French press", price_delta_minor=0, position=4
                ),
                ModifierOption(
                    group_id=chai_milk.id, name="Dairy milk", price_delta_minor=0, position=1
                ),
                ModifierOption(
                    group_id=chai_milk.id, name="Oat milk", price_delta_minor=4000, position=2
                ),
            ]
        )

        stocked_variants = [variant for variant in variants if variant.track_inventory]
        for location in (indiranagar, central):
            for variant in stocked_variants:
                db.add(
                    InventoryItem(
                        location_id=location.id,
                        variant_id=variant.id,
                        on_hand_quantity=40 if location is central else 12,
                        reserved_quantity=0,
                        reorder_point=8,
                    )
                )

        coffee_variants = [variant for variant in variants if variant.sku.startswith("BEAN-")]
        for variant in coffee_variants:
            db.add(
                InventoryBatch(
                    location_id=central.id,
                    variant_id=variant.id,
                    lot_code=f"ROAST-{date.today().isoformat()}-{variant.sku[-3:]}",
                    quantity=24,
                    roasted_at=date.today() - timedelta(days=2),
                    best_before=date.today() + timedelta(days=88),
                )
            )

        seed_merchant_admin(db, merchant)


if __name__ == "__main__":
    seed_database()
