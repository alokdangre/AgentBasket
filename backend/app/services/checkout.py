import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.core.config import get_settings
from app.core.errors import ConflictError, DomainError, NotFoundError
from app.db.models import (
    AuditEvent,
    Cart,
    Checkout,
    CheckoutApproval,
    CheckoutFulfillmentOption,
    CheckoutLineItem,
    CheckoutLineModifier,
    CustomerAddress,
    InventoryItem,
    InventoryReservation,
    ModifierGroup,
    ModifierOption,
    Product,
    ProductVariant,
    UserAccount,
)
from app.domain.enums import CheckoutStatus, FulfillmentType, ProductStatus, ReservationStatus
from app.schemas.checkout import (
    CheckoutApprovalCreate,
    CheckoutApprovalOut,
    CheckoutCancelOut,
    CheckoutCreate,
    CheckoutFromCartCreate,
    CheckoutFulfillmentOut,
    CheckoutItemCreate,
    CheckoutLineOut,
    CheckoutModifierOut,
    CheckoutOut,
)
from app.services.location import LocationService


def utc_now() -> datetime:
    return datetime.now(UTC)


class CheckoutService:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.settings = get_settings()
        self.locations = LocationService(db)

    def create(
        self, payload: CheckoutCreate, idempotency_key: str, customer: UserAccount
    ) -> CheckoutOut:
        with self.db.begin():
            return self._create(payload, idempotency_key, customer)

    def create_from_cart(
        self,
        payload: CheckoutFromCartCreate,
        idempotency_key: str,
        customer: UserAccount,
    ) -> CheckoutOut:
        with self.db.begin():
            merchant = self.locations.merchant_by_slug(payload.merchant_slug)
            cart = self.db.scalar(
                select(Cart)
                .where(Cart.user_id == customer.id, Cart.merchant_id == merchant.id)
                .options(selectinload(Cart.items))
            )
            if cart is None or not cart.items:
                raise DomainError("empty_cart", "Add at least one item before checkout.")

            address = None
            if payload.fulfillment_type != FulfillmentType.PICKUP:
                address = self.db.scalar(
                    select(CustomerAddress).where(
                        CustomerAddress.id == payload.address_id,
                        CustomerAddress.user_id == customer.id,
                    )
                )
                if address is None:
                    raise NotFoundError("address_not_found", "Delivery address was not found.")

            checkout_payload = CheckoutCreate(
                merchant_slug=payload.merchant_slug,
                fulfillment_type=payload.fulfillment_type,
                postal_code=address.postal_code if address else None,
                location_id=payload.location_id,
                delivery_address=self._address_snapshot(address) if address else None,
                items=[
                    CheckoutItemCreate(
                        variant_id=item.variant_id,
                        quantity=item.quantity,
                        modifier_option_ids=[
                            uuid.UUID(option_id) for option_id in item.modifier_option_ids
                        ],
                    )
                    for item in cart.items
                ],
            )
            return self._create(checkout_payload, idempotency_key, customer)

    def _create(
        self, payload: CheckoutCreate, idempotency_key: str, customer: UserAccount
    ) -> CheckoutOut:
        request_hash = self._request_hash(payload, customer.id)
        merchant = self.locations.merchant_by_slug(payload.merchant_slug)
        existing = self._checkout_by_idempotency(merchant.id, customer.id, idempotency_key)
        if existing:
            if existing.request_hash != request_hash:
                raise ConflictError(
                    "idempotency_key_reused",
                    "Idempotency-Key was already used for a different checkout request",
                )
            return self._to_output(existing)

        location, service_zone = self.locations.resolve_location(
            merchant=merchant,
            fulfillment_type=payload.fulfillment_type,
            postal_code=payload.postal_code,
            location_id=payload.location_id,
        )
        variants = self._load_variants(merchant.id, [item.variant_id for item in payload.items])
        if len(variants) != len(set(item.variant_id for item in payload.items)):
            raise NotFoundError("variant_not_found", "One or more product variants are unavailable")

        expires_at = utc_now() + timedelta(minutes=self.settings.checkout_ttl_minutes)
        checkout = Checkout(
            merchant_id=merchant.id,
            customer_id=customer.id,
            location_id=location.id,
            status=CheckoutStatus.OPEN,
            currency=merchant.currency,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            fulfillment_type=payload.fulfillment_type,
            postal_code=payload.postal_code,
            delivery_address=payload.delivery_address,
            expires_at=expires_at,
        )
        self.db.add(checkout)
        self.db.flush()

        subtotal_minor = 0
        for requested_item in payload.items:
            variant = variants[requested_item.variant_id]
            if variant.currency.upper() != merchant.currency.upper():
                raise DomainError(
                    "currency_mismatch", "Variant currency does not match merchant currency"
                )
            selected_modifiers = self._validate_modifiers(
                variant.product, requested_item.modifier_option_ids
            )
            modifier_unit_total = sum(option.price_delta_minor for option in selected_modifiers)
            line_total = (variant.price_minor + modifier_unit_total) * requested_item.quantity
            line = CheckoutLineItem(
                checkout_id=checkout.id,
                variant_id=variant.id,
                quantity=requested_item.quantity,
                unit_price_minor=variant.price_minor,
                modifier_total_minor=modifier_unit_total * requested_item.quantity,
                line_total_minor=line_total,
                product_snapshot={
                    "product_id": str(variant.product.id),
                    "product_name": variant.product.name,
                    "product_type": variant.product.product_type.value,
                    "variant_name": variant.name,
                    "sku": variant.sku,
                    "attributes": variant.attributes,
                },
            )
            self.db.add(line)
            self.db.flush()
            for option in selected_modifiers:
                line.modifiers.append(
                    CheckoutLineModifier(
                        modifier_option_id=option.id,
                        name=option.name,
                        price_delta_minor=option.price_delta_minor,
                    )
                )
            if variant.track_inventory:
                self._reserve_inventory(
                    line=line,
                    location_id=location.id,
                    variant_id=variant.id,
                    quantity=requested_item.quantity,
                    expires_at=expires_at,
                )
            subtotal_minor += line_total

        if service_zone and subtotal_minor < service_zone.minimum_order_minor:
            raise DomainError(
                "minimum_order_not_met",
                f"Minimum order is {service_zone.minimum_order_minor} minor currency units",
            )

        delivery_minor = 0
        if service_zone:
            delivery_minor = service_zone.fee_minor
            if (
                service_zone.free_above_minor is not None
                and subtotal_minor >= service_zone.free_above_minor
            ):
                delivery_minor = 0
            fulfillment = CheckoutFulfillmentOption(
                checkout_id=checkout.id,
                service_zone_id=service_zone.id,
                fulfillment_type=service_zone.fulfillment_type,
                title=self._fulfillment_title(service_zone.fulfillment_type),
                fee_minor=delivery_minor,
                eta_min_minutes=service_zone.eta_min_minutes,
                eta_max_minutes=service_zone.eta_max_minutes,
                selected=True,
            )
        else:
            fulfillment = CheckoutFulfillmentOption(
                checkout_id=checkout.id,
                service_zone_id=None,
                fulfillment_type=FulfillmentType.PICKUP,
                title=f"Pickup from {location.name}",
                fee_minor=0,
                eta_min_minutes=location.preparation_minutes,
                eta_max_minutes=location.preparation_minutes + 15,
                selected=True,
            )
        checkout.fulfillment_options.append(fulfillment)
        checkout.subtotal_minor = subtotal_minor
        checkout.delivery_minor = delivery_minor
        checkout.total_minor = subtotal_minor + delivery_minor
        checkout.status = CheckoutStatus.READY_FOR_APPROVAL
        self.db.add(
            AuditEvent(
                merchant_id=merchant.id,
                actor_type="customer",
                actor_id=str(customer.id),
                event_type="checkout.quoted",
                aggregate_type="checkout",
                aggregate_id=str(checkout.id),
                payload={
                    "total_minor": checkout.total_minor,
                    "currency": checkout.currency,
                    "location_id": str(location.id),
                    "fulfillment_type": checkout.fulfillment_type.value,
                },
            )
        )
        self.db.flush()
        return self._to_output(checkout)

    def get(self, checkout_id: uuid.UUID, customer: UserAccount) -> CheckoutOut:
        checkout = self._checkout_for_customer(checkout_id, customer.id)
        if checkout is None:
            raise NotFoundError("checkout_not_found", "Checkout was not found")
        return self._to_output(checkout)

    def cancel(self, checkout_id: uuid.UUID, customer: UserAccount) -> CheckoutCancelOut:
        with self.db.begin():
            checkout = self._checkout_for_customer(checkout_id, customer.id, lock=True)
            if checkout is None:
                raise NotFoundError("checkout_not_found", "Checkout was not found")
            if checkout.status not in {
                CheckoutStatus.OPEN,
                CheckoutStatus.READY_FOR_APPROVAL,
                CheckoutStatus.APPROVED,
            }:
                raise ConflictError(
                    "checkout_not_cancelable",
                    f"Checkout cannot be canceled while {checkout.status.value}",
                )
            self._release_reservations(checkout, ReservationStatus.RELEASED)
            checkout.status = CheckoutStatus.CANCELED
            self.db.add(
                AuditEvent(
                    merchant_id=checkout.merchant_id,
                    actor_type="customer",
                    actor_id=str(customer.id),
                    event_type="checkout.canceled",
                    aggregate_type="checkout",
                    aggregate_id=str(checkout.id),
                    payload={},
                )
            )
            self.db.flush()
            return CheckoutCancelOut(id=checkout.id, status=checkout.status)

    def approve(
        self,
        checkout_id: uuid.UUID,
        payload: CheckoutApprovalCreate,
        customer: UserAccount,
    ) -> CheckoutApprovalOut:
        expired = False
        output: CheckoutApprovalOut | None = None
        with self.db.begin():
            checkout = self._checkout_for_customer(checkout_id, customer.id, lock=True)
            if checkout is None:
                raise NotFoundError("checkout_not_found", "Checkout was not found")
            existing = self.db.scalar(
                select(CheckoutApproval).where(CheckoutApproval.checkout_id == checkout.id)
            )
            if existing is not None:
                if (
                    existing.quote_version != payload.quote_version
                    or existing.approved_total_minor != payload.expected_total_minor
                ):
                    raise ConflictError(
                        "approval_mismatch", "This checkout was approved with different terms."
                    )
                return self._approval_output(existing)
            if checkout.status != CheckoutStatus.READY_FOR_APPROVAL:
                raise ConflictError(
                    "checkout_not_approvable",
                    f"Checkout cannot be approved while {checkout.status.value}",
                )
            if self._is_expired(checkout.expires_at):
                self._release_reservations(checkout, ReservationStatus.EXPIRED)
                checkout.status = CheckoutStatus.EXPIRED
                expired = True
            elif (
                payload.expected_total_minor != checkout.total_minor
                or payload.quote_version != checkout.quote_version
            ):
                raise ConflictError(
                    "quote_changed",
                    "The approved amount or quote version does not match the current checkout.",
                )
            else:
                evidence = self._approval_evidence(checkout, customer.id)
                approval = CheckoutApproval(
                    checkout_id=checkout.id,
                    customer_id=customer.id,
                    quote_version=checkout.quote_version,
                    approved_total_minor=checkout.total_minor,
                    currency=checkout.currency,
                    evidence_sha256=evidence,
                )
                self.db.add(approval)
                checkout.status = CheckoutStatus.APPROVED
                self.db.add(
                    AuditEvent(
                        merchant_id=checkout.merchant_id,
                        actor_type="customer",
                        actor_id=str(customer.id),
                        event_type="checkout.approved",
                        aggregate_type="checkout",
                        aggregate_id=str(checkout.id),
                        payload={
                            "total_minor": checkout.total_minor,
                            "currency": checkout.currency,
                            "quote_version": checkout.quote_version,
                            "evidence_sha256": evidence,
                        },
                    )
                )
                self.db.flush()
                output = self._approval_output(approval)
        if expired:
            raise ConflictError("checkout_expired", "The checkout quote has expired.")
        if output is None:
            raise RuntimeError("Approval output was not created")
        return output

    def expire_due(self, now: datetime | None = None) -> int:
        instant = now or utc_now()
        with self.db.begin():
            checkouts = list(
                self.db.scalars(
                    select(Checkout)
                    .where(
                        Checkout.status.in_(
                            [CheckoutStatus.OPEN, CheckoutStatus.READY_FOR_APPROVAL]
                            + [CheckoutStatus.APPROVED]
                        ),
                        Checkout.expires_at <= instant,
                    )
                    .options(
                        selectinload(Checkout.lines).selectinload(CheckoutLineItem.reservations)
                    )
                ).unique()
            )
            for checkout in checkouts:
                self._release_reservations(checkout, ReservationStatus.EXPIRED)
                checkout.status = CheckoutStatus.EXPIRED
            return len(checkouts)

    def _checkout_by_idempotency(
        self, merchant_id: uuid.UUID, customer_id: uuid.UUID, idempotency_key: str
    ) -> Checkout | None:
        return self.db.scalar(
            select(Checkout)
            .where(
                Checkout.merchant_id == merchant_id,
                Checkout.customer_id == customer_id,
                Checkout.idempotency_key == idempotency_key,
            )
            .options(*self._checkout_load_options())
        )

    def _checkout_by_id(self, checkout_id: uuid.UUID) -> Checkout | None:
        return self.db.scalar(
            select(Checkout)
            .where(Checkout.id == checkout_id)
            .options(*self._checkout_load_options())
        )

    def _checkout_for_customer(
        self, checkout_id: uuid.UUID, customer_id: uuid.UUID, *, lock: bool = False
    ) -> Checkout | None:
        statement = (
            select(Checkout)
            .where(Checkout.id == checkout_id, Checkout.customer_id == customer_id)
            .options(*self._checkout_load_options())
        )
        if lock:
            statement = statement.with_for_update()
        return self.db.scalar(statement)

    @staticmethod
    def _checkout_load_options() -> tuple:
        return (
            selectinload(Checkout.lines).selectinload(CheckoutLineItem.modifiers),
            selectinload(Checkout.lines).selectinload(CheckoutLineItem.reservations),
            selectinload(Checkout.fulfillment_options),
        )

    def _load_variants(
        self, merchant_id: uuid.UUID, variant_ids: list[uuid.UUID]
    ) -> dict[uuid.UUID, ProductVariant]:
        statement = (
            select(ProductVariant)
            .join(Product, ProductVariant.product_id == Product.id)
            .where(
                ProductVariant.id.in_(set(variant_ids)),
                ProductVariant.merchant_id == merchant_id,
                Product.merchant_id == merchant_id,
                ProductVariant.sellable.is_(True),
                Product.status == ProductStatus.ACTIVE,
            )
            .options(
                selectinload(ProductVariant.product)
                .selectinload(Product.modifier_groups)
                .selectinload(ModifierGroup.options)
            )
        )
        return {variant.id: variant for variant in self.db.scalars(statement).unique()}

    @staticmethod
    def _validate_modifiers(
        product: Product, selected_ids: list[uuid.UUID]
    ) -> list[ModifierOption]:
        if len(selected_ids) != len(set(selected_ids)):
            raise DomainError("duplicate_modifier", "A modifier option was selected more than once")

        available = {
            option.id: option
            for group in product.modifier_groups
            for option in group.options
            if option.active
        }
        unknown = set(selected_ids) - set(available)
        if unknown:
            raise DomainError(
                "invalid_modifier", "A modifier does not belong to the selected product"
            )

        selected_set = set(selected_ids)
        for group in product.modifier_groups:
            count = sum(option.id in selected_set for option in group.options)
            if count < group.minimum_selections or (group.required and count == 0):
                raise DomainError("required_modifier_missing", f"Select an option for {group.name}")
            if count > group.maximum_selections:
                raise DomainError(
                    "too_many_modifiers", f"Too many options selected for {group.name}"
                )
        return [available[option_id] for option_id in selected_ids]

    def _reserve_inventory(
        self,
        line: CheckoutLineItem,
        location_id: uuid.UUID,
        variant_id: uuid.UUID,
        quantity: int,
        expires_at: datetime,
    ) -> None:
        inventory = self.db.scalar(
            select(InventoryItem)
            .where(
                InventoryItem.location_id == location_id,
                InventoryItem.variant_id == variant_id,
            )
            .with_for_update()
        )
        available = (
            0 if inventory is None else inventory.on_hand_quantity - inventory.reserved_quantity
        )
        if inventory is None or available < quantity:
            raise ConflictError("insufficient_inventory", "Requested quantity is unavailable")
        inventory.reserved_quantity += quantity
        line.reservations.append(
            InventoryReservation(
                inventory_item_id=inventory.id,
                quantity=quantity,
                status=ReservationStatus.ACTIVE,
                expires_at=expires_at,
            )
        )

    def _release_reservations(self, checkout: Checkout, terminal_status: ReservationStatus) -> None:
        for line in checkout.lines:
            for reservation in line.reservations:
                if reservation.status != ReservationStatus.ACTIVE:
                    continue
                inventory = self.db.get(InventoryItem, reservation.inventory_item_id)
                if inventory is not None:
                    inventory.reserved_quantity = max(
                        0, inventory.reserved_quantity - reservation.quantity
                    )
                reservation.status = terminal_status

    @staticmethod
    def _request_hash(payload: CheckoutCreate, customer_id: uuid.UUID) -> str:
        canonical = json.dumps(
            {"customer_id": str(customer_id), "checkout": payload.model_dump(mode="json")},
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode()).hexdigest()

    @staticmethod
    def _approval_evidence(checkout: Checkout, customer_id: uuid.UUID) -> str:
        canonical = json.dumps(
            {
                "checkout_id": str(checkout.id),
                "customer_id": str(customer_id),
                "currency": checkout.currency,
                "quote_version": checkout.quote_version,
                "request_hash": checkout.request_hash,
                "total_minor": checkout.total_minor,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode()).hexdigest()

    @staticmethod
    def _approval_output(approval: CheckoutApproval) -> CheckoutApprovalOut:
        return CheckoutApprovalOut(
            id=approval.id,
            checkout_id=approval.checkout_id,
            quote_version=approval.quote_version,
            approved_total_minor=approval.approved_total_minor,
            currency=approval.currency,
            evidence_sha256=approval.evidence_sha256,
            approved_at=approval.approved_at,
        )

    @staticmethod
    def _address_snapshot(address: CustomerAddress) -> dict[str, str | None]:
        return {
            "label": address.label,
            "recipient_name": address.recipient_name,
            "phone": address.phone,
            "line_one": address.line_one,
            "line_two": address.line_two,
            "landmark": address.landmark,
            "city": address.city,
            "region": address.region,
            "postal_code": address.postal_code,
            "country_code": address.country_code,
        }

    @staticmethod
    def _is_expired(expires_at: datetime) -> bool:
        comparable = expires_at if expires_at.tzinfo else expires_at.replace(tzinfo=UTC)
        return comparable <= utc_now()

    @staticmethod
    def _fulfillment_title(fulfillment_type: FulfillmentType) -> str:
        return {
            FulfillmentType.LOCAL_DELIVERY: "Local delivery",
            FulfillmentType.SHIPPING: "Standard shipping",
            FulfillmentType.PICKUP: "Pickup",
        }[fulfillment_type]

    @staticmethod
    def _to_output(checkout: Checkout) -> CheckoutOut:
        return CheckoutOut(
            id=checkout.id,
            merchant_id=checkout.merchant_id,
            customer_id=checkout.customer_id,
            location_id=checkout.location_id,
            status=checkout.status,
            currency=checkout.currency,
            fulfillment_type=checkout.fulfillment_type,
            postal_code=checkout.postal_code,
            lines=[
                CheckoutLineOut(
                    id=line.id,
                    variant_id=line.variant_id,
                    product_name=line.product_snapshot["product_name"],
                    variant_name=line.product_snapshot["variant_name"],
                    quantity=line.quantity,
                    unit_price_minor=line.unit_price_minor,
                    modifier_total_minor=line.modifier_total_minor,
                    line_total_minor=line.line_total_minor,
                    modifiers=[
                        CheckoutModifierOut(
                            id=modifier.modifier_option_id,
                            name=modifier.name,
                            price_delta_minor=modifier.price_delta_minor,
                        )
                        for modifier in line.modifiers
                    ],
                )
                for line in checkout.lines
            ],
            fulfillment_options=[
                CheckoutFulfillmentOut.model_validate(option)
                for option in checkout.fulfillment_options
            ],
            subtotal_minor=checkout.subtotal_minor,
            delivery_minor=checkout.delivery_minor,
            discount_minor=checkout.discount_minor,
            tax_minor=checkout.tax_minor,
            tax_included=checkout.tax_included,
            total_minor=checkout.total_minor,
            quote_version=checkout.quote_version,
            expires_at=checkout.expires_at,
        )
