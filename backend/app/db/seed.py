from __future__ import annotations

from collections.abc import Callable
from datetime import date, time, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import SessionLocal
from app.core.security import hash_password
from app.db.models import (
    CustomerAddress,
    InventoryBatch,
    InventoryItem,
    Location,
    LocationHours,
    Merchant,
    ModifierGroup,
    ModifierOption,
    PaymentInstrument,
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

SessionFactory = Callable[[], Session]


def _upsert[ModelT](
    db: Session,
    model: type[ModelT],
    lookup: dict[str, Any],
    values: dict[str, Any],
    *,
    update_existing: bool = True,
) -> ModelT:
    instance = db.scalar(select(model).filter_by(**lookup))
    if instance is None:
        instance = model(**lookup, **values)
        db.add(instance)
        db.flush()
    elif update_existing:
        for field, value in values.items():
            setattr(instance, field, value)
    return instance


def seed_merchant_admin(db: Session, merchant: Merchant) -> None:
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


def seed_demo_customer(db: Session) -> None:
    settings = get_settings()
    if settings.app_env.strip().casefold() in {"prod", "production"}:
        return
    if not settings.demo_customer_email or settings.demo_customer_password is None:
        return

    email = settings.demo_customer_email.strip().casefold()
    customer = db.scalar(select(UserAccount).where(UserAccount.email == email))
    if customer is None:
        customer = UserAccount(
            email=email,
            password_hash=hash_password(settings.demo_customer_password.get_secret_value()),
            full_name=settings.demo_customer_name.strip(),
            phone=settings.demo_customer_phone.strip(),
            role=UserRole.CUSTOMER,
        )
        db.add(customer)
        db.flush()
    elif customer.role != UserRole.CUSTOMER:
        return

    address_specs = [
        {
            "label": "Home",
            "recipient_name": customer.full_name,
            "phone": customer.phone or settings.demo_customer_phone,
            "line_one": "12, 100 Feet Road",
            "line_two": None,
            "landmark": "Near CMH Park",
            "city": "Bengaluru",
            "region": "Karnataka",
            "postal_code": "560038",
            "country_code": "IN",
        },
        {
            "label": "Office",
            "recipient_name": customer.full_name,
            "phone": customer.phone or settings.demo_customer_phone,
            "line_one": "42, Residency Road",
            "line_two": "Third floor",
            "landmark": "Near Mayo Hall",
            "city": "Bengaluru",
            "region": "Karnataka",
            "postal_code": "560001",
            "country_code": "IN",
        },
        {
            "label": "Outside delivery area",
            "recipient_name": customer.full_name,
            "phone": customer.phone or settings.demo_customer_phone,
            "line_one": "11, Marine Drive",
            "line_two": None,
            "landmark": None,
            "city": "Mumbai",
            "region": "Maharashtra",
            "postal_code": "400001",
            "country_code": "IN",
        },
    ]
    default_address_id = db.scalar(
        select(CustomerAddress.id).where(
            CustomerAddress.user_id == customer.id,
            CustomerAddress.is_default.is_(True),
        )
    )
    for spec in address_specs:
        label = spec["label"]
        address = _upsert(
            db,
            CustomerAddress,
            {"user_id": customer.id, "label": label},
            {key: value for key, value in spec.items() if key != "label"} | {"is_default": False},
            update_existing=False,
        )
        if default_address_id is None and label == "Home":
            address.is_default = True
            default_address_id = address.id

    _upsert(
        db,
        PaymentInstrument,
        {
            "user_id": customer.id,
            "provider": "razorpay_test",
            "alias": "Razorpay Test Checkout",
        },
        {
            "instrument_type": "com.razorpay.standard.test",
            "provider_customer_id": None,
            "provider_token_reference": None,
            "network": None,
            "last4": None,
            "status": "active",
            "is_default": True,
            "instrument_metadata": {
                "mode": "test",
                "requires_provider_checkout": True,
                "stores_pan": False,
            },
        },
    )


def _seed_locations(db: Session, merchant: Merchant) -> dict[str, Location]:
    location_specs = [
        {
            "slug": "indiranagar-cafe",
            "name": "Indiranagar Café",
            "kind": LocationKind.CAFE,
            "latitude": Decimal("12.978369"),
            "longitude": Decimal("77.640835"),
            "postal_code": "560038",
            "preparation_minutes": 12,
            "address": {
                "line_one": "100 Feet Road",
                "city": "Bengaluru",
                "region": "Karnataka",
                "country": "IN",
                "postal_code": "560038",
            },
            "active": True,
        },
        {
            "slug": "central-roastery",
            "name": "Central Roastery",
            "kind": LocationKind.ROASTERY,
            "latitude": Decimal("12.950690"),
            "longitude": Decimal("77.600090"),
            "postal_code": "560027",
            "preparation_minutes": 25,
            "address": {
                "line_one": "Lalbagh Road",
                "city": "Bengaluru",
                "region": "Karnataka",
                "country": "IN",
                "postal_code": "560027",
            },
            "active": True,
        },
    ]
    locations: dict[str, Location] = {}
    for spec in location_specs:
        slug = spec["slug"]
        locations[slug] = _upsert(
            db,
            Location,
            {"merchant_id": merchant.id, "slug": slug},
            {key: value for key, value in spec.items() if key != "slug"},
        )

    for location_slug, location in locations.items():
        for weekday in range(7):
            central_sunday = location_slug == "central-roastery" and weekday == 6
            _upsert(
                db,
                LocationHours,
                {"location_id": location.id, "weekday": weekday},
                {
                    "opens_at": None
                    if central_sunday
                    else time(8, 0)
                    if location_slug == "central-roastery"
                    else time(7, 0),
                    "closes_at": None
                    if central_sunday
                    else time(18, 0)
                    if location_slug == "central-roastery"
                    else time(22, 0),
                    "closed": central_sunday,
                },
            )

    zone_specs = [
        {
            "location": "indiranagar-cafe",
            "name": "Indiranagar express",
            "fulfillment_type": FulfillmentType.LOCAL_DELIVERY,
            "postal_codes": ["560008", "560038", "560071", "560075"],
            "radius_km": Decimal("7.00"),
            "minimum_order_minor": 20000,
            "fee_minor": 4900,
            "free_above_minor": 100000,
            "eta_min_minutes": 45,
            "eta_max_minutes": 90,
            "cutoff_time": time(20, 30),
            "active": True,
        },
        {
            "location": "central-roastery",
            "name": "Central same-day",
            "fulfillment_type": FulfillmentType.LOCAL_DELIVERY,
            "postal_codes": ["560001", "560002", "560025", "560027", "560029"],
            "radius_km": Decimal("12.00"),
            "minimum_order_minor": 30000,
            "fee_minor": 7900,
            "free_above_minor": 150000,
            "eta_min_minutes": 120,
            "eta_max_minutes": 240,
            "cutoff_time": time(15, 0),
            "active": True,
        },
        {
            "location": "central-roastery",
            "name": "Bengaluru next-day",
            "fulfillment_type": FulfillmentType.SHIPPING,
            "postal_codes": [
                "560001",
                "560008",
                "560025",
                "560027",
                "560029",
                "560038",
                "560071",
                "560075",
            ],
            "radius_km": None,
            "minimum_order_minor": 50000,
            "fee_minor": 9900,
            "free_above_minor": 200000,
            "eta_min_minutes": 720,
            "eta_max_minutes": 1440,
            "cutoff_time": time(14, 0),
            "active": True,
        },
    ]
    for spec in zone_specs:
        location = locations[spec["location"]]
        _upsert(
            db,
            ServiceZone,
            {
                "location_id": location.id,
                "name": spec["name"],
                "fulfillment_type": spec["fulfillment_type"],
            },
            {
                key: value
                for key, value in spec.items()
                if key not in {"location", "name", "fulfillment_type"}
            },
        )
    return locations


def _seed_products(
    db: Session, merchant: Merchant
) -> tuple[dict[str, Product], dict[str, ProductVariant], list[dict[str, Any]]]:
    product_specs = [
        {
            "slug": "house-cold-brew",
            "name": "House Cold Brew",
            "description": "Slow-steeped iced coffee with a smooth cocoa finish.",
            "product_type": ProductType.PREPARED_BEVERAGE,
            "status": ProductStatus.ACTIVE,
            "attributes": {
                "flavor_tags": ["cocoa", "smooth"],
                "caffeine_level": "regular",
                "dietary": ["vegan"],
                "temperature_options": ["iced"],
                "occasion": ["morning", "afternoon"],
            },
        },
        {
            "slug": "citrus-bloom-coffee",
            "name": "Citrus Bloom Single-Origin Coffee",
            "description": "Medium-roast coffee with orange blossom, peach and caramel notes.",
            "product_type": ProductType.PACKAGED_COFFEE,
            "status": ProductStatus.ACTIVE,
            "attributes": {
                "origin": "Chikmagalur, India",
                "roast": "medium",
                "flavor_tags": ["citrus", "floral", "caramel", "fruity"],
                "caffeine_level": "regular",
                "brew_methods": ["v60", "aeropress", "french press"],
                "occasion": ["morning"],
            },
        },
        {
            "slug": "masala-cloud-chai",
            "name": "Masala Cloud Chai",
            "description": "Assam tea, ginger and warm spices steamed to order.",
            "product_type": ProductType.PREPARED_BEVERAGE,
            "status": ProductStatus.ACTIVE,
            "attributes": {
                "flavor_tags": ["spiced", "ginger", "warming"],
                "caffeine_level": "regular",
                "dietary": ["vegan option"],
                "temperature_options": ["hot", "iced"],
                "occasion": ["morning", "afternoon"],
            },
        },
        {
            "slug": "darjeeling-first-flush",
            "name": "Darjeeling First Flush",
            "description": "Floral loose-leaf black tea with muscatel sweetness.",
            "product_type": ProductType.PACKAGED_TEA,
            "status": ProductStatus.ACTIVE,
            "attributes": {
                "origin": "Darjeeling, India",
                "tea_type": "black",
                "flavor_tags": ["floral", "muscatel", "delicate"],
                "caffeine_level": "regular",
                "occasion": ["afternoon"],
            },
        },
        {
            "slug": "v60-filter-papers",
            "name": "V60 Filter Papers",
            "description": "Oxygen-bleached size 02 paper filters, pack of 100.",
            "product_type": ProductType.ACCESSORY,
            "status": ProductStatus.ACTIVE,
            "attributes": {
                "compatible_with": ["v60", "v60-02"],
                "count": 100,
                "cross_sell_for": ["packaged coffee"],
            },
        },
        {
            "slug": "ember-flat-white",
            "name": "Ember Flat White",
            "description": "A café-style double-shot flat white with a chocolatey finish.",
            "product_type": ProductType.PREPARED_BEVERAGE,
            "status": ProductStatus.ACTIVE,
            "attributes": {
                "flavor_tags": ["chocolate", "nutty", "creamy"],
                "caffeine_level": "regular",
                "dietary": ["vegan option"],
                "temperature_options": ["hot", "iced"],
                "occasion": ["morning"],
            },
        },
        {
            "slug": "ceremonial-matcha-latte",
            "name": "Ceremonial Matcha Latte",
            "description": "Stone-ground matcha whisked into a smooth café latte.",
            "product_type": ProductType.PREPARED_BEVERAGE,
            "status": ProductStatus.ACTIVE,
            "attributes": {
                "flavor_tags": ["grassy", "creamy", "umami"],
                "caffeine_level": "lower caffeine",
                "dietary": ["vegan option"],
                "temperature_options": ["hot", "iced"],
                "occasion": ["morning", "afternoon"],
            },
        },
        {
            "slug": "espresso-tonic",
            "name": "Espresso Tonic",
            "description": "Bright espresso over tonic and citrus for a crisp iced drink.",
            "product_type": ProductType.PREPARED_BEVERAGE,
            "status": ProductStatus.ACTIVE,
            "attributes": {
                "flavor_tags": ["citrus", "bright", "refreshing"],
                "caffeine_level": "regular",
                "dietary": ["vegan"],
                "temperature_options": ["iced"],
                "occasion": ["afternoon"],
            },
        },
        {
            "slug": "bengaluru-filter-coffee",
            "name": "Bengaluru Filter Coffee",
            "description": "South Indian filter coffee with a deep roast and silky milk.",
            "product_type": ProductType.PREPARED_BEVERAGE,
            "status": ProductStatus.ACTIVE,
            "attributes": {
                "flavor_tags": ["bold", "caramel", "roasty"],
                "caffeine_level": "regular",
                "dietary": ["vegetarian", "vegan option"],
                "temperature_options": ["hot"],
                "occasion": ["morning", "afternoon"],
            },
        },
        {
            "slug": "hibiscus-citrus-iced-tea",
            "name": "Hibiscus Citrus Iced Tea",
            "description": "Tart hibiscus, orange and lemongrass shaken over ice.",
            "product_type": ProductType.PREPARED_BEVERAGE,
            "status": ProductStatus.ACTIVE,
            "attributes": {
                "flavor_tags": ["tart", "citrus", "refreshing"],
                "caffeine_level": "caffeine free",
                "dietary": ["vegan"],
                "temperature_options": ["iced"],
                "occasion": ["afternoon", "evening"],
            },
        },
        {
            "slug": "sparkling-kokum-cooler",
            "name": "Sparkling Kokum Cooler",
            "description": "Kokum, lime and soda with a lightly salted, tangy finish.",
            "product_type": ProductType.PREPARED_BEVERAGE,
            "status": ProductStatus.ACTIVE,
            "attributes": {
                "flavor_tags": ["tangy", "lime", "sparkling"],
                "caffeine_level": "caffeine free",
                "dietary": ["vegan"],
                "temperature_options": ["iced"],
                "occasion": ["afternoon", "evening"],
            },
        },
        {
            "slug": "salted-jaggery-hot-chocolate",
            "name": "Salted Jaggery Hot Chocolate",
            "description": "Dark cocoa, jaggery and sea salt steamed into a rich chocolate drink.",
            "product_type": ProductType.PREPARED_BEVERAGE,
            "status": ProductStatus.ACTIVE,
            "attributes": {
                "flavor_tags": ["chocolate", "caramel", "rich"],
                "caffeine_level": "low caffeine",
                "dietary": ["vegetarian", "vegan option"],
                "temperature_options": ["hot"],
                "occasion": ["afternoon", "evening"],
            },
        },
        {
            "slug": "monsoon-decaf-coffee",
            "name": "Monsoon Decaf Coffee",
            "description": "Swiss-water decaf coffee with cocoa, almond and brown sugar notes.",
            "product_type": ProductType.PACKAGED_COFFEE,
            "status": ProductStatus.ACTIVE,
            "attributes": {
                "origin": "Karnataka, India",
                "roast": "medium-dark",
                "flavor_tags": ["cocoa", "nutty", "brown sugar"],
                "caffeine_level": "decaf",
                "brew_methods": ["espresso", "v60", "french press"],
                "occasion": ["evening"],
            },
        },
        {
            "slug": "chamomile-citrus-tisane",
            "name": "Chamomile Citrus Tisane",
            "description": "Caffeine-free chamomile, lemongrass and orange peel for evenings.",
            "product_type": ProductType.PACKAGED_TEA,
            "status": ProductStatus.ACTIVE,
            "attributes": {
                "tea_type": "herbal tisane",
                "flavor_tags": ["floral", "citrus", "calming"],
                "caffeine_level": "caffeine free",
                "dietary": ["vegan"],
                "occasion": ["evening", "bedtime"],
            },
        },
        {
            "slug": "assam-breakfast-tea",
            "name": "Assam Breakfast Tea",
            "description": "A bold, malty loose-leaf breakfast tea from Assam.",
            "product_type": ProductType.PACKAGED_TEA,
            "status": ProductStatus.ACTIVE,
            "attributes": {
                "origin": "Assam, India",
                "tea_type": "black",
                "flavor_tags": ["malty", "strong", "rich"],
                "caffeine_level": "regular",
                "occasion": ["morning"],
            },
        },
        {
            "slug": "ceramic-v60-dripper",
            "name": "Ceramic V60 Dripper",
            "description": "A size 02 ceramic pour-over brewer for clean, bright coffee.",
            "product_type": ProductType.ACCESSORY,
            "status": ProductStatus.ACTIVE,
            "attributes": {
                "compatible_with": ["v60", "v60-02"],
                "brew_methods": ["v60", "pour over"],
                "cross_sell_for": ["packaged coffee", "v60 filter papers"],
            },
        },
        {
            "slug": "festival-reserve-coffee",
            "name": "Festival Reserve Coffee",
            "description": "A future seasonal micro-lot kept in draft for exclusion tests.",
            "product_type": ProductType.PACKAGED_COFFEE,
            "status": ProductStatus.DRAFT,
            "attributes": {
                "flavor_tags": ["seasonal", "berry"],
                "caffeine_level": "regular",
            },
        },
    ]
    products: dict[str, Product] = {}
    for spec in product_specs:
        slug = spec["slug"]
        fulfillment_types = [
            FulfillmentType.PICKUP.value,
            FulfillmentType.LOCAL_DELIVERY.value,
        ]
        if spec["product_type"] != ProductType.PREPARED_BEVERAGE:
            fulfillment_types.append(FulfillmentType.SHIPPING.value)
        products[slug] = _upsert(
            db,
            Product,
            {"merchant_id": merchant.id, "slug": slug},
            {key: value for key, value in spec.items() if key not in {"slug", "attributes"}}
            | {
                "attributes": {
                    **spec["attributes"],
                    "fulfillment_types": fulfillment_types,
                },
                "image_urls": [],
            },
        )

    variant_specs = [
        {
            "product": "house-cold-brew",
            "sku": "DRINK-CB-REG",
            "name": "Regular",
            "size_label": "300 ml",
            "price_minor": 22000,
            "preparation_minutes": 8,
            "track_inventory": False,
            "attributes": {"volume_ml": 300},
        },
        {
            "product": "house-cold-brew",
            "sku": "DRINK-CB-LRG",
            "name": "Large",
            "size_label": "450 ml",
            "price_minor": 28000,
            "preparation_minutes": 8,
            "track_inventory": False,
            "attributes": {"volume_ml": 450},
        },
        {
            "product": "citrus-bloom-coffee",
            "sku": "BEAN-CITRUS-250",
            "name": "250 g",
            "size_label": "250 g",
            "weight_grams": 250,
            "price_minor": 65000,
            "track_inventory": True,
            "attributes": {"replenishment_days": 14},
        },
        {
            "product": "citrus-bloom-coffee",
            "sku": "BEAN-CITRUS-500",
            "name": "500 g",
            "size_label": "500 g",
            "weight_grams": 500,
            "price_minor": 119000,
            "track_inventory": True,
            "attributes": {"replenishment_days": 28},
        },
        {
            "product": "citrus-bloom-coffee",
            "sku": "BEAN-CITRUS-1000-PAUSED",
            "name": "1 kg paused",
            "size_label": "1 kg",
            "weight_grams": 1000,
            "price_minor": 219000,
            "track_inventory": True,
            "sellable": False,
            "attributes": {"replenishment_days": 45},
        },
        {
            "product": "masala-cloud-chai",
            "sku": "DRINK-CHAI-REG",
            "name": "Regular",
            "size_label": "250 ml",
            "price_minor": 18000,
            "preparation_minutes": 10,
            "track_inventory": False,
            "attributes": {"volume_ml": 250},
        },
        {
            "product": "masala-cloud-chai",
            "sku": "DRINK-CHAI-LRG",
            "name": "Large",
            "size_label": "350 ml",
            "price_minor": 23000,
            "preparation_minutes": 10,
            "track_inventory": False,
            "attributes": {"volume_ml": 350},
        },
        {
            "product": "darjeeling-first-flush",
            "sku": "TEA-DARJ-100",
            "name": "100 g pouch",
            "size_label": "100 g",
            "weight_grams": 100,
            "price_minor": 78000,
            "track_inventory": True,
            "attributes": {"replenishment_days": 30},
        },
        {
            "product": "darjeeling-first-flush",
            "sku": "TEA-DARJ-200",
            "name": "200 g pouch",
            "size_label": "200 g",
            "weight_grams": 200,
            "price_minor": 142000,
            "track_inventory": True,
            "attributes": {"replenishment_days": 55},
        },
        {
            "product": "v60-filter-papers",
            "sku": "GEAR-V60-FILTER-100",
            "name": "Pack of 100",
            "size_label": "100 filters",
            "price_minor": 35000,
            "track_inventory": True,
            "attributes": {},
        },
        {
            "product": "ember-flat-white",
            "sku": "DRINK-FW-REG",
            "name": "Regular",
            "size_label": "240 ml",
            "price_minor": 24000,
            "preparation_minutes": 9,
            "track_inventory": False,
            "attributes": {"volume_ml": 240, "espresso_shots": 2},
        },
        {
            "product": "ember-flat-white",
            "sku": "DRINK-FW-LRG",
            "name": "Large",
            "size_label": "350 ml",
            "price_minor": 29000,
            "preparation_minutes": 10,
            "track_inventory": False,
            "attributes": {"volume_ml": 350, "espresso_shots": 2},
        },
        {
            "product": "ceremonial-matcha-latte",
            "sku": "DRINK-MATCHA-REG",
            "name": "Regular",
            "size_label": "250 ml",
            "price_minor": 30000,
            "preparation_minutes": 10,
            "track_inventory": False,
            "attributes": {"volume_ml": 250},
        },
        {
            "product": "ceremonial-matcha-latte",
            "sku": "DRINK-MATCHA-LRG",
            "name": "Large",
            "size_label": "350 ml",
            "price_minor": 36000,
            "preparation_minutes": 11,
            "track_inventory": False,
            "attributes": {"volume_ml": 350},
        },
        {
            "product": "espresso-tonic",
            "sku": "DRINK-TONIC-REG",
            "name": "Regular",
            "size_label": "300 ml",
            "price_minor": 26000,
            "preparation_minutes": 7,
            "track_inventory": False,
            "attributes": {"volume_ml": 300},
        },
        {
            "product": "bengaluru-filter-coffee",
            "sku": "DRINK-FILTER-REG",
            "name": "Regular",
            "size_label": "180 ml",
            "price_minor": 16000,
            "preparation_minutes": 8,
            "track_inventory": False,
            "attributes": {"volume_ml": 180},
        },
        {
            "product": "bengaluru-filter-coffee",
            "sku": "DRINK-FILTER-LRG",
            "name": "Large",
            "size_label": "280 ml",
            "price_minor": 21000,
            "preparation_minutes": 9,
            "track_inventory": False,
            "attributes": {"volume_ml": 280},
        },
        {
            "product": "hibiscus-citrus-iced-tea",
            "sku": "DRINK-HIBISCUS-REG",
            "name": "Regular",
            "size_label": "300 ml",
            "price_minor": 21000,
            "preparation_minutes": 6,
            "track_inventory": False,
            "attributes": {"volume_ml": 300},
        },
        {
            "product": "hibiscus-citrus-iced-tea",
            "sku": "DRINK-HIBISCUS-LRG",
            "name": "Large",
            "size_label": "450 ml",
            "price_minor": 26000,
            "preparation_minutes": 6,
            "track_inventory": False,
            "attributes": {"volume_ml": 450},
        },
        {
            "product": "sparkling-kokum-cooler",
            "sku": "DRINK-KOKUM-REG",
            "name": "Regular",
            "size_label": "300 ml",
            "price_minor": 24000,
            "preparation_minutes": 5,
            "track_inventory": False,
            "attributes": {"volume_ml": 300},
        },
        {
            "product": "salted-jaggery-hot-chocolate",
            "sku": "DRINK-CHOC-REG",
            "name": "Regular",
            "size_label": "250 ml",
            "price_minor": 26000,
            "preparation_minutes": 9,
            "track_inventory": False,
            "attributes": {"volume_ml": 250},
        },
        {
            "product": "salted-jaggery-hot-chocolate",
            "sku": "DRINK-CHOC-LRG",
            "name": "Large",
            "size_label": "350 ml",
            "price_minor": 32000,
            "preparation_minutes": 10,
            "track_inventory": False,
            "attributes": {"volume_ml": 350},
        },
        {
            "product": "monsoon-decaf-coffee",
            "sku": "BEAN-DECAF-250",
            "name": "250 g",
            "size_label": "250 g",
            "weight_grams": 250,
            "price_minor": 72000,
            "track_inventory": True,
            "attributes": {"replenishment_days": 14},
        },
        {
            "product": "monsoon-decaf-coffee",
            "sku": "BEAN-DECAF-500",
            "name": "500 g",
            "size_label": "500 g",
            "weight_grams": 500,
            "price_minor": 132000,
            "track_inventory": True,
            "attributes": {"replenishment_days": 28},
        },
        {
            "product": "chamomile-citrus-tisane",
            "sku": "TEA-CHAM-075",
            "name": "75 g tin",
            "size_label": "75 g",
            "weight_grams": 75,
            "price_minor": 52000,
            "track_inventory": True,
            "attributes": {"replenishment_days": 30},
        },
        {
            "product": "chamomile-citrus-tisane",
            "sku": "TEA-CHAM-150",
            "name": "150 g refill",
            "size_label": "150 g",
            "weight_grams": 150,
            "price_minor": 92000,
            "track_inventory": True,
            "attributes": {"replenishment_days": 60},
        },
        {
            "product": "assam-breakfast-tea",
            "sku": "TEA-ASSAM-100",
            "name": "100 g pouch",
            "size_label": "100 g",
            "weight_grams": 100,
            "price_minor": 42000,
            "track_inventory": True,
            "attributes": {"replenishment_days": 30},
        },
        {
            "product": "assam-breakfast-tea",
            "sku": "TEA-ASSAM-250",
            "name": "250 g pouch",
            "size_label": "250 g",
            "weight_grams": 250,
            "price_minor": 89000,
            "track_inventory": True,
            "attributes": {"replenishment_days": 60},
        },
        {
            "product": "ceramic-v60-dripper",
            "sku": "GEAR-V60-DRIPPER-02",
            "name": "Size 02",
            "size_label": "1 dripper",
            "price_minor": 85000,
            "track_inventory": True,
            "attributes": {"material": "ceramic"},
        },
        {
            "product": "festival-reserve-coffee",
            "sku": "BEAN-FESTIVAL-250",
            "name": "250 g",
            "size_label": "250 g",
            "weight_grams": 250,
            "price_minor": 98000,
            "track_inventory": True,
            "attributes": {},
        },
    ]
    variants: dict[str, ProductVariant] = {}
    for spec in variant_specs:
        sku = spec["sku"]
        variants[sku] = _upsert(
            db,
            ProductVariant,
            {"merchant_id": merchant.id, "sku": sku},
            {
                "product_id": products[spec["product"]].id,
                "name": spec["name"],
                "price_minor": spec["price_minor"],
                "currency": "INR",
                "size_label": spec.get("size_label"),
                "weight_grams": spec.get("weight_grams"),
                "preparation_minutes": spec.get("preparation_minutes", 0),
                "track_inventory": spec["track_inventory"],
                "sellable": spec.get("sellable", True),
                "attributes": spec["attributes"],
            },
        )
    return products, variants, variant_specs


def _seed_modifiers(db: Session, products: dict[str, Product]) -> None:
    milk_options = [
        ("Dairy milk", 0, True, {"dietary": ["vegetarian"]}),
        ("Oat milk", 4000, True, {"dietary": ["vegan"]}),
        ("Almond milk", 5000, True, {"dietary": ["vegan"]}),
    ]
    sweetness_options = [
        ("Unsweetened", 0, True, {"level": 0}),
        ("Lightly sweet", 0, True, {"level": 1}),
        ("Regular sweet", 0, True, {"level": 2}),
    ]
    temperature_options = [
        ("Hot", 0, True, {"temperature": "hot"}),
        ("Iced", 0, True, {"temperature": "iced"}),
    ]
    grind_options = [
        ("Whole bean", 0, True, {"brew_method": "whole bean"}),
        ("V60", 0, True, {"brew_method": "v60"}),
        ("Espresso", 0, True, {"brew_method": "espresso"}),
        ("Aeropress", 0, True, {"brew_method": "aeropress"}),
        ("French press", 0, True, {"brew_method": "french press"}),
    ]
    group_specs = [
        ("house-cold-brew", "Milk", False, 0, 1, 1, [("No milk", 0, True, {})] + milk_options[1:]),
        ("house-cold-brew", "Sweetness", True, 1, 1, 2, sweetness_options),
        ("citrus-bloom-coffee", "Grind", True, 1, 1, 1, grind_options),
        ("masala-cloud-chai", "Milk", True, 1, 1, 1, milk_options),
        ("masala-cloud-chai", "Sweetness", True, 1, 1, 2, sweetness_options),
        ("masala-cloud-chai", "Temperature", True, 1, 1, 3, temperature_options),
        (
            "ember-flat-white",
            "Milk",
            True,
            1,
            1,
            1,
            milk_options + [("Soy milk (paused)", 4000, False, {"dietary": ["vegan"]})],
        ),
        ("ember-flat-white", "Temperature", True, 1, 1, 2, temperature_options),
        (
            "ember-flat-white",
            "Extra shot",
            False,
            0,
            1,
            3,
            [("Add one espresso shot", 6000, True, {"espresso_shots": 1})],
        ),
        ("ceremonial-matcha-latte", "Milk", True, 1, 1, 1, milk_options),
        ("ceremonial-matcha-latte", "Sweetness", True, 1, 1, 2, sweetness_options),
        ("ceremonial-matcha-latte", "Temperature", True, 1, 1, 3, temperature_options),
        ("espresso-tonic", "Sweetness", True, 1, 1, 1, sweetness_options[:2]),
        ("bengaluru-filter-coffee", "Milk", True, 1, 1, 1, milk_options),
        ("bengaluru-filter-coffee", "Sweetness", True, 1, 1, 2, sweetness_options),
        ("hibiscus-citrus-iced-tea", "Sweetness", True, 1, 1, 1, sweetness_options),
        ("sparkling-kokum-cooler", "Sweetness", True, 1, 1, 1, sweetness_options[:2]),
        ("salted-jaggery-hot-chocolate", "Milk", True, 1, 1, 1, milk_options),
        ("salted-jaggery-hot-chocolate", "Sweetness", True, 1, 1, 2, sweetness_options),
        ("monsoon-decaf-coffee", "Grind", True, 1, 1, 1, grind_options),
    ]
    for product_slug, name, required, minimum, maximum, position, options in group_specs:
        group = _upsert(
            db,
            ModifierGroup,
            {"product_id": products[product_slug].id, "name": name},
            {
                "required": required,
                "minimum_selections": minimum,
                "maximum_selections": maximum,
                "position": position,
            },
        )
        for option_position, (option_name, price, active, attributes) in enumerate(options, 1):
            _upsert(
                db,
                ModifierOption,
                {"group_id": group.id, "name": option_name},
                {
                    "price_delta_minor": price,
                    "position": option_position,
                    "active": active,
                    "attributes": attributes,
                },
            )


def _seed_inventory(
    db: Session,
    locations: dict[str, Location],
    variants: dict[str, ProductVariant],
    variant_specs: list[dict[str, Any]],
    reference_date: date,
) -> None:
    stocked_skus = [
        spec["sku"]
        for spec in variant_specs
        if spec["track_inventory"]
        and spec.get("sellable", True)
        and spec["product"] != "festival-reserve-coffee"
    ]
    overrides = {
        ("indiranagar-cafe", "BEAN-CITRUS-500"): (2, 5),
        ("indiranagar-cafe", "BEAN-DECAF-250"): (0, 4),
        ("indiranagar-cafe", "BEAN-DECAF-500"): (0, 4),
        ("indiranagar-cafe", "TEA-CHAM-150"): (0, 4),
        ("central-roastery", "TEA-CHAM-150"): (0, 6),
        ("indiranagar-cafe", "GEAR-V60-FILTER-100"): (2, 5),
        ("indiranagar-cafe", "GEAR-V60-DRIPPER-02"): (0, 4),
        ("central-roastery", "GEAR-V60-DRIPPER-02"): (6, 3),
    }
    for location_slug, location in locations.items():
        default_quantity = 24 if location_slug == "central-roastery" else 8
        default_reorder = 6 if location_slug == "central-roastery" else 4
        for sku in stocked_skus:
            quantity, reorder_point = overrides.get(
                (location_slug, sku), (default_quantity, default_reorder)
            )
            _upsert(
                db,
                InventoryItem,
                {"location_id": location.id, "variant_id": variants[sku].id},
                {
                    "on_hand_quantity": quantity,
                    "reserved_quantity": 0,
                    "reorder_point": reorder_point,
                },
                update_existing=False,
            )

    central = locations["central-roastery"]
    for sku in ("BEAN-CITRUS-250", "BEAN-CITRUS-500", "BEAN-DECAF-250", "BEAN-DECAF-500"):
        _upsert(
            db,
            InventoryBatch,
            {"location_id": central.id, "lot_code": f"DEMO-{sku}"},
            {
                "variant_id": variants[sku].id,
                "quantity": 24,
                "roasted_at": reference_date - timedelta(days=2),
                "best_before": reference_date + timedelta(days=88),
            },
            update_existing=False,
        )


def seed_database(
    *,
    session_factory: SessionFactory = SessionLocal,
    reference_date: date | None = None,
) -> None:
    seed_date = reference_date or date.today()
    with session_factory() as db, db.begin():
        merchant = _upsert(
            db,
            Merchant,
            {"slug": "ember-and-leaf"},
            {
                "name": "Ember & Leaf",
                "currency": "INR",
                "timezone": "Asia/Kolkata",
                "settings": {
                    "prices_include_tax": True,
                    "default_checkout_ttl_minutes": 10,
                    "prepared_beverage_delivery": True,
                },
            },
        )
        locations = _seed_locations(db, merchant)
        products, variants, variant_specs = _seed_products(db, merchant)
        _seed_modifiers(db, products)
        _seed_inventory(db, locations, variants, variant_specs, seed_date)
        seed_merchant_admin(db, merchant)
        seed_demo_customer(db)


if __name__ == "__main__":
    seed_database()
