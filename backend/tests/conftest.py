from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime, time, timedelta
from pathlib import Path

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.database import get_db
from app.core.security import hash_password, hash_session_token
from app.db.base import Base
from app.db.models import (
    AuthSession,
    CustomerAddress,
    InventoryItem,
    Location,
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
from app.main import app


@pytest.fixture()
def session_factory(tmp_path: Path) -> Generator[sessionmaker[Session], None, None]:
    engine = create_engine(
        f"sqlite+pysqlite:///{tmp_path / 'test.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    yield factory
    Base.metadata.drop_all(engine)
    engine.dispose()


@pytest.fixture()
def seeded(session_factory: sessionmaker[Session]) -> dict[str, object]:
    with session_factory() as db, db.begin():
        merchant = Merchant(slug="ember-and-leaf", name="Ember & Leaf", currency="INR")
        db.add(merchant)
        db.flush()
        location = Location(
            merchant_id=merchant.id,
            slug="indiranagar-cafe",
            name="Indiranagar Café",
            kind=LocationKind.CAFE,
            postal_code="560038",
            preparation_minutes=10,
        )
        db.add(location)
        db.flush()
        zone = ServiceZone(
            location_id=location.id,
            name="Express",
            fulfillment_type=FulfillmentType.LOCAL_DELIVERY,
            postal_codes=["560038"],
            minimum_order_minor=20000,
            fee_minor=4900,
            free_above_minor=100000,
            eta_min_minutes=45,
            eta_max_minutes=90,
            cutoff_time=time(20, 0),
        )
        db.add(zone)

        drink = Product(
            merchant_id=merchant.id,
            slug="cold-brew",
            name="House Cold Brew",
            description="Smooth cold brew.",
            product_type=ProductType.PREPARED_BEVERAGE,
            status=ProductStatus.ACTIVE,
        )
        beans = Product(
            merchant_id=merchant.id,
            slug="citrus-beans",
            name="Citrus Bloom Coffee",
            description="Fresh medium-roast beans.",
            product_type=ProductType.PACKAGED_COFFEE,
            status=ProductStatus.ACTIVE,
        )
        db.add_all([drink, beans])
        db.flush()
        drink_variant = ProductVariant(
            merchant_id=merchant.id,
            product_id=drink.id,
            sku="DRINK-CB-REG",
            name="Regular",
            price_minor=22000,
            track_inventory=False,
        )
        bean_variant = ProductVariant(
            merchant_id=merchant.id,
            product_id=beans.id,
            sku="BEAN-CITRUS-500",
            name="500 g",
            price_minor=119000,
            weight_grams=500,
            track_inventory=True,
        )
        db.add_all([drink_variant, bean_variant])
        db.flush()

        sweetness = ModifierGroup(
            product_id=drink.id,
            name="Sweetness",
            required=True,
            minimum_selections=1,
            maximum_selections=1,
        )
        milk = ModifierGroup(
            product_id=drink.id,
            name="Milk",
            minimum_selections=0,
            maximum_selections=1,
        )
        grind = ModifierGroup(
            product_id=beans.id,
            name="Grind",
            required=True,
            minimum_selections=1,
            maximum_selections=1,
        )
        db.add_all([sweetness, milk, grind])
        db.flush()
        unsweetened = ModifierOption(group_id=sweetness.id, name="Unsweetened", price_delta_minor=0)
        oat = ModifierOption(group_id=milk.id, name="Oat milk", price_delta_minor=4000)
        whole = ModifierOption(group_id=grind.id, name="Whole bean", price_delta_minor=0)
        db.add_all([unsweetened, oat, whole])
        inventory = InventoryItem(
            location_id=location.id,
            variant_id=bean_variant.id,
            on_hand_quantity=2,
            reserved_quantity=0,
            reorder_point=3,
        )
        db.add(inventory)
        db.flush()
        other_merchant = Merchant(
            slug="another-merchant",
            name="Another Merchant",
            currency="INR",
        )
        db.add(other_merchant)
        db.flush()
        other_product = Product(
            merchant_id=other_merchant.id,
            slug="other-coffee",
            name="Other Coffee",
            description="Not sold by Ember & Leaf.",
            product_type=ProductType.PACKAGED_COFFEE,
            status=ProductStatus.ACTIVE,
        )
        db.add(other_product)
        db.flush()
        other_variant = ProductVariant(
            merchant_id=other_merchant.id,
            product_id=other_product.id,
            sku="OTHER-COFFEE-250",
            name="250 g",
            price_minor=50000,
            track_inventory=False,
        )
        db.add(other_variant)
        db.flush()
        merchant_admin = UserAccount(
            email="owner@emberandleaf.test",
            password_hash=hash_password("merchant-password-123"),
            full_name="Mira Rao",
            role=UserRole.MERCHANT_ADMIN,
            merchant_id=merchant.id,
        )
        db.add(merchant_admin)
        db.flush()
        customer = UserAccount(
            email="customer@emberandleaf.test",
            password_hash=hash_password("customer-password-123"),
            full_name="Aarav Mehta",
            phone="+919876543210",
            role=UserRole.CUSTOMER,
        )
        db.add(customer)
        db.flush()
        customer_token = "test-customer-session-token"
        db.add(
            AuthSession(
                user_id=customer.id,
                token_sha256=hash_session_token(customer_token),
                expires_at=datetime.now(UTC) + timedelta(days=1),
            )
        )
        address = CustomerAddress(
            user_id=customer.id,
            label="Home",
            recipient_name=customer.full_name,
            phone=customer.phone,
            line_one="12, 100 Feet Road",
            city="Bengaluru",
            region="Karnataka",
            postal_code="560038",
            country_code="IN",
            is_default=True,
        )
        db.add(address)
        db.flush()
        return {
            "merchant_id": merchant.id,
            "location_id": location.id,
            "drink_variant_id": drink_variant.id,
            "bean_variant_id": bean_variant.id,
            "unsweetened_id": unsweetened.id,
            "oat_id": oat.id,
            "whole_id": whole.id,
            "inventory_id": inventory.id,
            "other_variant_id": other_variant.id,
            "merchant_admin_id": merchant_admin.id,
            "merchant_admin_email": merchant_admin.email,
            "merchant_admin_password": "merchant-password-123",
            "customer_id": customer.id,
            "customer_token": customer_token,
            "customer_address_id": address.id,
        }


@pytest.fixture()
def anyio_backend() -> tuple[str, dict[str, bool]]:
    return "asyncio", {"use_uvloop": True}


@pytest.fixture()
async def client(
    session_factory: sessionmaker[Session], seeded: dict[str, object]
) -> AsyncGenerator[httpx.AsyncClient, None]:
    def override_db() -> Generator[Session, None, None]:
        with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_db
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as test_client:
        yield test_client
    app.dependency_overrides.clear()
