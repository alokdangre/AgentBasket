from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import ConflictError, DomainError, NotFoundError
from app.db.models import Merchant, PaymentInstrument, ScheduledPurchaseIntent, UserAccount
from app.domain.enums import PurchaseIntentStatus
from app.payments.razorpay import RazorpayOrder, RazorpayRecurringGateway
from app.protocols.ap2.autonomous import canonical_sha256
from app.schemas.trusted_surface import (
    RazorpayRecurringAuthorizationOut,
    RazorpayRecurringAuthorizationSessionOut,
    RazorpayRecurringAuthorizationVerify,
)

_AUTHORIZATION_AMOUNT_MINOR = 100


def _as_utc(value: datetime) -> datetime:
    return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)


class RazorpayRecurringAuthorizationService:
    """Register the provider mandate after AP2 bounds are signed by the user."""

    def __init__(self, db: Session) -> None:
        self.db = db
        self.settings = get_settings()

    def create_session(
        self,
        intent_id: uuid.UUID,
        customer: UserAccount,
        idempotency_key: str,
        gateway: RazorpayRecurringGateway,
    ) -> RazorpayRecurringAuthorizationSessionOut:
        self._require_enabled()
        intent, instrument, merchant = self._snapshot(intent_id, customer.id)
        if not customer.phone:
            raise ConflictError(
                "customer_contact_required",
                "Add a phone number before setting up UPI Autopay.",
            )
        if intent.status not in {
            PurchaseIntentStatus.PENDING_PROVIDER_AUTHORIZATION,
            PurchaseIntentStatus.NEEDS_ATTENTION,
        }:
            raise ConflictError(
                "scheduled_purchase_not_provider_pending",
                "Sign the AP2 schedule before setting up its recurring payment method.",
            )
        if instrument.instrument_type != "com.razorpay.upi.autopay":
            raise ConflictError(
                "upi_autopay_instrument_required",
                "Select the Razorpay UPI Autopay payment method for this schedule.",
            )
        request_sha256 = canonical_sha256(
            {
                "intent_id": str(intent.id),
                "customer_id": str(customer.id),
                "instrument_id": str(instrument.id),
                "max_amount_minor": intent.max_amount_minor,
                "expires_at": _as_utc(self._required_expiry(intent)).isoformat(),
            }
        )
        provider_customer_id = self._reserve_registration(
            intent,
            instrument,
            customer.id,
            idempotency_key,
            request_sha256,
        )
        if provider_customer_id is None:
            provider_customer = gateway.create_customer(
                name=customer.full_name,
                email=customer.email,
                contact=customer.phone,
                notes={"agentbasket_customer_id": str(customer.id)},
            )
            provider_customer_id = self._persist_customer(
                instrument.id, customer.id, provider_customer.id
            )

        receipt = self._receipt(intent.id, idempotency_key)
        provider_order = gateway.find_order_by_receipt(receipt)
        if provider_order is None:
            try:
                provider_order = gateway.create_mandate_order(
                    amount=_AUTHORIZATION_AMOUNT_MINOR,
                    currency=intent.currency,
                    receipt=receipt,
                    customer_id=provider_customer_id,
                    max_amount=intent.max_amount_minor,
                    frequency="as_presented",
                    expire_at=int(_as_utc(self._required_expiry(intent)).timestamp()),
                    notes={
                        "scheduled_purchase_id": str(intent.id),
                        "payment_instrument_id": str(instrument.id),
                    },
                )
            except DomainError:
                # A response can be lost after Razorpay creates the uniquely
                # receipted order. Reconcile it before exposing an error.
                provider_order = gateway.find_order_by_receipt(receipt)
                if provider_order is None:
                    raise
        self._validate_order(provider_order, intent.currency, receipt)
        self._persist_order(
            intent,
            instrument,
            provider_order,
            idempotency_key,
            request_sha256,
        )
        return RazorpayRecurringAuthorizationSessionOut(
            scheduled_purchase_id=intent.id,
            payment_instrument_id=instrument.id,
            key_id=gateway.key_id,
            provider_order_id=provider_order.id,
            provider_customer_id=provider_customer_id,
            amount_minor=_AUTHORIZATION_AMOUNT_MINOR,
            max_amount_minor=intent.max_amount_minor,
            currency=intent.currency,
            mandate_expires_at=_as_utc(self._required_expiry(intent)),
            merchant_name=merchant.name,
            description="Authorize bounded scheduled purchases with UPI Autopay",
            customer_name=customer.full_name,
            customer_email=customer.email,
            customer_phone=customer.phone,
        )

    def verify(
        self,
        intent_id: uuid.UUID,
        customer: UserAccount,
        payload: RazorpayRecurringAuthorizationVerify,
        gateway: RazorpayRecurringGateway,
    ) -> RazorpayRecurringAuthorizationOut:
        self._require_enabled()
        intent, instrument, _ = self._snapshot(intent_id, customer.id)
        metadata = dict(instrument.instrument_metadata or {})
        expected_order_id = str(metadata.get("registration_order_id") or "")
        if not expected_order_id or payload.razorpay_order_id != expected_order_id:
            raise ConflictError(
                "recurring_authorization_order_mismatch",
                "The recurring authorization does not match this schedule.",
            )
        if not gateway.verify_checkout_signature(
            order_id=expected_order_id,
            payment_id=payload.razorpay_payment_id,
            signature=payload.razorpay_signature,
        ):
            raise DomainError(
                "invalid_payment_signature",
                "Razorpay recurring authorization verification failed.",
                400,
            )
        provider_payment = gateway.fetch_payment(payload.razorpay_payment_id)
        if (
            provider_payment.id != payload.razorpay_payment_id
            or provider_payment.order_id != expected_order_id
            or provider_payment.amount != _AUTHORIZATION_AMOUNT_MINOR
            or provider_payment.currency.upper() != intent.currency.upper()
            or provider_payment.status not in {"authorized", "captured"}
        ):
            raise ConflictError(
                "recurring_authorization_state_mismatch",
                "Razorpay has not confirmed the expected mandate authorization payment.",
            )
        status = "token_confirmation_pending"
        token_confirmation_pending = True
        with self.db.begin():
            locked = self.db.scalar(
                select(PaymentInstrument)
                .where(
                    PaymentInstrument.id == instrument.id,
                    PaymentInstrument.user_id == customer.id,
                )
                .with_for_update()
            )
            if locked is None:
                raise NotFoundError("payment_instrument_not_found", "Payment method was not found.")
            current = dict(locked.instrument_metadata or {})
            if not provider_payment.token_id:
                raise ConflictError(
                    "recurring_authorization_token_missing",
                    "Razorpay has not returned the recurring token for this authorization.",
                )
            existing_token = locked.provider_token_reference
            existing_status = str(current.get("token_status") or "")
            if existing_status == "confirmed":
                if existing_token != provider_payment.token_id:
                    raise ConflictError(
                        "recurring_token_replacement_not_supported",
                        (
                            "This payment method already has a different confirmed token. "
                            "Cancel it before registering a replacement."
                        ),
                    )
                current.update(
                    {
                        "registration_status": "confirmed",
                        "registration_payment_id": provider_payment.id,
                    }
                )
                status = "confirmed"
                token_confirmation_pending = False
            else:
                locked.provider_token_reference = provider_payment.token_id
                current.update(
                    {
                        "registration_status": "token_confirmation_pending",
                        "registration_payment_id": provider_payment.id,
                        "token_status": "pending",
                    }
                )
            current["token_id_sha256"] = hashlib.sha256(
                provider_payment.token_id.encode()
            ).hexdigest()
            locked.instrument_metadata = current
        return RazorpayRecurringAuthorizationOut(
            scheduled_purchase_id=intent.id,
            payment_instrument_id=instrument.id,
            status=status,
            provider_payment_id=provider_payment.id,
            token_confirmation_pending=token_confirmation_pending,
        )

    def _reserve_registration(
        self,
        intent: ScheduledPurchaseIntent,
        instrument: PaymentInstrument,
        customer_id: uuid.UUID,
        idempotency_key: str,
        request_sha256: str,
    ) -> str | None:
        """Reserve one provider registration before any external side effect."""
        with self.db.begin():
            locked = self.db.scalar(
                select(PaymentInstrument)
                .where(
                    PaymentInstrument.id == instrument.id,
                    PaymentInstrument.user_id == customer_id,
                )
                .with_for_update()
            )
            if locked is None:
                raise NotFoundError("payment_instrument_not_found", "Payment method was not found.")
            metadata = dict(locked.instrument_metadata or {})
            if metadata.get("token_status") == "confirmed":
                raise ConflictError(
                    "recurring_payment_already_authorized",
                    (
                        "This UPI Autopay method already has a confirmed token. "
                        "Use it only when its stored bounds cover the schedule."
                    ),
                )
            existing_status = str(metadata.get("registration_status") or "")
            existing_intent = str(metadata.get("registration_intent_id") or "")
            existing_key = str(metadata.get("registration_idempotency_key") or "")
            existing_hash = str(metadata.get("registration_request_sha256") or "")
            in_progress = existing_status in {
                "provider_customer_required",
                "provider_order_required",
                "checkout_required",
                "token_confirmation_pending",
            }
            if in_progress and (
                existing_intent != str(intent.id)
                or existing_key != idempotency_key
                or existing_hash != request_sha256
            ):
                if existing_key == idempotency_key and existing_hash != request_sha256:
                    raise ConflictError(
                        "idempotency_key_reused",
                        "Idempotency-Key was used for another recurring authorization.",
                    )
                raise ConflictError(
                    "recurring_registration_in_progress",
                    "Finish the existing UPI Autopay setup before starting another.",
                )
            if not in_progress:
                metadata.update(
                    {
                        "registration_intent_id": str(intent.id),
                        "registration_idempotency_key": idempotency_key,
                        "registration_request_sha256": request_sha256,
                        "registration_status": (
                            "provider_order_required"
                            if locked.provider_customer_id
                            else "provider_customer_required"
                        ),
                    }
                )
                locked.instrument_metadata = metadata
            return locked.provider_customer_id

    def _snapshot(
        self, intent_id: uuid.UUID, customer_id: uuid.UUID
    ) -> tuple[ScheduledPurchaseIntent, PaymentInstrument, Merchant]:
        intent = self.db.scalar(
            select(ScheduledPurchaseIntent).where(
                ScheduledPurchaseIntent.id == intent_id,
                ScheduledPurchaseIntent.customer_id == customer_id,
            )
        )
        if intent is None:
            raise NotFoundError("scheduled_purchase_not_found", "Scheduled purchase was not found.")
        instrument = self.db.scalar(
            select(PaymentInstrument).where(
                PaymentInstrument.id == intent.payment_instrument_id,
                PaymentInstrument.user_id == customer_id,
                PaymentInstrument.status == "active",
            )
        )
        merchant = self.db.get(Merchant, intent.merchant_id)
        if instrument is None or merchant is None:
            raise NotFoundError(
                "scheduled_purchase_dependency_missing",
                "Merchant or payment method was not found.",
            )
        self.db.expunge(intent)
        self.db.expunge(instrument)
        self.db.expunge(merchant)
        self.db.rollback()
        return intent, instrument, merchant

    def _persist_customer(
        self, instrument_id: uuid.UUID, customer_id: uuid.UUID, provider_customer_id: str
    ) -> str:
        with self.db.begin():
            instrument = self.db.scalar(
                select(PaymentInstrument)
                .where(
                    PaymentInstrument.id == instrument_id,
                    PaymentInstrument.user_id == customer_id,
                )
                .with_for_update()
            )
            if instrument is None:
                raise NotFoundError("payment_instrument_not_found", "Payment method was not found.")
            if instrument.provider_customer_id is None:
                instrument.provider_customer_id = provider_customer_id
            metadata = dict(instrument.instrument_metadata or {})
            metadata["registration_status"] = "provider_order_required"
            instrument.instrument_metadata = metadata
            return instrument.provider_customer_id

    def _persist_order(
        self,
        intent: ScheduledPurchaseIntent,
        instrument: PaymentInstrument,
        order: RazorpayOrder,
        idempotency_key: str,
        request_sha256: str,
    ) -> None:
        with self.db.begin():
            locked = self.db.scalar(
                select(PaymentInstrument)
                .where(PaymentInstrument.id == instrument.id)
                .with_for_update()
            )
            if locked is None:
                raise NotFoundError("payment_instrument_not_found", "Payment method was not found.")
            metadata = dict(locked.instrument_metadata or {})
            existing_intent = metadata.get("registration_intent_id")
            if existing_intent != str(intent.id):
                raise ConflictError(
                    "recurring_registration_in_progress",
                    "Finish the existing UPI Autopay setup before starting another.",
                )
            if (
                metadata.get("registration_idempotency_key") != idempotency_key
                or metadata.get("registration_request_sha256") != request_sha256
            ):
                raise ConflictError(
                    "recurring_registration_reservation_mismatch",
                    "The recurring authorization reservation changed before order creation.",
                )
            metadata.update(
                {
                    "registration_intent_id": str(intent.id),
                    "registration_idempotency_key": idempotency_key,
                    "registration_request_sha256": request_sha256,
                    "registration_order_id": order.id,
                    "registration_status": "checkout_required",
                    "token_status": "not_confirmed",
                    "mandate_max_amount_minor": intent.max_amount_minor,
                    "mandate_expires_at": int(_as_utc(self._required_expiry(intent)).timestamp()),
                    "mandate_frequency": "as_presented",
                }
            )
            locked.instrument_metadata = metadata

    @staticmethod
    def _validate_order(order: RazorpayOrder, currency: str, receipt: str) -> None:
        if (
            order.amount != _AUTHORIZATION_AMOUNT_MINOR
            or order.currency.upper() != currency.upper()
            or order.receipt != receipt
            or order.status not in {"created", "attempted"}
        ):
            raise ConflictError(
                "razorpay_mandate_order_mismatch",
                "Razorpay mandate order does not match the signed schedule.",
            )

    def _require_enabled(self) -> None:
        if not self.settings.razorpay_recurring_enabled:
            raise DomainError(
                "razorpay_recurring_not_enabled",
                "Razorpay recurring payments are not enabled for this environment.",
                503,
            )

    @staticmethod
    def _required_expiry(intent: ScheduledPurchaseIntent) -> datetime:
        if intent.expires_at is None:
            raise ConflictError("scheduled_purchase_expiry_missing", "Schedule expiry is missing.")
        return intent.expires_at

    @staticmethod
    def _receipt(intent_id: uuid.UUID, idempotency_key: str) -> str:
        suffix = hashlib.sha256(idempotency_key.encode()).hexdigest()[:8]
        return f"abm-{intent_id.hex[:24]}-{suffix}"
