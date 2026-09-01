import hashlib
import json
import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.core.config import get_settings
from app.core.errors import ConflictError, DomainError, NotFoundError
from app.db.models import (
    AuditEvent,
    Cart,
    Checkout,
    CheckoutApproval,
    CheckoutLineItem,
    InventoryItem,
    Merchant,
    Order,
    Payment,
    PaymentCredentialGrant,
    UserAccount,
    WebhookEvent,
)
from app.domain.enums import (
    CheckoutStatus,
    OrderStatus,
    PaymentStatus,
    ReservationStatus,
)
from app.payments.razorpay import (
    RazorpayGateway,
    RazorpayOrder,
    RazorpayPayment,
    verify_webhook_signature,
)
from app.schemas.payment import (
    OrderReceiptOut,
    OrderSummaryOut,
    OrderTimelineEventOut,
    PaymentReceiptOut,
    RazorpaySessionOut,
    RazorpayVerifyRequest,
    WebhookResultOut,
)
from app.services.ap2 import AP2Service
from app.services.credentials_provider import CredentialsProviderService


def utc_now() -> datetime:
    return datetime.now(UTC)


class PaymentService:
    def __init__(self, db: Session, gateway: RazorpayGateway | None = None) -> None:
        self.db = db
        self.gateway = gateway

    def create_session(self, checkout_id: uuid.UUID, customer: UserAccount) -> RazorpaySessionOut:
        if self.gateway is None:
            raise RuntimeError("Razorpay gateway is required")
        expired = False
        output: RazorpaySessionOut | None = None
        with self.db.begin():
            checkout = self._checkout(checkout_id, customer.id, lock=True)
            if checkout is None:
                raise NotFoundError("checkout_not_found", "Checkout was not found.")
            if checkout.status not in {
                CheckoutStatus.APPROVED,
                CheckoutStatus.PAYMENT_PENDING,
            }:
                raise ConflictError(
                    "checkout_not_payment_ready",
                    f"Checkout cannot start payment while {checkout.status.value}.",
                )
            if checkout.status == CheckoutStatus.APPROVED and self._is_expired(checkout.expires_at):
                self._release_reservations(checkout)
                checkout.status = CheckoutStatus.EXPIRED
                expired = True
            else:
                approval = self.db.scalar(
                    select(CheckoutApproval).where(
                        CheckoutApproval.checkout_id == checkout.id,
                        CheckoutApproval.customer_id == customer.id,
                    )
                )
                if approval is None:
                    raise ConflictError(
                        "approval_required", "Approve the exact checkout before payment."
                    )
                credential_grant = None
                if checkout.source == "agent":
                    ap2_service = AP2Service(self.db)
                    ap2_service.require_verified_mandates(checkout, approval)
                    credential_grant = CredentialsProviderService(
                        self.db, ap2_service.keys
                    ).require_grant(checkout, customer.id)
                order = self.db.scalar(
                    select(Order).where(Order.checkout_id == checkout.id).with_for_update()
                )
                if order is None:
                    order = Order(
                        merchant_id=checkout.merchant_id,
                        checkout_id=checkout.id,
                        customer_id=customer.id,
                        public_number=self._public_number(checkout.id),
                        status=OrderStatus.AWAITING_PAYMENT,
                        currency=checkout.currency,
                        total_minor=checkout.total_minor,
                        fulfillment_snapshot=self._fulfillment_snapshot(checkout),
                    )
                    self.db.add(order)
                    self.db.flush()
                payment = self.db.scalar(
                    select(Payment)
                    .where(Payment.order_id == order.id, Payment.provider == "razorpay")
                    .with_for_update()
                )
                receipt = self._provider_receipt(checkout.id)
                if payment is None:
                    payment = Payment(
                        order_id=order.id,
                        provider="razorpay",
                        approval_id=approval.id,
                        provider_receipt=receipt,
                        status=PaymentStatus.CREATED,
                        amount_minor=checkout.total_minor,
                        currency=checkout.currency,
                    )
                    self.db.add(payment)
                    self.db.flush()
                if payment.status == PaymentStatus.CAPTURED:
                    raise ConflictError("payment_already_captured", "Payment is already complete.")
                if payment.provider_order_id is None:
                    provider_order = self.gateway.find_order_by_receipt(receipt)
                    if provider_order is None:
                        provider_order = self.gateway.create_order(
                            amount=checkout.total_minor,
                            currency=checkout.currency,
                            receipt=receipt,
                            notes={
                                "checkout_id": str(checkout.id),
                                "internal_order_id": str(order.id),
                                "public_number": order.public_number,
                            },
                        )
                    self._validate_created_order(provider_order, checkout, receipt)
                    payment.provider_order_id = provider_order.id
                if credential_grant is not None and credential_grant.status == "issued":
                    credential_grant.status = "presented"
                    credential_grant.presented_at = utc_now()
                transitioned = checkout.status != CheckoutStatus.PAYMENT_PENDING
                checkout.status = CheckoutStatus.PAYMENT_PENDING
                if transitioned:
                    self.db.add(
                        AuditEvent(
                            merchant_id=checkout.merchant_id,
                            actor_type="customer",
                            actor_id=str(customer.id),
                            event_type="payment.session_created",
                            aggregate_type="payment",
                            aggregate_id=str(payment.id),
                            payload={
                                "checkout_id": str(checkout.id),
                                "order_id": str(order.id),
                                "provider_order_id": payment.provider_order_id,
                                "amount_minor": payment.amount_minor,
                                "currency": payment.currency,
                                "credential_grant_id": (
                                    str(credential_grant.id)
                                    if credential_grant is not None
                                    else None
                                ),
                            },
                        )
                    )
                merchant = self.db.get(Merchant, checkout.merchant_id)
                if merchant is None:
                    raise NotFoundError("merchant_not_found", "Merchant was not found.")
                output = RazorpaySessionOut(
                    checkout_id=checkout.id,
                    order_id=order.id,
                    public_number=order.public_number,
                    key_id=self.gateway.key_id,
                    provider_order_id=payment.provider_order_id,
                    amount_minor=payment.amount_minor,
                    currency=payment.currency,
                    merchant_name=merchant.name,
                    description=f"Order {order.public_number}",
                    customer_name=customer.full_name,
                    customer_email=customer.email,
                    customer_phone=customer.phone or (checkout.delivery_address or {}).get("phone"),
                )
        if expired:
            raise ConflictError("checkout_expired", "The checkout quote has expired.")
        if output is None:
            raise RuntimeError("Payment session output was not created")
        return output

    def verify_checkout_result(
        self, payload: RazorpayVerifyRequest, customer: UserAccount
    ) -> OrderReceiptOut:
        if self.gateway is None:
            raise RuntimeError("Razorpay gateway is required")
        invalid_signature = False
        with self.db.begin():
            checkout = self._checkout(payload.checkout_id, customer.id)
            if checkout is None:
                raise NotFoundError("checkout_not_found", "Checkout was not found.")
            order, payment = self._order_and_payment(checkout.id, customer.id)
            if payment.provider_order_id != payload.razorpay_order_id:
                invalid_signature = True
            elif not self.gateway.verify_checkout_signature(
                order_id=payment.provider_order_id,
                payment_id=payload.razorpay_payment_id,
                signature=payload.razorpay_signature,
            ):
                invalid_signature = True
            if invalid_signature:
                self.db.add(
                    AuditEvent(
                        merchant_id=checkout.merchant_id,
                        actor_type="customer",
                        actor_id=str(customer.id),
                        event_type="payment.signature_rejected",
                        aggregate_type="payment",
                        aggregate_id=str(payment.id),
                        payload={"checkout_id": str(checkout.id)},
                    )
                )
            internal_order_id = order.id
            expected_payment = self._payment_expectation(payment)
        if invalid_signature:
            raise DomainError(
                "invalid_payment_signature",
                "Payment verification failed. The order was not marked paid.",
                400,
            )

        provider_payment = self.gateway.fetch_payment(payload.razorpay_payment_id)
        provider_order = self.gateway.fetch_order(payload.razorpay_order_id)
        self._validate_captured_provider_state(provider_payment, provider_order, expected_payment)
        with self.db.begin():
            checkout = self._checkout(payload.checkout_id, customer.id, lock=True)
            if checkout is None:
                raise NotFoundError("checkout_not_found", "Checkout was not found.")
            order, payment = self._order_and_payment(checkout.id, customer.id, lock=True)
            self._capture(payment, order, checkout, provider_payment, source="checkout_callback")
        return self.receipt(internal_order_id, customer)

    def handle_webhook(self, raw_body: bytes, signature: str, event_id: str) -> WebhookResultOut:
        secret = get_settings().razorpay_webhook_secret
        if not secret:
            raise DomainError(
                "razorpay_webhook_not_configured",
                "Razorpay webhook verification is not configured.",
                503,
            )
        if not signature or not verify_webhook_signature(raw_body, signature, secret):
            raise DomainError("invalid_webhook_signature", "Invalid webhook signature.", 400)
        if not event_id:
            raise DomainError("missing_webhook_event_id", "Webhook event ID is required.", 400)
        try:
            payload = json.loads(raw_body)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise DomainError(
                "invalid_webhook_payload", "Webhook payload is invalid.", 400
            ) from error
        if not isinstance(payload, dict) or not isinstance(payload.get("event"), str):
            raise DomainError("invalid_webhook_payload", "Webhook payload is invalid.", 400)
        event_type = payload["event"]
        payload_hash = hashlib.sha256(raw_body).hexdigest()
        with self.db.begin():
            existing = self.db.scalar(
                select(WebhookEvent).where(
                    WebhookEvent.provider == "razorpay", WebhookEvent.event_id == event_id
                )
            )
            if existing is not None:
                if existing.payload_sha256 != payload_hash:
                    raise ConflictError(
                        "webhook_event_collision",
                        "Webhook event ID was reused with a different payload.",
                    )
                return WebhookResultOut(
                    status="duplicate", event_id=event_id, event_type=existing.event_type
                )
            event = WebhookEvent(
                provider="razorpay",
                event_id=event_id,
                event_type=event_type,
                payload_sha256=payload_hash,
            )
            self.db.add(event)
            self.db.flush()
            status = "ignored"
            if event_type in {"payment.captured", "order.paid"}:
                entity = self._webhook_payment_entity(payload)
                payment, order, checkout = self._records_by_provider_order(
                    str(entity.get("order_id", "")), lock=True
                )
                provider_payment = self._payment_from_entity(entity)
                self._capture(payment, order, checkout, provider_payment, source="webhook")
                status = "processed"
            elif event_type == "payment.failed":
                entity = self._webhook_payment_entity(payload)
                payment, order, checkout = self._records_by_provider_order(
                    str(entity.get("order_id", "")), lock=True
                )
                self._record_failure(payment, order, checkout, entity)
                status = "processed"
            event.processed = True
            event.processed_at = utc_now()
            return WebhookResultOut(status=status, event_id=event_id, event_type=event_type)

    def receipt(self, order_id: uuid.UUID, customer: UserAccount) -> OrderReceiptOut:
        order = self.db.scalar(
            select(Order).where(Order.id == order_id, Order.customer_id == customer.id)
        )
        if order is None:
            raise NotFoundError("order_not_found", "Order was not found.")
        checkout = self.db.get(Checkout, order.checkout_id)
        payment = self.db.scalar(
            select(Payment).where(Payment.order_id == order.id, Payment.provider == "razorpay")
        )
        if checkout is None or payment is None:
            raise NotFoundError("order_not_found", "Order was not found.")
        aggregate_ids = [str(checkout.id), str(order.id), str(payment.id)]
        timeline = list(
            self.db.scalars(
                select(AuditEvent)
                .where(
                    AuditEvent.merchant_id == order.merchant_id,
                    AuditEvent.aggregate_id.in_(aggregate_ids),
                )
                .order_by(AuditEvent.occurred_at)
            )
        )
        return OrderReceiptOut(
            id=order.id,
            public_number=order.public_number,
            checkout_id=checkout.id,
            checkout_status=checkout.status,
            status=order.status,
            total_minor=order.total_minor,
            currency=order.currency,
            fulfillment=order.fulfillment_snapshot,
            created_at=order.created_at,
            payment=PaymentReceiptOut(
                status=payment.status,
                provider=payment.provider,
                provider_order_id=payment.provider_order_id,
                provider_payment_id=payment.provider_payment_id,
                amount_minor=payment.amount_minor,
                currency=payment.currency,
                captured_at=payment.captured_at,
            ),
            timeline=[
                OrderTimelineEventOut(
                    event_type=item.event_type,
                    actor_type=item.actor_type,
                    occurred_at=item.occurred_at,
                    payload=item.payload,
                )
                for item in timeline
            ],
        )

    def orders(self, customer: UserAccount) -> list[OrderSummaryOut]:
        orders = list(
            self.db.scalars(
                select(Order)
                .where(Order.customer_id == customer.id)
                .order_by(Order.created_at.desc())
                .limit(100)
            )
        )
        return [
            OrderSummaryOut(
                id=order.id,
                public_number=order.public_number,
                status=order.status,
                total_minor=order.total_minor,
                currency=order.currency,
                created_at=order.created_at,
            )
            for order in orders
        ]

    def _capture(
        self,
        payment: Payment,
        order: Order,
        checkout: Checkout,
        provider_payment: RazorpayPayment,
        *,
        source: str,
    ) -> None:
        expectation = self._payment_expectation(payment)
        if (
            provider_payment.order_id != expectation["provider_order_id"]
            or provider_payment.amount != expectation["amount_minor"]
            or provider_payment.currency != expectation["currency"]
            or provider_payment.status != "captured"
            or not provider_payment.captured
        ):
            raise ConflictError(
                "payment_state_mismatch",
                "Captured payment does not match the approved order.",
            )
        if payment.status == PaymentStatus.CAPTURED:
            return
        payment.provider_payment_id = provider_payment.id
        payment.status = PaymentStatus.CAPTURED
        payment.captured_at = utc_now()
        payment.failure_code = None
        payment.failure_description = None
        order.status = OrderStatus.PAID
        checkout.status = CheckoutStatus.COMPLETED
        grant = self.db.scalar(
            select(PaymentCredentialGrant).where(PaymentCredentialGrant.checkout_id == checkout.id)
        )
        if grant is not None:
            grant.status = "consumed"
            grant.consumed_at = utc_now()
            grant.payment_id = payment.id
        self._consume_reservations(checkout)
        self._subtract_checkout_from_cart(checkout)
        AP2Service(self.db).record_success_receipts(
            checkout,
            order,
            payment,
            provider_payment.network_confirmation_id,
        )
        self.db.add_all(
            [
                AuditEvent(
                    merchant_id=checkout.merchant_id,
                    actor_type="payment_provider",
                    actor_id="razorpay",
                    event_type="payment.captured",
                    aggregate_type="payment",
                    aggregate_id=str(payment.id),
                    payload={
                        "provider_order_id": payment.provider_order_id,
                        "provider_payment_id": provider_payment.id,
                        "amount_minor": payment.amount_minor,
                        "currency": payment.currency,
                        "source": source,
                    },
                ),
                AuditEvent(
                    merchant_id=checkout.merchant_id,
                    actor_type="commerce_core",
                    actor_id=None,
                    event_type="order.paid",
                    aggregate_type="order",
                    aggregate_id=str(order.id),
                    payload={"payment_id": str(payment.id)},
                ),
                AuditEvent(
                    merchant_id=checkout.merchant_id,
                    actor_type="commerce_core",
                    actor_id=None,
                    event_type="checkout.completed",
                    aggregate_type="checkout",
                    aggregate_id=str(checkout.id),
                    payload={"order_id": str(order.id)},
                ),
            ]
        )

    def _record_failure(
        self,
        payment: Payment,
        order: Order,
        checkout: Checkout,
        entity: dict,
    ) -> None:
        if payment.status == PaymentStatus.CAPTURED:
            return
        if (
            int(entity.get("amount", -1)) != payment.amount_minor
            or str(entity.get("currency", "")).upper() != payment.currency
        ):
            raise ConflictError(
                "payment_state_mismatch", "Failed payment does not match the order."
            )
        payment.provider_payment_id = str(entity.get("id", "")) or None
        payment.status = PaymentStatus.FAILED
        payment.failure_code = str(entity.get("error_code") or "payment_failed")[:120]
        payment.failure_description = str(
            entity.get("error_description") or "Razorpay reported a failed payment attempt."
        )
        self.db.add(
            AuditEvent(
                merchant_id=checkout.merchant_id,
                actor_type="payment_provider",
                actor_id="razorpay",
                event_type="payment.failed",
                aggregate_type="payment",
                aggregate_id=str(payment.id),
                payload={
                    "provider_payment_id": payment.provider_payment_id,
                    "error_code": payment.failure_code,
                    "order_id": str(order.id),
                },
            )
        )

    def _consume_reservations(self, checkout: Checkout) -> None:
        for line in checkout.lines:
            for reservation in line.reservations:
                if reservation.status != ReservationStatus.ACTIVE:
                    continue
                inventory = self.db.scalar(
                    select(InventoryItem)
                    .where(InventoryItem.id == reservation.inventory_item_id)
                    .with_for_update()
                )
                if (
                    inventory is None
                    or inventory.reserved_quantity < reservation.quantity
                    or inventory.on_hand_quantity < reservation.quantity
                ):
                    raise ConflictError(
                        "reserved_inventory_inconsistent",
                        "Reserved inventory is inconsistent; fulfillment remains blocked.",
                    )
                inventory.reserved_quantity -= reservation.quantity
                inventory.on_hand_quantity -= reservation.quantity
                reservation.status = ReservationStatus.CONSUMED

    def _subtract_checkout_from_cart(self, checkout: Checkout) -> None:
        if checkout.customer_id is None:
            return
        cart = self.db.scalar(
            select(Cart)
            .where(
                Cart.user_id == checkout.customer_id,
                Cart.merchant_id == checkout.merchant_id,
            )
            .options(selectinload(Cart.items))
        )
        if cart is None:
            return
        for line in checkout.lines:
            signature = self._modifier_signature(
                [str(modifier.modifier_option_id) for modifier in line.modifiers]
            )
            item = next(
                (
                    row
                    for row in cart.items
                    if row.variant_id == line.variant_id and row.modifier_signature == signature
                ),
                None,
            )
            if item is None:
                continue
            if item.quantity <= line.quantity:
                self.db.delete(item)
            else:
                item.quantity -= line.quantity

    def _checkout(
        self,
        checkout_id: uuid.UUID,
        customer_id: uuid.UUID,
        *,
        lock: bool = False,
    ) -> Checkout | None:
        statement = (
            select(Checkout)
            .where(Checkout.id == checkout_id, Checkout.customer_id == customer_id)
            .options(
                selectinload(Checkout.lines).selectinload(CheckoutLineItem.modifiers),
                selectinload(Checkout.lines).selectinload(CheckoutLineItem.reservations),
                selectinload(Checkout.fulfillment_options),
            )
        )
        if lock:
            statement = statement.with_for_update()
        return self.db.scalar(statement)

    def _order_and_payment(
        self, checkout_id: uuid.UUID, customer_id: uuid.UUID, *, lock: bool = False
    ) -> tuple[Order, Payment]:
        order_statement = select(Order).where(
            Order.checkout_id == checkout_id, Order.customer_id == customer_id
        )
        if lock:
            order_statement = order_statement.with_for_update()
        order = self.db.scalar(order_statement)
        if order is None:
            raise NotFoundError("order_not_found", "Payment order was not found.")
        payment_statement = select(Payment).where(
            Payment.order_id == order.id, Payment.provider == "razorpay"
        )
        if lock:
            payment_statement = payment_statement.with_for_update()
        payment = self.db.scalar(payment_statement)
        if payment is None or payment.provider_order_id is None:
            raise NotFoundError("payment_not_found", "Payment session was not found.")
        return order, payment

    def _records_by_provider_order(
        self, provider_order_id: str, *, lock: bool
    ) -> tuple[Payment, Order, Checkout]:
        statement = select(Payment).where(
            Payment.provider == "razorpay", Payment.provider_order_id == provider_order_id
        )
        if lock:
            statement = statement.with_for_update()
        payment = self.db.scalar(statement)
        if payment is None:
            raise NotFoundError("webhook_payment_not_found", "Webhook payment order was not found.")
        order = self.db.get(Order, payment.order_id)
        if order is None:
            raise NotFoundError("order_not_found", "Payment order was not found.")
        checkout = self._checkout(order.checkout_id, order.customer_id, lock=lock)
        if checkout is None:
            raise NotFoundError("checkout_not_found", "Checkout was not found.")
        return payment, order, checkout

    @staticmethod
    def _validate_created_order(
        provider_order: RazorpayOrder, checkout: Checkout, receipt: str
    ) -> None:
        if (
            provider_order.amount != checkout.total_minor
            or provider_order.currency != checkout.currency
            or provider_order.receipt != receipt
            or provider_order.status not in {"created", "attempted"}
        ):
            raise ConflictError(
                "razorpay_order_mismatch",
                "Razorpay order does not match the approved checkout.",
            )

    @staticmethod
    def _validate_captured_provider_state(
        payment: RazorpayPayment,
        order: RazorpayOrder,
        expected: dict[str, str | int],
    ) -> None:
        if (
            payment.id == ""
            or payment.order_id != expected["provider_order_id"]
            or payment.amount != expected["amount_minor"]
            or payment.currency != expected["currency"]
            or payment.status != "captured"
            or not payment.captured
            or order.id != expected["provider_order_id"]
            or order.amount != expected["amount_minor"]
            or order.amount_paid < expected["amount_minor"]
            or order.currency != expected["currency"]
            or order.status != "paid"
        ):
            raise ConflictError(
                "payment_not_captured",
                "Razorpay has not confirmed a captured payment and paid order.",
            )

    @staticmethod
    def _payment_expectation(payment: Payment) -> dict[str, str | int]:
        if payment.provider_order_id is None:
            raise NotFoundError("payment_not_found", "Payment session was not found.")
        return {
            "provider_order_id": payment.provider_order_id,
            "amount_minor": payment.amount_minor,
            "currency": payment.currency,
        }

    @staticmethod
    def _payment_from_entity(entity: dict) -> RazorpayPayment:
        try:
            return RazorpayPayment(
                id=str(entity["id"]),
                order_id=str(entity["order_id"]),
                amount=int(entity["amount"]),
                currency=str(entity["currency"]).upper(),
                status=str(entity["status"]),
                captured=bool(entity["captured"]),
                network_confirmation_id=next(
                    (
                        str((entity.get("acquirer_data") or {})[key])
                        for key in ("rrn", "upi_transaction_id", "auth_code")
                        if (entity.get("acquirer_data") or {}).get(key)
                    ),
                    None,
                ),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise DomainError(
                "invalid_webhook_payload", "Webhook payment entity is invalid.", 400
            ) from error

    @staticmethod
    def _webhook_payment_entity(payload: dict) -> dict:
        entity = payload.get("payload", {}).get("payment", {}).get("entity")
        if not isinstance(entity, dict):
            raise DomainError("invalid_webhook_payload", "Webhook payment entity is missing.", 400)
        return entity

    @staticmethod
    def _fulfillment_snapshot(checkout: Checkout) -> dict:
        option = next((item for item in checkout.fulfillment_options if item.selected), None)
        return {
            "type": checkout.fulfillment_type.value,
            "postal_code": checkout.postal_code,
            "delivery_address": checkout.delivery_address,
            "title": option.title if option else checkout.fulfillment_type.value,
            "eta_min_minutes": option.eta_min_minutes if option else None,
            "eta_max_minutes": option.eta_max_minutes if option else None,
        }

    def _release_reservations(self, checkout: Checkout) -> None:
        for line in checkout.lines:
            for reservation in line.reservations:
                if reservation.status != ReservationStatus.ACTIVE:
                    continue
                inventory = self.db.get(InventoryItem, reservation.inventory_item_id)
                if inventory is not None:
                    inventory.reserved_quantity = max(
                        0, inventory.reserved_quantity - reservation.quantity
                    )
                reservation.status = ReservationStatus.EXPIRED

    @staticmethod
    def _provider_receipt(checkout_id: uuid.UUID) -> str:
        return f"ab-{checkout_id.hex}"

    @staticmethod
    def _public_number(checkout_id: uuid.UUID) -> str:
        return f"EL-{checkout_id.hex.upper()}"

    @staticmethod
    def _modifier_signature(option_ids: list[str]) -> str:
        return hashlib.sha256(",".join(sorted(option_ids)).encode()).hexdigest()

    @staticmethod
    def _is_expired(expires_at: datetime) -> bool:
        comparable = expires_at if expires_at.tzinfo else expires_at.replace(tzinfo=UTC)
        return comparable <= utc_now()
