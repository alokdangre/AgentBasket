import uuid
from datetime import date

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.agents.tools import AgentToolbox
from app.core.config import get_settings
from app.core.errors import DomainError, NotFoundError
from app.core.security import verify_password
from app.db.models import (
    Ap2ConsentChallenge,
    AuthSession,
    Cart,
    Checkout,
    CustomerAddress,
    InventoryBatch,
    InventoryItem,
    Location,
    LocationHours,
    Merchant,
    ModifierGroup,
    ModifierOption,
    Order,
    Payment,
    Product,
    ProductVariant,
    ServiceZone,
    UserAccount,
)
from app.db.seed import seed_database
from app.domain.enums import FulfillmentType, ProductType, UserRole
from app.schemas.checkout import CheckoutCreate, CheckoutItemCreate
from app.services.catalog import CatalogService
from app.services.checkout import CheckoutService

SEEDED_MODELS = (
    Merchant,
    Location,
    LocationHours,
    ServiceZone,
    Product,
    ProductVariant,
    ModifierGroup,
    ModifierOption,
    InventoryItem,
    InventoryBatch,
    UserAccount,
    CustomerAddress,
)


@pytest.fixture()
def demo_seed_environment(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("DEMO_CUSTOMER_EMAIL", "shopper@emberandleaf.test")
    monkeypatch.setenv("DEMO_CUSTOMER_PASSWORD", "demo-customer-password-123")
    monkeypatch.setenv("DEMO_CUSTOMER_NAME", "Aarav Mehta")
    monkeypatch.setenv("DEMO_CUSTOMER_PHONE", "+919876543210")
    monkeypatch.delenv("MERCHANT_ADMIN_EMAIL", raising=False)
    monkeypatch.delenv("MERCHANT_ADMIN_PASSWORD", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _counts(db: Session) -> dict[str, int]:
    return {
        model.__tablename__: db.scalar(select(func.count()).select_from(model)) or 0
        for model in SEEDED_MODELS
    }


def test_seed_is_incremental_idempotent_imageless_and_transaction_free(
    session_factory: sessionmaker[Session],
    demo_seed_environment: None,
) -> None:
    first_reference_date = date(2026, 8, 31)
    seed_database(session_factory=session_factory, reference_date=first_reference_date)

    with session_factory() as db:
        first_counts = _counts(db)
        assert first_counts == {
            "merchants": 1,
            "locations": 2,
            "location_hours": 14,
            "service_zones": 3,
            "products": 13,
            "product_variants": 23,
            "modifier_groups": 14,
            "modifier_options": 41,
            "inventory_items": 24,
            "inventory_batches": 4,
            "user_accounts": 1,
            "customer_addresses": 3,
        }
        assert all(not product.image_urls for product in db.scalars(select(Product)))
        lots = list(db.scalars(select(InventoryBatch).order_by(InventoryBatch.lot_code)))
        assert all(lot.lot_code.startswith("DEMO-") for lot in lots)
        assert {lot.roasted_at for lot in lots} == {date(2026, 8, 29)}
        assert {lot.best_before for lot in lots} == {date(2026, 11, 27)}

        customer = db.scalar(select(UserAccount))
        assert customer is not None
        assert customer.role == UserRole.CUSTOMER
        assert verify_password("demo-customer-password-123", customer.password_hash)
        addresses = list(
            db.scalars(
                select(CustomerAddress)
                .where(CustomerAddress.user_id == customer.id)
                .order_by(CustomerAddress.label)
            )
        )
        assert {(address.label, address.postal_code) for address in addresses} == {
            ("Home", "560038"),
            ("Office", "560001"),
            ("Outside delivery area", "400001"),
        }
        assert [address.label for address in addresses if address.is_default] == ["Home"]

        for transactional_model in (
            AuthSession,
            Cart,
            Checkout,
            Ap2ConsentChallenge,
            Order,
            Payment,
        ):
            assert db.scalar(select(func.count()).select_from(transactional_model)) == 0

    seed_database(session_factory=session_factory, reference_date=date(2027, 1, 1))
    with session_factory() as db:
        assert _counts(db) == first_counts
        lots = list(db.scalars(select(InventoryBatch)))
        assert {lot.roasted_at for lot in lots} == {date(2026, 8, 29)}


def test_seed_backfills_an_existing_merchant_without_duplicates(
    session_factory: sessionmaker[Session],
    demo_seed_environment: None,
) -> None:
    with session_factory() as db, db.begin():
        db.add(Merchant(slug="ember-and-leaf", name="Legacy Demo", currency="INR"))

    seed_database(session_factory=session_factory, reference_date=date(2026, 8, 31))
    seed_database(session_factory=session_factory, reference_date=date(2026, 8, 31))

    with session_factory() as db:
        merchant = db.scalar(select(Merchant).where(Merchant.slug == "ember-and-leaf"))
        assert merchant is not None
        assert merchant.name == "Ember & Leaf"
        assert db.scalar(select(func.count()).select_from(Merchant)) == 1
        assert db.scalar(select(func.count()).select_from(Location)) == 2
        assert db.scalar(select(func.count()).select_from(Product)) == 13
        assert db.scalar(select(func.count()).select_from(ProductVariant)) == 23
        assert len(set(db.scalars(select(Product.slug)))) == 13
        assert len(set(db.scalars(select(ProductVariant.sku)))) == 23


def test_catalog_uses_fulfillment_location_and_filters_unavailable_data(
    session_factory: sessionmaker[Session],
    demo_seed_environment: None,
) -> None:
    seed_database(session_factory=session_factory, reference_date=date(2026, 8, 31))

    with session_factory() as db:
        local = CatalogService(db).search("ember-and-leaf", postal_code="560038")
        local_location = db.get(Location, local.location_id)
        assert local_location is not None
        assert local_location.slug == "indiranagar-cafe"
        local_products = {product.slug: product for product in local.products}
        assert "festival-reserve-coffee" not in local_products
        assert "monsoon-decaf-coffee" not in local_products
        assert "ceramic-v60-dripper" not in local_products
        assert {variant.sku for variant in local_products["citrus-bloom-coffee"].variants} == {
            "BEAN-CITRUS-250",
            "BEAN-CITRUS-500",
        }
        assert {variant.sku for variant in local_products["chamomile-citrus-tisane"].variants} == {
            "TEA-CHAM-075"
        }

        flat_white = local_products["ember-flat-white"]
        flat_milk = next(group for group in flat_white.modifier_groups if group.name == "Milk")
        assert "Soy milk (paused)" not in {option.name for option in flat_milk.options}

        shipping = CatalogService(db).search(
            "ember-and-leaf",
            postal_code="560038",
            fulfillment_type=FulfillmentType.SHIPPING,
        )
        shipping_location = db.get(Location, shipping.location_id)
        assert shipping_location is not None
        assert shipping_location.slug == "central-roastery"
        shipping_products = {product.slug: product for product in shipping.products}
        assert "monsoon-decaf-coffee" in shipping_products
        assert "ceramic-v60-dripper" in shipping_products
        assert all(
            product.product_type != ProductType.PREPARED_BEVERAGE
            for product in shipping_products.values()
        )
        assert {
            variant.sku for variant in shipping_products["chamomile-citrus-tisane"].variants
        } == {"TEA-CHAM-075"}

        with pytest.raises(NotFoundError) as unserviceable:
            CatalogService(db).search("ember-and-leaf", postal_code="400001")
        assert unserviceable.value.code == "address_not_serviceable"
        assert unserviceable.value.status_code == 404


def test_checkout_rejects_shipping_a_prepared_beverage(
    session_factory: sessionmaker[Session],
    demo_seed_environment: None,
) -> None:
    seed_database(session_factory=session_factory, reference_date=date(2026, 8, 31))

    with session_factory() as lookup:
        customer = lookup.scalar(
            select(UserAccount).where(UserAccount.email == "shopper@emberandleaf.test")
        )
        variant = lookup.scalar(select(ProductVariant).where(ProductVariant.sku == "DRINK-FW-REG"))
        assert customer is not None
        assert variant is not None
        milk_id = lookup.scalar(
            select(ModifierOption.id)
            .join(ModifierGroup, ModifierOption.group_id == ModifierGroup.id)
            .where(
                ModifierGroup.product_id == variant.product_id,
                ModifierGroup.name == "Milk",
                ModifierOption.name == "Dairy milk",
            )
        )
        hot_id = lookup.scalar(
            select(ModifierOption.id)
            .join(ModifierGroup, ModifierOption.group_id == ModifierGroup.id)
            .where(
                ModifierGroup.product_id == variant.product_id,
                ModifierGroup.name == "Temperature",
                ModifierOption.name == "Hot",
            )
        )
        assert milk_id is not None
        assert hot_id is not None

    payload = CheckoutCreate(
        merchant_slug="ember-and-leaf",
        fulfillment_type=FulfillmentType.SHIPPING,
        postal_code="560038",
        items=[
            CheckoutItemCreate(
                variant_id=variant.id,
                quantity=1,
                modifier_option_ids=[milk_id, hot_id],
            )
        ],
    )
    with session_factory() as db, pytest.raises(DomainError) as unsupported:
        CheckoutService(db).create(payload, "seed-shipping-prepared-1", customer)
    assert unsupported.value.code == "fulfillment_not_supported"
    assert unsupported.value.status_code == 422
    with session_factory() as db:
        assert db.scalar(select(func.count()).select_from(Checkout)) == 0


def test_seeded_catalog_supports_deterministic_recommendation_dimensions(
    session_factory: sessionmaker[Session],
    demo_seed_environment: None,
) -> None:
    seed_database(session_factory=session_factory, reference_date=date(2026, 8, 31))

    with session_factory() as db:
        merchant = db.scalar(select(Merchant).where(Merchant.slug == "ember-and-leaf"))
        customer = db.scalar(
            select(UserAccount).where(UserAccount.email == "shopper@emberandleaf.test")
        )
        assert merchant is not None
        assert customer is not None
        toolbox = AgentToolbox(
            db=db,
            customer=customer,
            merchant_id=merchant.id,
            merchant_slug=merchant.slug,
            conversation_id=uuid.uuid4(),
            run_id=uuid.uuid4(),
        )

        cases = [
            ("decaf cocoa evening", 80000, "monsoon-decaf-coffee"),
            ("caffeine free floral evening", 60000, "chamomile-citrus-tisane"),
            ("vegan iced refreshing citrus", 30000, "espresso-tonic"),
            ("v60 pour over accessory", 100000, "ceramic-v60-dripper"),
        ]
        for preferences, budget_minor, expected_slug in cases:
            result = toolbox._recommend_products(preferences, budget_minor, "")
            assert result["status"] == "success"
            assert result["products"][0]["slug"] == expected_slug


def test_production_seed_never_creates_demo_customer(
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("DEMO_CUSTOMER_EMAIL", "must-not-exist@example.com")
    monkeypatch.setenv("DEMO_CUSTOMER_PASSWORD", "demo-customer-password-123")
    monkeypatch.delenv("MERCHANT_ADMIN_EMAIL", raising=False)
    monkeypatch.delenv("MERCHANT_ADMIN_PASSWORD", raising=False)
    get_settings.cache_clear()

    seed_database(session_factory=session_factory, reference_date=date(2026, 8, 31))

    with session_factory() as db:
        assert db.scalar(select(func.count()).select_from(UserAccount)) == 0
        assert db.scalar(select(func.count()).select_from(Product)) == 13
    get_settings.cache_clear()
