from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, time
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    String,
    Text,
    Time,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.domain.enums import (
    CartStatus,
    CheckoutStatus,
    FulfillmentType,
    LocationKind,
    MerchantStatus,
    OrderStatus,
    PaymentStatus,
    ProductStatus,
    ProductType,
    PurchaseIntentStatus,
    ReservationStatus,
    ScheduledRunStatus,
    UserRole,
)


def utc_now() -> datetime:
    return datetime.now(UTC)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )


class Merchant(TimestampMixin, Base):
    __tablename__ = "merchants"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    slug: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(160))
    currency: Mapped[str] = mapped_column(String(3), default="INR")
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Kolkata")
    status: Mapped[MerchantStatus] = mapped_column(
        Enum(MerchantStatus, native_enum=False), default=MerchantStatus.ACTIVE
    )
    settings: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    locations: Mapped[list[Location]] = relationship(back_populates="merchant")
    products: Mapped[list[Product]] = relationship(back_populates="merchant")


class UserAccount(TimestampMixin, Base):
    __tablename__ = "user_accounts"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(512))
    full_name: Mapped[str] = mapped_column(String(160))
    phone: Mapped[str | None] = mapped_column(String(32))
    role: Mapped[UserRole] = mapped_column(
        Enum(UserRole, native_enum=False), default=UserRole.CUSTOMER, index=True
    )
    merchant_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("merchants.id", ondelete="CASCADE"), index=True
    )
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    sessions: Mapped[list[AuthSession]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    addresses: Mapped[list[CustomerAddress]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    carts: Mapped[list[Cart]] = relationship(back_populates="user", cascade="all, delete-orphan")
    agent_conversations: Mapped[list[AgentConversation]] = relationship(
        back_populates="customer", cascade="all, delete-orphan"
    )


class AuthSession(TimestampMixin, Base):
    __tablename__ = "auth_sessions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("user_accounts.id", ondelete="CASCADE"), index=True
    )
    token_sha256: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)

    user: Mapped[UserAccount] = relationship(back_populates="sessions")


class CustomerAddress(TimestampMixin, Base):
    __tablename__ = "customer_addresses"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("user_accounts.id", ondelete="CASCADE"), index=True
    )
    label: Mapped[str] = mapped_column(String(80))
    recipient_name: Mapped[str] = mapped_column(String(160))
    phone: Mapped[str] = mapped_column(String(32))
    line_one: Mapped[str] = mapped_column(String(200))
    line_two: Mapped[str | None] = mapped_column(String(200))
    landmark: Mapped[str | None] = mapped_column(String(160))
    city: Mapped[str] = mapped_column(String(120))
    region: Mapped[str] = mapped_column(String(120))
    postal_code: Mapped[str] = mapped_column(String(20), index=True)
    country_code: Mapped[str] = mapped_column(String(2), default="IN")
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, index=True)

    user: Mapped[UserAccount] = relationship(back_populates="addresses")


class WebAuthnCredential(TimestampMixin, Base):
    __tablename__ = "webauthn_credentials"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("user_accounts.id", ondelete="CASCADE"), index=True
    )
    credential_id: Mapped[str] = mapped_column(String(512), unique=True, index=True)
    public_key: Mapped[bytes] = mapped_column(LargeBinary)
    sign_count: Mapped[int] = mapped_column(Integer, default=0)
    transports: Mapped[list[str]] = mapped_column(JSON, default=list)
    device_type: Mapped[str] = mapped_column(String(32))
    backed_up: Mapped[bool] = mapped_column(Boolean, default=False)
    label: Mapped[str] = mapped_column(String(80), default="Passkey")
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class WebAuthnCeremony(TimestampMixin, Base):
    __tablename__ = "webauthn_ceremonies"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("user_accounts.id", ondelete="CASCADE"), index=True
    )
    purpose: Mapped[str] = mapped_column(String(32), index=True)
    challenge: Mapped[str] = mapped_column(String(256), unique=True)
    status: Mapped[str] = mapped_column(String(24), default="pending", index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PaymentInstrument(TimestampMixin, Base):
    __tablename__ = "payment_instruments"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "provider", "instrument_type", name="uq_payment_instrument_provider_type"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("user_accounts.id", ondelete="CASCADE"), index=True
    )
    provider: Mapped[str] = mapped_column(String(40), index=True)
    instrument_type: Mapped[str] = mapped_column(String(120))
    alias: Mapped[str] = mapped_column(String(160))
    provider_customer_id: Mapped[str | None] = mapped_column(String(120))
    provider_token_reference: Mapped[str | None] = mapped_column(String(240))
    network: Mapped[str | None] = mapped_column(String(40))
    last4: Mapped[str | None] = mapped_column(String(4))
    status: Mapped[str] = mapped_column(String(24), default="active", index=True)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    instrument_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class Location(TimestampMixin, Base):
    __tablename__ = "locations"
    __table_args__ = (UniqueConstraint("merchant_id", "slug", name="uq_location_merchant_slug"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    merchant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("merchants.id", ondelete="CASCADE"), index=True
    )
    slug: Mapped[str] = mapped_column(String(80))
    name: Mapped[str] = mapped_column(String(160))
    kind: Mapped[LocationKind] = mapped_column(Enum(LocationKind, native_enum=False))
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Kolkata")
    latitude: Mapped[Decimal | None] = mapped_column(Numeric(9, 6))
    longitude: Mapped[Decimal | None] = mapped_column(Numeric(9, 6))
    address: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    postal_code: Mapped[str] = mapped_column(String(20), index=True)
    preparation_minutes: Mapped[int] = mapped_column(Integer, default=15)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)

    merchant: Mapped[Merchant] = relationship(back_populates="locations")
    hours: Mapped[list[LocationHours]] = relationship(
        back_populates="location", cascade="all, delete-orphan"
    )
    service_zones: Mapped[list[ServiceZone]] = relationship(
        back_populates="location", cascade="all, delete-orphan"
    )


class LocationHours(Base):
    __tablename__ = "location_hours"
    __table_args__ = (
        UniqueConstraint("location_id", "weekday", name="uq_location_hours_weekday"),
        CheckConstraint("weekday >= 0 AND weekday <= 6", name="ck_location_hours_weekday"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    location_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("locations.id", ondelete="CASCADE"), index=True
    )
    weekday: Mapped[int] = mapped_column(Integer)
    opens_at: Mapped[time | None] = mapped_column(Time)
    closes_at: Mapped[time | None] = mapped_column(Time)
    closed: Mapped[bool] = mapped_column(Boolean, default=False)

    location: Mapped[Location] = relationship(back_populates="hours")


class ServiceZone(TimestampMixin, Base):
    __tablename__ = "service_zones"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    location_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("locations.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(120))
    fulfillment_type: Mapped[FulfillmentType] = mapped_column(
        Enum(FulfillmentType, native_enum=False)
    )
    postal_codes: Mapped[list[str]] = mapped_column(JSON, default=list)
    radius_km: Mapped[Decimal | None] = mapped_column(Numeric(6, 2))
    minimum_order_minor: Mapped[int] = mapped_column(Integer, default=0)
    fee_minor: Mapped[int] = mapped_column(Integer, default=0)
    free_above_minor: Mapped[int | None] = mapped_column(Integer)
    eta_min_minutes: Mapped[int] = mapped_column(Integer)
    eta_max_minutes: Mapped[int] = mapped_column(Integer)
    cutoff_time: Mapped[time | None] = mapped_column(Time)
    active: Mapped[bool] = mapped_column(Boolean, default=True)

    location: Mapped[Location] = relationship(back_populates="service_zones")


class Product(TimestampMixin, Base):
    __tablename__ = "products"
    __table_args__ = (UniqueConstraint("merchant_id", "slug", name="uq_product_merchant_slug"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    merchant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("merchants.id", ondelete="CASCADE"), index=True
    )
    slug: Mapped[str] = mapped_column(String(120))
    name: Mapped[str] = mapped_column(String(180), index=True)
    description: Mapped[str] = mapped_column(Text)
    product_type: Mapped[ProductType] = mapped_column(Enum(ProductType, native_enum=False))
    status: Mapped[ProductStatus] = mapped_column(
        Enum(ProductStatus, native_enum=False), default=ProductStatus.DRAFT, index=True
    )
    attributes: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    image_urls: Mapped[list[str]] = mapped_column(JSON, default=list)

    merchant: Mapped[Merchant] = relationship(back_populates="products")
    variants: Mapped[list[ProductVariant]] = relationship(
        back_populates="product", cascade="all, delete-orphan", order_by="ProductVariant.name"
    )
    modifier_groups: Mapped[list[ModifierGroup]] = relationship(
        back_populates="product", cascade="all, delete-orphan", order_by="ModifierGroup.position"
    )


class ProductVariant(TimestampMixin, Base):
    __tablename__ = "product_variants"
    __table_args__ = (
        UniqueConstraint("merchant_id", "sku", name="uq_variant_merchant_sku"),
        CheckConstraint("price_minor >= 0", name="ck_variant_price_nonnegative"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    merchant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("merchants.id", ondelete="CASCADE"), index=True
    )
    product_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE"), index=True
    )
    sku: Mapped[str] = mapped_column(String(80))
    name: Mapped[str] = mapped_column(String(160))
    price_minor: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(3), default="INR")
    size_label: Mapped[str | None] = mapped_column(String(80))
    weight_grams: Mapped[int | None] = mapped_column(Integer)
    preparation_minutes: Mapped[int] = mapped_column(Integer, default=0)
    track_inventory: Mapped[bool] = mapped_column(Boolean, default=True)
    sellable: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    attributes: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    product: Mapped[Product] = relationship(back_populates="variants")


class ModifierGroup(TimestampMixin, Base):
    __tablename__ = "modifier_groups"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    product_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(120))
    required: Mapped[bool] = mapped_column(Boolean, default=False)
    minimum_selections: Mapped[int] = mapped_column(Integer, default=0)
    maximum_selections: Mapped[int] = mapped_column(Integer, default=1)
    position: Mapped[int] = mapped_column(Integer, default=0)

    product: Mapped[Product] = relationship(back_populates="modifier_groups")
    options: Mapped[list[ModifierOption]] = relationship(
        back_populates="group", cascade="all, delete-orphan", order_by="ModifierOption.position"
    )


class ModifierOption(TimestampMixin, Base):
    __tablename__ = "modifier_options"
    __table_args__ = (
        CheckConstraint("price_delta_minor >= 0", name="ck_modifier_price_nonnegative"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    group_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("modifier_groups.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(120))
    price_delta_minor: Mapped[int] = mapped_column(Integer, default=0)
    position: Mapped[int] = mapped_column(Integer, default=0)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    attributes: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    group: Mapped[ModifierGroup] = relationship(back_populates="options")


class InventoryItem(TimestampMixin, Base):
    __tablename__ = "inventory_items"
    __table_args__ = (
        UniqueConstraint("location_id", "variant_id", name="uq_inventory_location_variant"),
        CheckConstraint("on_hand_quantity >= 0", name="ck_inventory_on_hand_nonnegative"),
        CheckConstraint("reserved_quantity >= 0", name="ck_inventory_reserved_nonnegative"),
        CheckConstraint(
            "reserved_quantity <= on_hand_quantity", name="ck_inventory_reserved_within_on_hand"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    location_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("locations.id", ondelete="CASCADE"), index=True
    )
    variant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("product_variants.id", ondelete="CASCADE"), index=True
    )
    on_hand_quantity: Mapped[int] = mapped_column(Integer, default=0)
    reserved_quantity: Mapped[int] = mapped_column(Integer, default=0)
    reorder_point: Mapped[int] = mapped_column(Integer, default=0)


class InventoryBatch(TimestampMixin, Base):
    __tablename__ = "inventory_batches"
    __table_args__ = (
        UniqueConstraint("location_id", "lot_code", name="uq_inventory_batch_location_lot"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    location_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("locations.id", ondelete="CASCADE"), index=True
    )
    variant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("product_variants.id", ondelete="CASCADE"), index=True
    )
    lot_code: Mapped[str] = mapped_column(String(80))
    quantity: Mapped[int] = mapped_column(Integer)
    roasted_at: Mapped[date | None] = mapped_column(Date)
    best_before: Mapped[date | None] = mapped_column(Date)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class Cart(TimestampMixin, Base):
    __tablename__ = "carts"
    __table_args__ = (UniqueConstraint("user_id", "merchant_id", name="uq_cart_user_merchant"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("user_accounts.id", ondelete="CASCADE"), index=True
    )
    merchant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("merchants.id", ondelete="CASCADE"), index=True
    )
    status: Mapped[CartStatus] = mapped_column(
        Enum(CartStatus, native_enum=False), default=CartStatus.ACTIVE, index=True
    )
    currency: Mapped[str] = mapped_column(String(3), default="INR")

    user: Mapped[UserAccount] = relationship(back_populates="carts")
    items: Mapped[list[CartItem]] = relationship(
        back_populates="cart", cascade="all, delete-orphan", order_by="CartItem.created_at"
    )


class CartItem(TimestampMixin, Base):
    __tablename__ = "cart_items"
    __table_args__ = (
        UniqueConstraint(
            "cart_id", "variant_id", "modifier_signature", name="uq_cart_item_configuration"
        ),
        CheckConstraint("quantity > 0", name="ck_cart_item_quantity_positive"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    cart_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("carts.id", ondelete="CASCADE"), index=True
    )
    variant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("product_variants.id", ondelete="RESTRICT"), index=True
    )
    quantity: Mapped[int] = mapped_column(Integer)
    modifier_option_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    modifier_signature: Mapped[str] = mapped_column(String(64))

    cart: Mapped[Cart] = relationship(back_populates="items")


class Checkout(TimestampMixin, Base):
    __tablename__ = "checkouts"
    __table_args__ = (
        UniqueConstraint(
            "merchant_id",
            "customer_id",
            "idempotency_key",
            name="uq_checkout_customer_idempotency",
        ),
        CheckConstraint("subtotal_minor >= 0", name="ck_checkout_subtotal_nonnegative"),
        CheckConstraint("delivery_minor >= 0", name="ck_checkout_delivery_nonnegative"),
        CheckConstraint("discount_minor >= 0", name="ck_checkout_discount_nonnegative"),
        CheckConstraint("total_minor >= 0", name="ck_checkout_total_nonnegative"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    merchant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("merchants.id", ondelete="RESTRICT"), index=True
    )
    customer_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("user_accounts.id", ondelete="SET NULL"), index=True
    )
    location_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("locations.id", ondelete="RESTRICT"), index=True
    )
    status: Mapped[CheckoutStatus] = mapped_column(
        Enum(CheckoutStatus, native_enum=False), default=CheckoutStatus.OPEN, index=True
    )
    currency: Mapped[str] = mapped_column(String(3))
    idempotency_key: Mapped[str] = mapped_column(String(128))
    request_hash: Mapped[str] = mapped_column(String(64))
    fulfillment_type: Mapped[FulfillmentType] = mapped_column(
        Enum(FulfillmentType, native_enum=False)
    )
    postal_code: Mapped[str | None] = mapped_column(String(20))
    delivery_address: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    subtotal_minor: Mapped[int] = mapped_column(Integer, default=0)
    delivery_minor: Mapped[int] = mapped_column(Integer, default=0)
    discount_minor: Mapped[int] = mapped_column(Integer, default=0)
    tax_minor: Mapped[int] = mapped_column(Integer, default=0)
    tax_included: Mapped[bool] = mapped_column(Boolean, default=True)
    total_minor: Mapped[int] = mapped_column(Integer, default=0)
    quote_version: Mapped[int] = mapped_column(Integer, default=1)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    source: Mapped[str] = mapped_column(String(24), default="storefront", index=True)
    agent_conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("agent_conversations.id", ondelete="SET NULL"), index=True
    )
    agent_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="SET NULL"), index=True
    )

    lines: Mapped[list[CheckoutLineItem]] = relationship(
        back_populates="checkout",
        cascade="all, delete-orphan",
        order_by="CheckoutLineItem.created_at",
    )
    fulfillment_options: Mapped[list[CheckoutFulfillmentOption]] = relationship(
        back_populates="checkout", cascade="all, delete-orphan"
    )


class UcpCheckoutSession(TimestampMixin, Base):
    """Server-side state for a UCP checkout that must continue on the trusted UI."""

    __tablename__ = "ucp_checkout_sessions"
    __table_args__ = (
        UniqueConstraint(
            "merchant_id",
            "agent_profile_url",
            "idempotency_key",
            name="uq_ucp_checkout_agent_idempotency",
        ),
        CheckConstraint("subtotal_minor >= 0", name="ck_ucp_checkout_subtotal_nonnegative"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    merchant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("merchants.id", ondelete="CASCADE"), index=True
    )
    agent_profile_url: Mapped[str] = mapped_column(String(1000))
    protocol_version: Mapped[str] = mapped_column(String(32))
    idempotency_key: Mapped[str] = mapped_column(String(128))
    request_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), default="requires_escalation", index=True)
    currency: Mapped[str] = mapped_column(String(3))
    line_items: Mapped[list[dict[str, Any]]] = mapped_column(JSON)
    subtotal_minor: Mapped[int] = mapped_column(Integer, default=0)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    claimed_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("user_accounts.id", ondelete="SET NULL"), index=True
    )
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    claimed_result: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    canceled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CheckoutLineItem(TimestampMixin, Base):
    __tablename__ = "checkout_line_items"
    __table_args__ = (
        CheckConstraint("quantity > 0", name="ck_checkout_line_quantity_positive"),
        CheckConstraint("unit_price_minor >= 0", name="ck_checkout_line_unit_price_nonnegative"),
        CheckConstraint("modifier_total_minor >= 0", name="ck_checkout_line_modifier_nonnegative"),
        CheckConstraint("line_total_minor >= 0", name="ck_checkout_line_total_nonnegative"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    checkout_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("checkouts.id", ondelete="CASCADE"), index=True
    )
    variant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("product_variants.id", ondelete="RESTRICT"), index=True
    )
    quantity: Mapped[int] = mapped_column(Integer)
    unit_price_minor: Mapped[int] = mapped_column(Integer)
    modifier_total_minor: Mapped[int] = mapped_column(Integer, default=0)
    line_total_minor: Mapped[int] = mapped_column(Integer)
    product_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON)

    checkout: Mapped[Checkout] = relationship(back_populates="lines")
    modifiers: Mapped[list[CheckoutLineModifier]] = relationship(
        back_populates="line_item", cascade="all, delete-orphan"
    )
    reservations: Mapped[list[InventoryReservation]] = relationship(
        back_populates="line_item", cascade="all, delete-orphan"
    )


class CheckoutLineModifier(Base):
    __tablename__ = "checkout_line_modifiers"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    line_item_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("checkout_line_items.id", ondelete="CASCADE"), index=True
    )
    modifier_option_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("modifier_options.id", ondelete="RESTRICT")
    )
    name: Mapped[str] = mapped_column(String(120))
    price_delta_minor: Mapped[int] = mapped_column(Integer)

    line_item: Mapped[CheckoutLineItem] = relationship(back_populates="modifiers")


class CheckoutFulfillmentOption(Base):
    __tablename__ = "checkout_fulfillment_options"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    checkout_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("checkouts.id", ondelete="CASCADE"), index=True
    )
    service_zone_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("service_zones.id", ondelete="SET NULL")
    )
    fulfillment_type: Mapped[FulfillmentType] = mapped_column(
        Enum(FulfillmentType, native_enum=False)
    )
    title: Mapped[str] = mapped_column(String(120))
    fee_minor: Mapped[int] = mapped_column(Integer)
    eta_min_minutes: Mapped[int] = mapped_column(Integer)
    eta_max_minutes: Mapped[int] = mapped_column(Integer)
    selected: Mapped[bool] = mapped_column(Boolean, default=False)

    checkout: Mapped[Checkout] = relationship(back_populates="fulfillment_options")


class InventoryReservation(TimestampMixin, Base):
    __tablename__ = "inventory_reservations"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    checkout_line_item_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("checkout_line_items.id", ondelete="CASCADE"), index=True
    )
    inventory_item_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("inventory_items.id", ondelete="RESTRICT"), index=True
    )
    quantity: Mapped[int] = mapped_column(Integer)
    status: Mapped[ReservationStatus] = mapped_column(
        Enum(ReservationStatus, native_enum=False), default=ReservationStatus.ACTIVE, index=True
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)

    line_item: Mapped[CheckoutLineItem] = relationship(back_populates="reservations")


class CheckoutApproval(Base):
    __tablename__ = "checkout_approvals"
    __table_args__ = (
        UniqueConstraint("checkout_id", name="uq_checkout_approval_checkout"),
        CheckConstraint("approved_total_minor >= 0", name="ck_checkout_approval_total_nonnegative"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    checkout_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("checkouts.id", ondelete="RESTRICT"), index=True
    )
    customer_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("user_accounts.id", ondelete="RESTRICT"), index=True
    )
    quote_version: Mapped[int] = mapped_column(Integer)
    approved_total_minor: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(3))
    evidence_sha256: Mapped[str] = mapped_column(String(64), unique=True)
    approved_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, index=True
    )


class Ap2TrustedIssuer(TimestampMixin, Base):
    __tablename__ = "ap2_trusted_issuers"
    __table_args__ = (UniqueConstraint("issuer", "key_id", name="uq_ap2_trusted_issuer_key"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    issuer: Mapped[str] = mapped_column(String(240), index=True)
    key_id: Mapped[str] = mapped_column(String(120))
    role: Mapped[str] = mapped_column(String(40))
    algorithm: Mapped[str] = mapped_column(String(16), default="ES256")
    public_key_pem: Mapped[str] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)


class Ap2ConsentChallenge(TimestampMixin, Base):
    __tablename__ = "ap2_consent_challenges"
    __table_args__ = (
        UniqueConstraint("customer_id", "idempotency_key", name="uq_ap2_challenge_idempotency"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    checkout_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("checkouts.id", ondelete="RESTRICT"), index=True
    )
    customer_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("user_accounts.id", ondelete="RESTRICT"), index=True
    )
    approval_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("checkout_approvals.id", ondelete="SET NULL"), unique=True
    )
    payment_instrument_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("payment_instruments.id", ondelete="RESTRICT"), index=True
    )
    idempotency_key: Mapped[str] = mapped_column(String(128))
    request_sha256: Mapped[str] = mapped_column(String(64))
    nonce: Mapped[str] = mapped_column(String(128), unique=True)
    checkout_jwt: Mapped[str] = mapped_column(Text)
    checkout_hash: Mapped[str] = mapped_column(String(64), index=True)
    display_sha256: Mapped[str] = mapped_column(String(64))
    display_payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    webauthn_challenge: Mapped[str | None] = mapped_column(String(256), unique=True)
    status: Mapped[str] = mapped_column(String(24), default="pending", index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approval_idempotency_key: Mapped[str | None] = mapped_column(String(128))
    approval_request_sha256: Mapped[str | None] = mapped_column(String(64))


class Ap2Mandate(Base):
    __tablename__ = "ap2_mandates"
    __table_args__ = (UniqueConstraint("challenge_id", "mandate_type", name="uq_ap2_mandate_type"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    challenge_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("ap2_consent_challenges.id", ondelete="CASCADE"), index=True
    )
    checkout_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("checkouts.id", ondelete="RESTRICT"), index=True
    )
    customer_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("user_accounts.id", ondelete="RESTRICT"), index=True
    )
    mandate_type: Mapped[str] = mapped_column(String(24), index=True)
    vct: Mapped[str] = mapped_column(String(64))
    issuer: Mapped[str] = mapped_column(String(240))
    key_id: Mapped[str] = mapped_column(String(120))
    signed_jwt: Mapped[str] = mapped_column(Text)
    payload_sha256: Mapped[str] = mapped_column(String(64), unique=True)
    checkout_hash: Mapped[str] = mapped_column(String(64), index=True)
    verification_status: Mapped[str] = mapped_column(String(24), index=True)
    rejection_code: Mapped[str | None] = mapped_column(String(120))
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, index=True
    )


class PaymentCredentialGrant(TimestampMixin, Base):
    __tablename__ = "payment_credential_grants"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("user_accounts.id", ondelete="RESTRICT"), index=True
    )
    checkout_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("checkouts.id", ondelete="RESTRICT"), unique=True, index=True
    )
    payment_mandate_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("ap2_mandates.id", ondelete="RESTRICT"), unique=True
    )
    payment_instrument_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("payment_instruments.id", ondelete="RESTRICT"), index=True
    )
    token_sha256: Mapped[str] = mapped_column(String(64), unique=True)
    signed_credential: Mapped[str] = mapped_column(Text)
    credential_kind: Mapped[str] = mapped_column(String(80))
    checkout_hash: Mapped[str] = mapped_column(String(64), index=True)
    amount_minor: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(3))
    status: Mapped[str] = mapped_column(String(24), default="issued", index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    presented_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    payment_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("payments.id", ondelete="SET NULL"), unique=True
    )


class Ap2Receipt(Base):
    __tablename__ = "ap2_receipts"
    __table_args__ = (
        UniqueConstraint("checkout_id", "receipt_type", name="uq_ap2_checkout_receipt_type"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    checkout_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("checkouts.id", ondelete="RESTRICT"), index=True
    )
    order_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("orders.id", ondelete="RESTRICT"), index=True
    )
    payment_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("payments.id", ondelete="RESTRICT"), index=True
    )
    receipt_type: Mapped[str] = mapped_column(String(24), index=True)
    status: Mapped[str] = mapped_column(String(24))
    issuer: Mapped[str] = mapped_column(String(240))
    key_id: Mapped[str] = mapped_column(String(120))
    reference: Mapped[str] = mapped_column(String(64), index=True)
    signed_jwt: Mapped[str] = mapped_column(Text)
    payload_sha256: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, index=True
    )


class Order(TimestampMixin, Base):
    __tablename__ = "orders"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    merchant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("merchants.id", ondelete="RESTRICT"), index=True
    )
    checkout_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("checkouts.id", ondelete="RESTRICT"), unique=True
    )
    customer_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("user_accounts.id", ondelete="SET NULL"), index=True
    )
    public_number: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    status: Mapped[OrderStatus] = mapped_column(
        Enum(OrderStatus, native_enum=False), default=OrderStatus.AWAITING_PAYMENT, index=True
    )
    currency: Mapped[str] = mapped_column(String(3))
    total_minor: Mapped[int] = mapped_column(Integer)
    fulfillment_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class Payment(TimestampMixin, Base):
    __tablename__ = "payments"
    __table_args__ = (
        UniqueConstraint("provider", "provider_payment_id", name="uq_provider_payment"),
        UniqueConstraint("order_id", "provider", name="uq_payment_order_provider"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    order_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("orders.id", ondelete="RESTRICT"), index=True
    )
    provider: Mapped[str] = mapped_column(String(40), default="razorpay")
    approval_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("checkout_approvals.id", ondelete="RESTRICT"), index=True
    )
    provider_receipt: Mapped[str | None] = mapped_column(String(40), unique=True)
    provider_order_id: Mapped[str | None] = mapped_column(String(120), index=True)
    provider_payment_id: Mapped[str | None] = mapped_column(String(120))
    status: Mapped[PaymentStatus] = mapped_column(
        Enum(PaymentStatus, native_enum=False), default=PaymentStatus.CREATED, index=True
    )
    amount_minor: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(3))
    failure_code: Mapped[str | None] = mapped_column(String(120))
    failure_description: Mapped[str | None] = mapped_column(Text)
    captured_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ScheduledPurchaseIntent(TimestampMixin, Base):
    __tablename__ = "scheduled_purchase_intents"
    __table_args__ = (
        UniqueConstraint(
            "customer_id",
            "idempotency_key",
            name="uq_scheduled_intent_customer_idempotency",
        ),
        UniqueConstraint(
            "customer_id",
            "authorization_challenge_idempotency_key",
            name="uq_scheduled_intent_customer_challenge_idempotency",
        ),
        CheckConstraint("interval_count > 0", name="ck_scheduled_intent_interval_positive"),
        CheckConstraint("max_occurrences > 0", name="ck_scheduled_intent_occurrences_positive"),
        CheckConstraint(
            "successful_occurrences >= 0",
            name="ck_scheduled_intent_successes_nonnegative",
        ),
        CheckConstraint("max_amount_minor >= 0", name="ck_scheduled_intent_order_cap"),
        CheckConstraint("max_total_minor >= 0", name="ck_scheduled_intent_total_cap"),
        CheckConstraint(
            "max_total_minor >= max_amount_minor",
            name="ck_scheduled_intent_total_covers_order",
        ),
        CheckConstraint("spent_minor >= 0", name="ck_scheduled_intent_spent_nonnegative"),
        CheckConstraint(
            "spent_minor <= max_total_minor",
            name="ck_scheduled_intent_spent_within_budget",
        ),
        CheckConstraint(
            "successful_occurrences <= max_occurrences",
            name="ck_scheduled_intent_successes_within_limit",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    merchant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("merchants.id", ondelete="RESTRICT"), index=True
    )
    customer_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("user_accounts.id", ondelete="RESTRICT"), index=True
    )
    address_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("customer_addresses.id", ondelete="RESTRICT"), index=True
    )
    payment_instrument_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("payment_instruments.id", ondelete="RESTRICT"), index=True
    )
    location_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("locations.id", ondelete="RESTRICT"), index=True
    )
    customer_reference: Mapped[str] = mapped_column(String(120), index=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(128))
    request_sha256: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[PurchaseIntentStatus] = mapped_column(
        Enum(PurchaseIntentStatus, native_enum=False),
        default=PurchaseIntentStatus.DRAFT,
        index=True,
    )
    constraints: Mapped[dict[str, Any]] = mapped_column(JSON)
    fulfillment_type: Mapped[FulfillmentType | None] = mapped_column(
        Enum(FulfillmentType, native_enum=False)
    )
    frequency: Mapped[str] = mapped_column(String(16), default="once")
    interval_count: Mapped[int] = mapped_column(Integer, default=1)
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Kolkata")
    max_occurrences: Mapped[int] = mapped_column(Integer, default=1)
    successful_occurrences: Mapped[int] = mapped_column(Integer, default=0)
    max_amount_minor: Mapped[int] = mapped_column(Integer, default=0)
    max_total_minor: Mapped[int] = mapped_column(Integer, default=0)
    spent_minor: Mapped[int] = mapped_column(Integer, default=0)
    currency: Mapped[str] = mapped_column(String(3), default="INR")
    address_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    address_sha256: Mapped[str | None] = mapped_column(String(64))
    display_payload: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    display_sha256: Mapped[str | None] = mapped_column(String(64), index=True)
    authorization_nonce: Mapped[str | None] = mapped_column(String(128), unique=True, index=True)
    authorization_challenge_idempotency_key: Mapped[str | None] = mapped_column(String(128))
    authorization_challenge_request_sha256: Mapped[str | None] = mapped_column(String(64))
    approval_idempotency_key: Mapped[str | None] = mapped_column(String(128))
    approval_request_sha256: Mapped[str | None] = mapped_column(String(64))
    webauthn_challenge: Mapped[str | None] = mapped_column(String(256), unique=True)
    authorization_challenge_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    agent_key_id: Mapped[str | None] = mapped_column(String(120))
    agent_public_jwk: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    open_checkout_mandate: Mapped[str | None] = mapped_column(Text)
    open_payment_mandate: Mapped[str | None] = mapped_column(Text)
    open_checkout_hash: Mapped[str | None] = mapped_column(String(128), index=True)
    authorization_reference: Mapped[str | None] = mapped_column(String(200))
    payment_token_reference: Mapped[str | None] = mapped_column(String(200))
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    next_execution_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    authorized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    provider_authorized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    paused_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_failure_code: Mapped[str | None] = mapped_column(String(120))
    last_failure_message: Mapped[str | None] = mapped_column(Text)


class ScheduledPurchaseRun(TimestampMixin, Base):
    __tablename__ = "scheduled_purchase_runs"
    __table_args__ = (
        UniqueConstraint("intent_id", "scheduled_for", name="uq_scheduled_run_intent_occurrence"),
        UniqueConstraint("idempotency_key", name="uq_scheduled_run_idempotency"),
        CheckConstraint("attempt_count >= 0", name="ck_scheduled_run_attempt_nonnegative"),
        CheckConstraint("amount_minor >= 0", name="ck_scheduled_run_amount_nonnegative"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    intent_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("scheduled_purchase_intents.id", ondelete="CASCADE"), index=True
    )
    scheduled_for: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    status: Mapped[ScheduledRunStatus] = mapped_column(
        Enum(ScheduledRunStatus, native_enum=False),
        default=ScheduledRunStatus.PENDING,
        index=True,
    )
    idempotency_key: Mapped[str] = mapped_column(String(128))
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    checkout_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("checkouts.id", ondelete="SET NULL"), unique=True, index=True
    )
    order_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("orders.id", ondelete="SET NULL"), unique=True, index=True
    )
    payment_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("payments.id", ondelete="SET NULL"), unique=True, index=True
    )
    amount_minor: Mapped[int] = mapped_column(Integer, default=0)
    currency: Mapped[str] = mapped_column(String(3), default="INR")
    merchant_checkout_jwt: Mapped[str | None] = mapped_column(Text)
    merchant_checkout_hash: Mapped[str | None] = mapped_column(String(128), index=True)
    closed_checkout_mandate: Mapped[str | None] = mapped_column(Text)
    closed_payment_mandate: Mapped[str | None] = mapped_column(Text)
    provider_notification_id: Mapped[str | None] = mapped_column(String(120), index=True)
    provider_payment_after: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), index=True
    )
    provider_order_id: Mapped[str | None] = mapped_column(String(120), index=True)
    provider_payment_id: Mapped[str | None] = mapped_column(String(120), index=True)
    failure_code: Mapped[str | None] = mapped_column(String(120), index=True)
    failure_message: Mapped[str | None] = mapped_column(Text)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class AgentConversation(TimestampMixin, Base):
    __tablename__ = "agent_conversations"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    merchant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("merchants.id", ondelete="RESTRICT"), index=True
    )
    customer_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("user_accounts.id", ondelete="CASCADE"), index=True
    )
    title: Mapped[str] = mapped_column(String(160), default="Shopping with Ember")
    status: Mapped[str] = mapped_column(String(24), default="active", index=True)
    last_activity_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, index=True
    )

    customer: Mapped[UserAccount] = relationship(back_populates="agent_conversations")
    messages: Mapped[list[AgentMessage]] = relationship(
        back_populates="conversation",
        cascade="all, delete-orphan",
        order_by="AgentMessage.created_at",
    )
    runs: Mapped[list[AgentRun]] = relationship(
        back_populates="conversation", cascade="all, delete-orphan"
    )


class AgentRun(Base):
    __tablename__ = "agent_runs"
    __table_args__ = (
        UniqueConstraint("conversation_id", "idempotency_key", name="uq_agent_run_idempotency"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("agent_conversations.id", ondelete="CASCADE"), index=True
    )
    customer_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("user_accounts.id", ondelete="CASCADE"), index=True
    )
    idempotency_key: Mapped[str] = mapped_column(String(128))
    request_sha256: Mapped[str] = mapped_column(String(64))
    response_sha256: Mapped[str | None] = mapped_column(String(64))
    model: Mapped[str] = mapped_column(String(120))
    graph_version: Mapped[str] = mapped_column(String(80), default="ask-ember-graph-2")
    policy_version: Mapped[str] = mapped_column(String(80), default="agent-action-policy-1")
    intent: Mapped[str] = mapped_column(String(40), default="answer", index=True)
    risk_level: Mapped[str] = mapped_column(String(24), default="low", index=True)
    allowed_tools: Mapped[list[str]] = mapped_column(JSON, default=list)
    checkpoint_state: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    tool_call_count: Mapped[int] = mapped_column(Integer, default=0)
    mutation_count: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(24), default="running", index=True)
    error_code: Mapped[str | None] = mapped_column(String(120))
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, index=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    conversation: Mapped[AgentConversation] = relationship(back_populates="runs")
    messages: Mapped[list[AgentMessage]] = relationship(back_populates="run")
    tool_calls: Mapped[list[AgentToolCall]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )


class AgentMessage(Base):
    __tablename__ = "agent_messages"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("agent_conversations.id", ondelete="CASCADE"), index=True
    )
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="SET NULL"), index=True
    )
    role: Mapped[str] = mapped_column(String(16), index=True)
    content: Mapped[str] = mapped_column(Text)
    structured_content: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    model: Mapped[str | None] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, index=True
    )

    conversation: Mapped[AgentConversation] = relationship(back_populates="messages")
    run: Mapped[AgentRun | None] = relationship(back_populates="messages")


class AgentToolCall(Base):
    __tablename__ = "agent_tool_calls"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="CASCADE"), index=True
    )
    tool_name: Mapped[str] = mapped_column(String(120), index=True)
    status: Mapped[str] = mapped_column(String(24), default="running", index=True)
    input_payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    output_payload: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    error_code: Mapped[str | None] = mapped_column(String(120))
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, index=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    run: Mapped[AgentRun] = relationship(back_populates="tool_calls")


class AgentMemorySetting(TimestampMixin, Base):
    __tablename__ = "agent_memory_settings"
    __table_args__ = (
        UniqueConstraint(
            "customer_id", "merchant_id", name="uq_agent_memory_setting_customer_merchant"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    customer_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("user_accounts.id", ondelete="CASCADE"), index=True
    )
    merchant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("merchants.id", ondelete="CASCADE"), index=True
    )
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class AgentMemoryFact(TimestampMixin, Base):
    __tablename__ = "agent_memory_facts"
    __table_args__ = (
        UniqueConstraint(
            "customer_id",
            "merchant_id",
            "kind",
            "normalized_key",
            name="uq_agent_memory_fact_scope_key",
        ),
        CheckConstraint(
            "confidence >= 0 AND confidence <= 1", name="ck_agent_memory_confidence_range"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    customer_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("user_accounts.id", ondelete="CASCADE"), index=True
    )
    merchant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("merchants.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(48), index=True)
    normalized_key: Mapped[str] = mapped_column(String(80))
    value: Mapped[dict[str, Any]] = mapped_column(JSON)
    source_message_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("agent_messages.id", ondelete="SET NULL"), index=True
    )
    confidence: Mapped[Decimal] = mapped_column(Numeric(4, 3), default=Decimal("1.000"))
    sensitivity: Mapped[str] = mapped_column(String(24), default="standard")
    content_sha256: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    merchant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("merchants.id", ondelete="RESTRICT"), index=True
    )
    actor_type: Mapped[str] = mapped_column(String(40))
    actor_id: Mapped[str | None] = mapped_column(String(160))
    event_type: Mapped[str] = mapped_column(String(120), index=True)
    aggregate_type: Mapped[str] = mapped_column(String(80), index=True)
    aggregate_id: Mapped[str] = mapped_column(String(120), index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    schema_version: Mapped[str] = mapped_column(String(16), default="1")
    severity: Mapped[str] = mapped_column(String(16), default="info", index=True)
    source_component: Mapped[str] = mapped_column(String(80), default="commerce-core")
    correlation_id: Mapped[str] = mapped_column(String(160), index=True)
    sequence: Mapped[int] = mapped_column(Integer, default=0)
    previous_hash: Mapped[str | None] = mapped_column(String(64))
    current_hash: Mapped[str] = mapped_column(String(64), default="legacy")
    hash_algorithm: Mapped[str] = mapped_column(String(16), default="SHA-256")
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, index=True
    )


class WebhookEvent(Base):
    __tablename__ = "webhook_events"
    __table_args__ = (UniqueConstraint("provider", "event_id", name="uq_webhook_provider_event"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    provider: Mapped[str] = mapped_column(String(40))
    event_id: Mapped[str] = mapped_column(String(160))
    event_type: Mapped[str] = mapped_column(String(120), index=True)
    payload_sha256: Mapped[str] = mapped_column(String(64))
    processed: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, index=True
    )
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failure_message: Mapped[str | None] = mapped_column(Text)


Index("ix_audit_aggregate", AuditEvent.aggregate_type, AuditEvent.aggregate_id)
Index(
    "uq_audit_stream_sequence",
    AuditEvent.merchant_id,
    AuditEvent.aggregate_type,
    AuditEvent.aggregate_id,
    AuditEvent.sequence,
    unique=True,
    sqlite_where=AuditEvent.sequence > 0,
    postgresql_where=AuditEvent.sequence > 0,
)
Index("ix_inventory_variant_location", InventoryItem.variant_id, InventoryItem.location_id)
Index(
    "ix_scheduled_purchase_due",
    ScheduledPurchaseIntent.status,
    ScheduledPurchaseIntent.next_run_at,
)

# Register listeners only after every mapped class, including AuditEvent, exists.
from app.db import audit as audit  # noqa: E402,F401
