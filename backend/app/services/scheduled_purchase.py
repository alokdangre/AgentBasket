from __future__ import annotations

import calendar
import hashlib
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.core.config import get_settings
from app.core.errors import ConflictError, DomainError, NotFoundError
from app.db.models import (
    AuditEvent,
    Checkout,
    CustomerAddress,
    Merchant,
    Payment,
    PaymentInstrument,
    Product,
    ProductVariant,
    ScheduledPurchaseIntent,
    ScheduledPurchaseRun,
    UserAccount,
)
from app.domain.enums import (
    FulfillmentType,
    ProductStatus,
    PurchaseIntentStatus,
    ScheduledRunStatus,
)
from app.domain.fulfillment import supports_fulfillment
from app.protocols.ap2.autonomous import (
    canonical_sha256,
    create_open_mandates,
    derive_agent_key,
    public_jwk,
    scheduled_policy_sha256,
)
from app.protocols.ap2.crypto import AP2KeySet, get_ap2_key_set
from app.schemas.scheduled_purchase import (
    ScheduledPurchaseActionOut,
    ScheduledPurchaseAuthorizationCreate,
    ScheduledPurchaseAuthorizationOut,
    ScheduledPurchaseChallengeOut,
    ScheduledPurchaseDraftCreate,
    ScheduledPurchaseListOut,
    ScheduledPurchaseOut,
    ScheduledPurchaseRunOut,
)
from app.services.checkout import CheckoutService
from app.services.credentials_provider import recurring_instrument_ready
from app.services.location import LocationService
from app.services.trusted_surface import TrustedSurfaceService


def utc_now() -> datetime:
    return datetime.now(UTC)


def as_utc(value: datetime) -> datetime:
    return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)


AUTONOMOUS_PAYMENT_INSTRUMENT_TYPE = "com.razorpay.upi.autopay"


def advance_schedule(
    value: datetime,
    frequency: str,
    interval_count: int,
    timezone: str,
    *,
    anchor: datetime | None = None,
) -> datetime | None:
    if frequency == "once":
        return None
    zone = ZoneInfo(timezone)
    local = as_utc(value).astimezone(zone)
    if frequency == "daily":
        return (local + timedelta(days=interval_count)).astimezone(UTC)
    if frequency == "weekly":
        return (local + timedelta(weeks=interval_count)).astimezone(UTC)
    if frequency != "monthly":
        raise ValueError("Unsupported schedule frequency")
    anchor_local = as_utc(anchor or value).astimezone(zone)
    anchor_month_index = anchor_local.year * 12 + anchor_local.month - 1
    current_month_index = local.year * 12 + local.month - 1
    completed_intervals = max(0, (current_month_index - anchor_month_index) // interval_count)
    month_index = anchor_month_index + (completed_intervals + 1) * interval_count
    year, month_zero = divmod(month_index, 12)
    month = month_zero + 1
    day = min(anchor_local.day, calendar.monthrange(year, month)[1])
    return anchor_local.replace(year=year, month=month, day=day).astimezone(UTC)


class ScheduledPurchaseService:
    def __init__(
        self,
        db: Session,
        keys: AP2KeySet | None = None,
        trusted_surface: TrustedSurfaceService | None = None,
    ) -> None:
        self.db = db
        self.settings = get_settings()
        self._keys = keys
        self.trusted_surface = trusted_surface or TrustedSurfaceService(db)
        self.locations = LocationService(db)

    @property
    def keys(self) -> AP2KeySet:
        if self._keys is None:
            self._keys = get_ap2_key_set()
        return self._keys

    def create_draft(
        self,
        payload: ScheduledPurchaseDraftCreate,
        customer: UserAccount,
        idempotency_key: str,
    ) -> ScheduledPurchaseOut:
        now = utc_now()
        first_run = as_utc(payload.first_run_at)
        expires_at = as_utc(payload.expires_at)
        if first_run <= now + timedelta(minutes=5):
            raise DomainError(
                "scheduled_purchase_too_soon",
                "The first purchase must be at least five minutes in the future.",
                422,
            )
        notification_lead = timedelta(
            hours=self.settings.razorpay_recurring_notification_lead_hours
        )
        if first_run <= now + notification_lead:
            raise DomainError(
                "scheduled_notification_window_too_short",
                (
                    "The first purchase must leave at least "
                    f"{self.settings.razorpay_recurring_notification_lead_hours} hours "
                    "for the Razorpay Autopay pre-debit notification."
                ),
                422,
            )
        if payload.max_amount_minor > self.settings.razorpay_recurring_unattended_limit_minor:
            raise DomainError(
                "scheduled_unattended_cap_exceeded",
                (
                    "The per-order cap exceeds this merchant's unattended Razorpay "
                    "Autopay limit. Lower the cap or use human-present checkout."
                ),
                422,
            )
        try:
            ZoneInfo(payload.timezone)
        except ZoneInfoNotFoundError as error:
            raise DomainError("invalid_timezone", "Use a valid IANA timezone.", 422) from error

        request_sha256 = canonical_sha256(
            {
                "customer_id": str(customer.id),
                "scheduled_purchase": payload.model_dump(mode="json"),
            }
        )
        with self.db.begin():
            existing = self.db.scalar(
                select(ScheduledPurchaseIntent).where(
                    ScheduledPurchaseIntent.customer_id == customer.id,
                    ScheduledPurchaseIntent.idempotency_key == idempotency_key,
                )
            )
            if existing is not None:
                if existing.request_sha256 != request_sha256:
                    raise ConflictError(
                        "idempotency_key_reused",
                        "Idempotency-Key was already used for another schedule.",
                    )
                return self._out_with_lookups(existing)
            merchant = self.locations.merchant_by_slug(payload.merchant_slug)
            if payload.currency.upper() != merchant.currency.upper():
                raise DomainError(
                    "currency_mismatch", "Schedule currency must match the merchant.", 422
                )
            instrument = self._instrument(customer.id, payload.payment_instrument_id)
            self._require_autonomous_instrument(instrument)
            address, address_snapshot, address_sha256, location_id, delivery_fee = (
                self._destination(payload, customer, merchant)
            )
            constraints, display_items, estimated_max = self._item_policy(
                payload, merchant, delivery_fee
            )
            if estimated_max > payload.max_amount_minor:
                raise DomainError(
                    "scheduled_order_cap_too_low",
                    "The current maximum item and delivery price exceeds the per-order cap.",
                    422,
                )
            intent_id = uuid.uuid4()
            strict_constraints = {
                "version": 1,
                "merchant_id": str(merchant.id),
                "merchant_name": merchant.name,
                "items": constraints,
                "fulfillment_type": payload.fulfillment_type.value,
                "address_sha256": address_sha256,
                "location_id": str(location_id),
                "frequency": payload.frequency,
                "interval_count": payload.interval_count,
                "timezone": payload.timezone,
                "first_run_at": first_run.isoformat(),
                "expires_at": expires_at.isoformat(),
                "max_occurrences": payload.max_occurrences,
                "max_amount_minor": payload.max_amount_minor,
                "max_total_minor": payload.max_total_minor,
                "currency": merchant.currency,
            }
            display = {
                "intent_id": str(intent_id),
                "merchant": {"id": str(merchant.id), "name": merchant.name},
                "items": display_items,
                "fulfillment": {
                    "type": payload.fulfillment_type.value,
                    "address": address_snapshot,
                    "location_id": str(location_id),
                    "address_sha256": address_sha256,
                },
                "schedule": {
                    "frequency": payload.frequency,
                    "interval_count": payload.interval_count,
                    "timezone": payload.timezone,
                    "first_run_at": first_run.isoformat(),
                    "expires_at": expires_at.isoformat(),
                    "max_occurrences": payload.max_occurrences,
                },
                "payment": {
                    "instrument_id": str(instrument.id),
                    "instrument_alias": instrument.alias,
                    "provider": instrument.provider,
                    "per_order_cap_minor": payload.max_amount_minor,
                    "total_budget_minor": payload.max_total_minor,
                    "currency": merchant.currency,
                    "provider_authorization_required": not self._provider_ready(instrument),
                },
                "human_not_present": True,
                "authorization": {
                    "scheduled_policy_sha256": scheduled_policy_sha256(strict_constraints)
                },
            }
            intent = ScheduledPurchaseIntent(
                id=intent_id,
                merchant_id=merchant.id,
                customer_id=customer.id,
                customer_reference=str(customer.id),
                idempotency_key=idempotency_key,
                request_sha256=request_sha256,
                address_id=address.id if address else None,
                payment_instrument_id=instrument.id,
                location_id=location_id,
                status=PurchaseIntentStatus.DRAFT,
                constraints=strict_constraints,
                fulfillment_type=payload.fulfillment_type,
                frequency=payload.frequency,
                interval_count=payload.interval_count,
                timezone=payload.timezone,
                max_occurrences=payload.max_occurrences,
                max_amount_minor=payload.max_amount_minor,
                max_total_minor=payload.max_total_minor,
                currency=merchant.currency,
                address_snapshot=address_snapshot,
                address_sha256=address_sha256,
                display_payload=display,
                display_sha256=canonical_sha256(display),
                next_execution_at=first_run,
                expires_at=expires_at,
            )
            self.db.add(intent)
            self.db.flush()
            self._audit(
                intent,
                "scheduled_purchase.drafted",
                "customer",
                str(customer.id),
                {
                    "display_sha256": intent.display_sha256,
                    "first_run_at": first_run.isoformat(),
                    "provider_ready": self._provider_ready(instrument),
                },
            )
            return self._out(intent, merchant, instrument)

    def list(self, customer: UserAccount) -> ScheduledPurchaseListOut:
        rows = list(
            self.db.scalars(
                select(ScheduledPurchaseIntent)
                .where(ScheduledPurchaseIntent.customer_id == customer.id)
                .order_by(ScheduledPurchaseIntent.created_at.desc())
            )
        )
        output = [self._out_with_lookups(row) for row in rows]
        self.db.rollback()
        return ScheduledPurchaseListOut(scheduled_purchases=output)

    def get(self, intent_id: uuid.UUID, customer: UserAccount) -> ScheduledPurchaseOut:
        intent = self._owned(intent_id, customer.id)
        output = self._out_with_lookups(intent, include_runs=True)
        self.db.rollback()
        return output

    def create_authorization_challenge(
        self,
        intent_id: uuid.UUID,
        customer: UserAccount,
        idempotency_key: str,
    ) -> ScheduledPurchaseChallengeOut:
        master_key = self._master_key()
        expired = False
        request_sha256 = canonical_sha256(
            {"intent_id": str(intent_id), "customer_id": str(customer.id)}
        )
        with self.db.begin():
            intent = self._owned(intent_id, customer.id, lock=True)
            if (
                intent.status == PurchaseIntentStatus.DRAFT
                and as_utc(intent.expires_at) <= utc_now()
            ):
                intent.status = PurchaseIntentStatus.EXPIRED
                intent.next_run_at = None
                intent.authorization_nonce = None
                intent.webauthn_challenge = None
                intent.authorization_challenge_expires_at = None
                self._audit(
                    intent,
                    "scheduled_purchase.expired",
                    "system",
                    "authorization_challenge",
                    {"reason": "schedule_expired"},
                )
                expired = True
            elif intent.authorization_challenge_idempotency_key == idempotency_key:
                if intent.authorization_challenge_request_sha256 != request_sha256:
                    raise ConflictError(
                        "idempotency_key_reused",
                        "Idempotency-Key was used for another authorization challenge.",
                    )
                if (
                    intent.authorization_nonce is None
                    or intent.webauthn_challenge is None
                    or intent.authorization_challenge_expires_at is None
                ):
                    raise ConflictError(
                        "scheduled_authorization_consumed",
                        "This authorization challenge was already consumed.",
                    )
                _, options = self.trusted_surface.authentication_options(
                    customer, intent.webauthn_challenge
                )
                return self._challenge_out(intent, options)
            elif intent.status != PurchaseIntentStatus.DRAFT:
                raise ConflictError(
                    "scheduled_purchase_not_draft",
                    "Only a draft schedule can be authorized.",
                )
            else:
                instrument = self.db.get(PaymentInstrument, intent.payment_instrument_id)
                if instrument is None:
                    raise NotFoundError(
                        "payment_instrument_not_found", "Payment method was not found."
                    )
                self._require_autonomous_instrument(instrument)
                agent_key = derive_agent_key(master_key, intent.id)
                challenge, options = self.trusted_surface.authentication_options(customer)
                intent.authorization_nonce = secrets.token_urlsafe(32)
                intent.webauthn_challenge = challenge
                intent.authorization_challenge_idempotency_key = idempotency_key
                intent.authorization_challenge_request_sha256 = request_sha256
                intent.authorization_challenge_expires_at = utc_now() + timedelta(
                    seconds=self.settings.webauthn_challenge_ttl_seconds
                )
                intent.agent_key_id = str(agent_key.get("kid"))
                intent.agent_public_jwk = public_jwk(agent_key)
                self.db.flush()
                return self._challenge_out(intent, options)
        if expired:
            raise ConflictError("scheduled_purchase_expired", "This schedule has expired.")
        raise RuntimeError("Scheduled authorization challenge reached an invalid state.")

    def authorize(
        self,
        intent_id: uuid.UUID,
        payload: ScheduledPurchaseAuthorizationCreate,
        customer: UserAccount,
        idempotency_key: str,
    ) -> ScheduledPurchaseAuthorizationOut:
        master_key = self._master_key()
        request_sha256 = canonical_sha256(payload.model_dump(mode="json"))
        with self.db.begin():
            intent = self._owned(intent_id, customer.id, lock=True)
            if intent.status != PurchaseIntentStatus.DRAFT:
                if (
                    intent.open_checkout_mandate
                    and intent.open_payment_mandate
                    and intent.approval_idempotency_key == idempotency_key
                    and intent.approval_request_sha256 == request_sha256
                ):
                    return self._authorization_out(intent)
                raise ConflictError(
                    "scheduled_authorization_consumed",
                    "This schedule authorization was already consumed.",
                )
            challenge_expiry = intent.authorization_challenge_expires_at
            if (
                intent.webauthn_challenge is None
                or intent.authorization_nonce is None
                or challenge_expiry is None
                or as_utc(challenge_expiry) <= utc_now()
            ):
                raise ConflictError(
                    "scheduled_authorization_expired",
                    "The passkey authorization challenge expired. Prepare it again.",
                )
            if not secrets.compare_digest(
                payload.nonce, intent.authorization_nonce
            ) or not secrets.compare_digest(
                payload.display_sha256,
                self._required(intent.display_sha256, "display hash"),
            ):
                raise ConflictError(
                    "scheduled_authorization_terms_mismatch",
                    "The displayed schedule does not match the authoritative terms.",
                )
            current_policy_sha256 = scheduled_policy_sha256(intent.constraints)
            display_authorization = (intent.display_payload or {}).get("authorization")
            displayed_policy_sha256 = (
                display_authorization.get("scheduled_policy_sha256")
                if isinstance(display_authorization, dict)
                else None
            )
            if not isinstance(displayed_policy_sha256, str) or not secrets.compare_digest(
                displayed_policy_sha256, current_policy_sha256
            ):
                raise ConflictError(
                    "scheduled_authorization_policy_mismatch",
                    "The schedule policy changed after it was displayed.",
                )
            self.trusted_surface.verify_authentication(
                customer,
                intent.webauthn_challenge,
                payload.webauthn_credential,
            )
            merchant = self.db.get(Merchant, intent.merchant_id)
            instrument = self.db.get(PaymentInstrument, intent.payment_instrument_id)
            if merchant is None or instrument is None:
                raise NotFoundError(
                    "scheduled_purchase_dependency_missing",
                    "Merchant or payment instrument was not found.",
                )
            self._require_autonomous_instrument(instrument)
            agent_key = derive_agent_key(master_key, intent.id)
            if public_jwk(agent_key) != intent.agent_public_jwk:
                raise ConflictError(
                    "scheduled_agent_key_mismatch", "The delegated agent key changed."
                )
            bundle = create_open_mandates(
                keys=self.keys,
                agent_key=agent_key,
                merchant_id=str(merchant.id),
                merchant_name=merchant.name,
                item_requirements=[
                    {
                        "id": item["id"],
                        "acceptable_items": item["acceptable_items"],
                        "quantity": item["quantity"],
                    }
                    for item in intent.constraints["items"]
                ],
                payment_instrument_id=str(instrument.id),
                payment_instrument_type=instrument.instrument_type,
                currency=intent.currency,
                max_amount_minor=intent.max_amount_minor,
                max_total_minor=intent.max_total_minor,
                frequency=intent.frequency,
                max_occurrences=intent.max_occurrences,
                first_run_at=as_utc(self._schedule_anchor(intent)),
                expires_at=as_utc(intent.expires_at),
                strict_constraints=intent.constraints,
            )
            intent.open_checkout_mandate = bundle.checkout_token
            intent.open_payment_mandate = bundle.payment_token
            intent.open_checkout_hash = bundle.checkout_hash
            intent.authorization_reference = hashlib.sha256(
                f"{bundle.checkout_token}:{bundle.payment_token}".encode()
            ).hexdigest()
            intent.approval_idempotency_key = idempotency_key
            intent.approval_request_sha256 = request_sha256
            intent.authorized_at = utc_now()
            intent.webauthn_challenge = None
            intent.authorization_challenge_expires_at = None
            provider_ready = self._provider_ready(instrument, intent)
            if provider_ready and self._has_notification_lead(intent.next_execution_at):
                intent.status = PurchaseIntentStatus.ACTIVE
                intent.provider_authorized_at = utc_now()
                intent.payment_token_reference = instrument.provider_token_reference
                intent.next_run_at = self._notification_at(intent.next_execution_at)
            elif provider_ready:
                intent.status = PurchaseIntentStatus.NEEDS_ATTENTION
                intent.last_failure_code = "provider_notification_window_missed"
                intent.last_failure_message = (
                    "Razorpay Autopay needs advance notification; choose a later first run."
                )
            else:
                intent.status = PurchaseIntentStatus.PENDING_PROVIDER_AUTHORIZATION
                intent.next_run_at = None
            self._audit(
                intent,
                "scheduled_purchase.authorized",
                "trusted_surface",
                str(customer.id),
                {
                    "authorization_reference": intent.authorization_reference,
                    "open_checkout_hash": intent.open_checkout_hash,
                    "scheduled_policy_sha256": bundle.policy_sha256,
                    "agent_key_id": intent.agent_key_id,
                    "status": intent.status.value,
                },
            )
            self.db.flush()
            return self._authorization_out(intent, merchant, instrument)

    def pause(self, intent_id: uuid.UUID, customer: UserAccount) -> ScheduledPurchaseActionOut:
        with self.db.begin():
            intent = self._owned(intent_id, customer.id, lock=True)
            if intent.status == PurchaseIntentStatus.PAUSED:
                return self._action_out(intent)
            if intent.status not in {
                PurchaseIntentStatus.ACTIVE,
                PurchaseIntentStatus.NEEDS_ATTENTION,
                PurchaseIntentStatus.PENDING_PROVIDER_AUTHORIZATION,
            }:
                raise ConflictError(
                    "scheduled_purchase_not_pausable",
                    f"A {intent.status.value} schedule cannot be paused.",
                )
            intent.status = PurchaseIntentStatus.PAUSED
            intent.paused_at = utc_now()
            intent.next_run_at = None
            self._audit(intent, "scheduled_purchase.paused", "customer", str(customer.id), {})
            return self._action_out(intent)

    def resume(self, intent_id: uuid.UUID, customer: UserAccount) -> ScheduledPurchaseActionOut:
        with self.db.begin():
            intent = self._owned(intent_id, customer.id, lock=True)
            if intent.status == PurchaseIntentStatus.ACTIVE:
                return self._action_out(intent)
            if intent.status not in {
                PurchaseIntentStatus.PAUSED,
                PurchaseIntentStatus.NEEDS_ATTENTION,
            }:
                raise ConflictError(
                    "scheduled_purchase_not_resumable",
                    "Only a paused or attention-required schedule can be resumed.",
                )
            if not intent.open_checkout_mandate or not intent.open_payment_mandate:
                raise ConflictError(
                    "scheduled_purchase_not_authorized", "Authorize the schedule before resuming."
                )
            if intent.successful_occurrences >= intent.max_occurrences:
                intent.status = PurchaseIntentStatus.COMPLETED
                intent.next_execution_at = None
                intent.next_run_at = None
                return self._action_out(intent)
            if as_utc(intent.expires_at) <= utc_now():
                intent.status = PurchaseIntentStatus.EXPIRED
                intent.next_execution_at = None
                intent.next_run_at = None
                return self._action_out(intent)
            instrument = self.db.get(PaymentInstrument, intent.payment_instrument_id)
            if instrument is None or not self._provider_ready(instrument, intent):
                intent.status = PurchaseIntentStatus.PENDING_PROVIDER_AUTHORIZATION
                intent.next_run_at = None
            elif intent.next_execution_at is None:
                intent.status = PurchaseIntentStatus.NEEDS_ATTENTION
                intent.next_run_at = None
                intent.last_failure_code = "schedule_has_no_future_occurrence"
                intent.last_failure_message = "This schedule has no future occurrence to resume."
            else:
                next_execution = self._next_recoverable_execution(intent, intent.next_execution_at)
                if next_execution is None:
                    intent.status = PurchaseIntentStatus.NEEDS_ATTENTION
                    intent.next_run_at = None
                    intent.last_failure_code = "schedule_time_passed"
                    intent.last_failure_message = (
                        "No future occurrence has enough provider notification lead time."
                    )
                else:
                    intent.status = PurchaseIntentStatus.ACTIVE
                    intent.provider_authorized_at = intent.provider_authorized_at or utc_now()
                    intent.payment_token_reference = instrument.provider_token_reference
                    intent.paused_at = None
                    intent.next_execution_at = next_execution
                    intent.next_run_at = self._notification_at(next_execution)
                    intent.last_failure_code = None
                    intent.last_failure_message = None
            self._audit(
                intent,
                "scheduled_purchase.resumed",
                "customer",
                str(customer.id),
                {"status": intent.status.value},
            )
            return self._action_out(intent)

    def revoke(self, intent_id: uuid.UUID, customer: UserAccount) -> ScheduledPurchaseActionOut:
        with self.db.begin():
            intent = self._owned(intent_id, customer.id, lock=True)
            if intent.status in {
                PurchaseIntentStatus.COMPLETED,
                PurchaseIntentStatus.EXPIRED,
                PurchaseIntentStatus.REVOKED,
            }:
                return self._action_out(intent)
            intent.status = PurchaseIntentStatus.REVOKED
            intent.revoked_at = utc_now()
            intent.next_run_at = None
            self._audit(intent, "scheduled_purchase.revoked", "customer", str(customer.id), {})
            return self._action_out(intent)

    def merchant_list(self, merchant_user: UserAccount) -> list[ScheduledPurchaseOut]:
        rows = list(
            self.db.scalars(
                select(ScheduledPurchaseIntent)
                .where(
                    ScheduledPurchaseIntent.merchant_id == merchant_user.merchant_id,
                    ScheduledPurchaseIntent.customer_id.is_not(None),
                    ScheduledPurchaseIntent.payment_instrument_id.is_not(None),
                    ScheduledPurchaseIntent.location_id.is_not(None),
                    ScheduledPurchaseIntent.fulfillment_type.is_not(None),
                    ScheduledPurchaseIntent.display_payload.is_not(None),
                    ScheduledPurchaseIntent.display_sha256.is_not(None),
                    ScheduledPurchaseIntent.expires_at.is_not(None),
                )
                .order_by(ScheduledPurchaseIntent.created_at.desc())
                .limit(200)
            )
        )
        output = [self._out_with_lookups(row, include_runs=True) for row in rows]
        self.db.rollback()
        return output

    def activate_confirmed_instrument(self, instrument: PaymentInstrument) -> int:
        """Called by a verified token webhook; caller owns the transaction."""
        rows = list(
            self.db.scalars(
                select(ScheduledPurchaseIntent)
                .where(
                    ScheduledPurchaseIntent.payment_instrument_id == instrument.id,
                    ScheduledPurchaseIntent.status
                    == PurchaseIntentStatus.PENDING_PROVIDER_AUTHORIZATION,
                )
                .with_for_update()
            )
        )
        activated = 0
        for intent in rows:
            if not self._provider_ready(instrument, intent):
                intent.status = PurchaseIntentStatus.NEEDS_ATTENTION
                intent.last_failure_code = "provider_authorization_bounds_mismatch"
                intent.last_failure_message = (
                    "The confirmed provider mandate does not cover this schedule's bounds."
                )
                continue
            if (
                intent.next_execution_at is None
                or as_utc(intent.next_execution_at) <= utc_now()
                or not self._has_notification_lead(intent.next_execution_at)
            ):
                intent.status = PurchaseIntentStatus.NEEDS_ATTENTION
                intent.last_failure_code = "provider_notification_window_missed"
                intent.last_failure_message = "Choose a later first execution time."
                continue
            intent.status = PurchaseIntentStatus.ACTIVE
            intent.provider_authorized_at = utc_now()
            intent.payment_token_reference = instrument.provider_token_reference
            intent.next_run_at = self._notification_at(intent.next_execution_at)
            self._audit(
                intent,
                "scheduled_purchase.provider_authorized",
                "credentials_provider",
                instrument.provider,
                {"instrument_id": str(instrument.id)},
            )
            activated += 1
        return activated

    def deactivate_recurring_instrument(
        self, instrument: PaymentInstrument, provider_status: str
    ) -> int:
        """Stop future executions after a verified provider token state change."""
        rows = list(
            self.db.scalars(
                select(ScheduledPurchaseIntent)
                .where(
                    ScheduledPurchaseIntent.payment_instrument_id == instrument.id,
                    ScheduledPurchaseIntent.status == PurchaseIntentStatus.ACTIVE,
                )
                .with_for_update()
            )
        )
        for intent in rows:
            intent.status = PurchaseIntentStatus.PENDING_PROVIDER_AUTHORIZATION
            intent.next_run_at = None
            intent.last_failure_code = f"provider_token_{provider_status}"[:120]
            intent.last_failure_message = (
                f"Razorpay reported the recurring authorization as {provider_status}."
            )
            self._audit(
                intent,
                "scheduled_purchase.provider_authorization_changed",
                "credentials_provider",
                instrument.provider,
                {
                    "instrument_id": str(instrument.id),
                    "provider_status": provider_status,
                },
            )
        return len(rows)

    def complete_payment(
        self, checkout: Checkout, payment: Payment, *, provider_payment_id: str
    ) -> None:
        """Complete schedule usage inside the caller's payment transaction."""
        run = self.db.scalar(
            select(ScheduledPurchaseRun)
            .where(ScheduledPurchaseRun.checkout_id == checkout.id)
            .with_for_update()
        )
        if run is None or run.status == ScheduledRunStatus.SUCCEEDED:
            return
        intent = self.db.scalar(
            select(ScheduledPurchaseIntent)
            .where(ScheduledPurchaseIntent.id == run.intent_id)
            .with_for_update()
        )
        if intent is None:
            raise ConflictError(
                "scheduled_purchase_missing", "Scheduled payment has no owning intent."
            )
        prior_status = intent.status
        preserve_control_state = self._preserve_after_payment(prior_status)
        run.status = ScheduledRunStatus.SUCCEEDED
        run.payment_id = payment.id
        run.provider_payment_id = provider_payment_id
        run.completed_at = utc_now()
        intent.successful_occurrences += 1
        intent.spent_minor += payment.amount_minor
        intent.last_executed_at = utc_now()
        if not preserve_control_state:
            intent.last_failure_code = None
            intent.last_failure_message = None
        next_execution = advance_schedule(
            run.scheduled_for,
            intent.frequency,
            intent.interval_count,
            intent.timezone,
            anchor=self._schedule_anchor(intent),
        )
        exhausted = (
            next_execution is None
            or intent.successful_occurrences >= intent.max_occurrences
            or next_execution >= as_utc(intent.expires_at)
        )
        if exhausted:
            if not preserve_control_state:
                intent.status = PurchaseIntentStatus.COMPLETED
            intent.next_execution_at = None
            intent.next_run_at = None
        else:
            intent.next_execution_at = next_execution
            if preserve_control_state:
                intent.next_run_at = None
            elif self._has_notification_lead(next_execution):
                intent.status = PurchaseIntentStatus.ACTIVE
                intent.next_run_at = self._notification_at(next_execution)
            else:
                intent.status = PurchaseIntentStatus.NEEDS_ATTENTION
                intent.next_run_at = None
                intent.last_failure_code = "provider_notification_window_missed"
                intent.last_failure_message = (
                    "The next occurrence needs a new provider notification window."
                )
        self._audit(
            intent,
            "scheduled_purchase.execution_succeeded",
            "payment_provider",
            "razorpay",
            {
                "run_id": str(run.id),
                "checkout_id": str(checkout.id),
                "payment_id": str(payment.id),
                "amount_minor": payment.amount_minor,
                "successful_occurrences": intent.successful_occurrences,
                "spent_minor": intent.spent_minor,
            },
        )

    def fail_payment(self, checkout: Checkout, payment: Payment) -> None:
        run = self.db.scalar(
            select(ScheduledPurchaseRun)
            .where(ScheduledPurchaseRun.checkout_id == checkout.id)
            .with_for_update()
        )
        if run is None or run.status == ScheduledRunStatus.SUCCEEDED:
            return
        intent = self.db.scalar(
            select(ScheduledPurchaseIntent)
            .where(ScheduledPurchaseIntent.id == run.intent_id)
            .with_for_update()
        )
        if intent is None:
            return
        prior_status = intent.status
        preserve_control_state = self._preserve_after_payment(prior_status)
        run.status = ScheduledRunStatus.REQUIRES_HUMAN_ACTION
        run.payment_id = payment.id
        run.failure_code = payment.failure_code or "payment_failed"
        run.failure_message = payment.failure_description
        run.completed_at = utc_now()
        next_execution = advance_schedule(
            run.scheduled_for,
            intent.frequency,
            intent.interval_count,
            intent.timezone,
            anchor=self._schedule_anchor(intent),
        )
        if next_execution is None or next_execution >= as_utc(intent.expires_at):
            intent.next_execution_at = None
        elif prior_status not in {
            PurchaseIntentStatus.REVOKED,
            PurchaseIntentStatus.EXPIRED,
            PurchaseIntentStatus.COMPLETED,
        }:
            intent.next_execution_at = next_execution
        if not preserve_control_state:
            intent.status = PurchaseIntentStatus.NEEDS_ATTENTION
            intent.last_failure_code = run.failure_code
            intent.last_failure_message = run.failure_message
        intent.next_run_at = None
        self._audit(
            intent,
            "scheduled_purchase.execution_failed",
            "payment_provider",
            "razorpay",
            {"run_id": str(run.id), "failure_code": run.failure_code},
        )

    def _destination(
        self,
        payload: ScheduledPurchaseDraftCreate,
        customer: UserAccount,
        merchant: Merchant,
    ) -> tuple[CustomerAddress | None, dict | None, str | None, uuid.UUID, int]:
        address = None
        address_snapshot = None
        address_sha256 = None
        postal_code = None
        if payload.fulfillment_type != FulfillmentType.PICKUP:
            address = self.db.scalar(
                select(CustomerAddress).where(
                    CustomerAddress.id == payload.address_id,
                    CustomerAddress.user_id == customer.id,
                )
            )
            if address is None:
                raise NotFoundError("address_not_found", "Delivery address was not found.")
            address_snapshot = CheckoutService._address_snapshot(address)
            address_sha256 = canonical_sha256(address_snapshot)
            postal_code = address.postal_code
        location, zone = self.locations.resolve_location(
            merchant,
            payload.fulfillment_type,
            postal_code,
            payload.location_id,
        )
        return address, address_snapshot, address_sha256, location.id, zone.fee_minor if zone else 0

    def _item_policy(
        self,
        payload: ScheduledPurchaseDraftCreate,
        merchant: Merchant,
        delivery_fee_minor: int,
    ) -> tuple[list[dict], list[dict], int]:
        variant_ids = {
            variant_id for item in payload.items for variant_id in item.acceptable_variant_ids
        }
        variants = {
            variant.id: variant
            for variant in self.db.scalars(
                select(ProductVariant)
                .join(Product, ProductVariant.product_id == Product.id)
                .where(
                    ProductVariant.id.in_(variant_ids),
                    ProductVariant.merchant_id == merchant.id,
                    ProductVariant.sellable.is_(True),
                    Product.status == ProductStatus.ACTIVE,
                )
                .options(selectinload(ProductVariant.product).selectinload(Product.modifier_groups))
            ).unique()
        }
        if set(variants) != variant_ids:
            raise NotFoundError(
                "variant_not_found", "One or more scheduled variants are unavailable."
            )
        policies: list[dict] = []
        display: list[dict] = []
        estimated_max = delivery_fee_minor
        for index, requested in enumerate(payload.items):
            acceptable: list[dict] = []
            max_unit = 0
            modifier_names: list[str] | None = None
            for variant_id in requested.acceptable_variant_ids:
                variant = variants[variant_id]
                if not supports_fulfillment(variant.product.attributes, payload.fulfillment_type):
                    raise DomainError(
                        "fulfillment_not_supported",
                        "A scheduled item does not support this fulfillment type.",
                        422,
                    )
                selected = CheckoutService._validate_modifiers(
                    variant.product, requested.modifier_option_ids
                )
                names = [option.name for option in selected]
                if modifier_names is None:
                    modifier_names = names
                elif modifier_names != names:
                    raise DomainError(
                        "scheduled_alternative_modifier_mismatch",
                        "Alternative variants must accept the same modifier choices.",
                        422,
                    )
                unit = variant.price_minor + sum(option.price_delta_minor for option in selected)
                max_unit = max(max_unit, unit)
                acceptable.append(
                    {
                        "id": str(variant.id),
                        "title": f"{variant.product.name} — {variant.name}",
                        "sku": variant.sku,
                        "current_unit_price_minor": unit,
                    }
                )
            requirement_id = f"scheduled-line-{index + 1}"
            policy = {
                "id": requirement_id,
                "acceptable_variant_ids": [
                    str(value) for value in requested.acceptable_variant_ids
                ],
                "acceptable_items": [
                    {"id": item["id"], "title": item["title"]} for item in acceptable
                ],
                "quantity": requested.quantity,
                "modifier_option_ids": [str(value) for value in requested.modifier_option_ids],
            }
            policies.append(policy)
            display.append(
                {
                    **policy,
                    "acceptable_items": acceptable,
                    "modifier_names": modifier_names or [],
                }
            )
            estimated_max += max_unit * requested.quantity
        return policies, display, estimated_max

    def _instrument(self, customer_id: uuid.UUID, instrument_id: uuid.UUID) -> PaymentInstrument:
        instrument = self.db.scalar(
            select(PaymentInstrument).where(
                PaymentInstrument.id == instrument_id,
                PaymentInstrument.user_id == customer_id,
                PaymentInstrument.status == "active",
            )
        )
        if instrument is None:
            raise NotFoundError("payment_instrument_not_found", "Payment method was not found.")
        return instrument

    @staticmethod
    def _require_autonomous_instrument(instrument: PaymentInstrument) -> None:
        if instrument.instrument_type != AUTONOMOUS_PAYMENT_INSTRUMENT_TYPE:
            raise DomainError(
                "scheduled_autopay_instrument_required",
                "Human-not-present schedules require a Razorpay UPI Autopay instrument.",
                422,
            )

    @staticmethod
    def _preserve_after_payment(status: PurchaseIntentStatus) -> bool:
        return status in {
            PurchaseIntentStatus.DRAFT,
            PurchaseIntentStatus.PENDING_PROVIDER_AUTHORIZATION,
            PurchaseIntentStatus.PAUSED,
            PurchaseIntentStatus.COMPLETED,
            PurchaseIntentStatus.EXPIRED,
            PurchaseIntentStatus.REVOKED,
        }

    def _next_recoverable_execution(
        self, intent: ScheduledPurchaseIntent, starting_at: datetime
    ) -> datetime | None:
        candidate: datetime | None = as_utc(starting_at)
        expires_at = as_utc(intent.expires_at)
        while candidate < expires_at:
            previous_run = self.db.scalar(
                select(ScheduledPurchaseRun).where(
                    ScheduledPurchaseRun.intent_id == intent.id,
                    ScheduledPurchaseRun.scheduled_for == candidate,
                )
            )
            if previous_run is not None and previous_run.failure_code in {
                "scheduled_debit_submission_ambiguous",
                "scheduled_notification_submission_ambiguous",
            }:
                raise ConflictError(
                    "scheduled_reconciliation_required",
                    (
                        "Resolve the prior Razorpay submission with the merchant before "
                        "resuming this schedule."
                    ),
                )
            occurrence_finished = previous_run is not None and previous_run.status in {
                ScheduledRunStatus.SUCCEEDED,
                ScheduledRunStatus.REQUIRES_HUMAN_ACTION,
                ScheduledRunStatus.FAILED,
                ScheduledRunStatus.SKIPPED,
            }
            if not occurrence_finished and self._has_notification_lead(candidate):
                return candidate
            candidate = advance_schedule(
                candidate,
                intent.frequency,
                intent.interval_count,
                intent.timezone,
                anchor=self._schedule_anchor(intent),
            )
            if candidate is None:
                return None
        return None

    @staticmethod
    def _schedule_anchor(intent: ScheduledPurchaseIntent) -> datetime:
        value = (intent.constraints or {}).get("first_run_at")
        if not isinstance(value, str):
            raise ValueError("Scheduled purchase is missing its signed first execution")
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("Signed first execution must include a timezone")
        return parsed

    def _owned(
        self, intent_id: uuid.UUID, customer_id: uuid.UUID, *, lock: bool = False
    ) -> ScheduledPurchaseIntent:
        statement = select(ScheduledPurchaseIntent).where(
            ScheduledPurchaseIntent.id == intent_id,
            ScheduledPurchaseIntent.customer_id == customer_id,
        )
        if lock:
            statement = statement.with_for_update()
        intent = self.db.scalar(statement)
        if intent is None:
            raise NotFoundError("scheduled_purchase_not_found", "Scheduled purchase was not found.")
        return intent

    def _authorization_out(
        self,
        intent: ScheduledPurchaseIntent,
        merchant: Merchant | None = None,
        instrument: PaymentInstrument | None = None,
    ) -> ScheduledPurchaseAuthorizationOut:
        return ScheduledPurchaseAuthorizationOut(
            scheduled_purchase=self._out(
                intent,
                merchant or self._required(self.db.get(Merchant, intent.merchant_id), "merchant"),
                instrument
                or self._required(
                    self.db.get(PaymentInstrument, intent.payment_instrument_id),
                    "payment instrument",
                ),
            ),
            open_checkout_mandate=self._required(
                intent.open_checkout_mandate, "open checkout mandate"
            ),
            open_payment_mandate=self._required(
                intent.open_payment_mandate, "open payment mandate"
            ),
        )

    def _out_with_lookups(
        self, intent: ScheduledPurchaseIntent, *, include_runs: bool = False
    ) -> ScheduledPurchaseOut:
        merchant = self.db.get(Merchant, intent.merchant_id)
        instrument = self.db.get(PaymentInstrument, intent.payment_instrument_id)
        if merchant is None or instrument is None:
            raise NotFoundError(
                "scheduled_purchase_dependency_missing",
                "Merchant or payment instrument was not found.",
            )
        runs = None
        if include_runs:
            runs = list(
                self.db.scalars(
                    select(ScheduledPurchaseRun)
                    .where(ScheduledPurchaseRun.intent_id == intent.id)
                    .order_by(ScheduledPurchaseRun.scheduled_for.desc())
                    .limit(50)
                )
            )
        return self._out(intent, merchant, instrument, runs)

    def _out(
        self,
        intent: ScheduledPurchaseIntent,
        merchant: Merchant,
        instrument: PaymentInstrument,
        runs: list[ScheduledPurchaseRun] | None = None,
    ) -> ScheduledPurchaseOut:
        if intent.fulfillment_type is None or intent.payment_instrument_id is None:
            raise DomainError(
                "legacy_schedule_unsupported", "This legacy schedule must be recreated.", 409
            )
        return ScheduledPurchaseOut(
            id=intent.id,
            merchant_slug=merchant.slug,
            status=intent.status,
            fulfillment_type=intent.fulfillment_type,
            address_id=intent.address_id,
            location_id=intent.location_id,
            payment_instrument_id=intent.payment_instrument_id,
            payment_instrument_alias=instrument.alias,
            constraints=intent.constraints,
            frequency=intent.frequency,
            interval_count=intent.interval_count,
            timezone=intent.timezone,
            next_run_at=intent.next_run_at,
            next_execution_at=intent.next_execution_at,
            expires_at=self._required(intent.expires_at, "schedule expiry"),
            max_occurrences=intent.max_occurrences,
            successful_occurrences=intent.successful_occurrences,
            max_amount_minor=intent.max_amount_minor,
            max_total_minor=intent.max_total_minor,
            spent_minor=intent.spent_minor,
            currency=intent.currency,
            display=intent.display_payload,
            display_sha256=intent.display_sha256,
            open_checkout_hash=intent.open_checkout_hash,
            authorization_reference=intent.authorization_reference,
            provider_ready=self._provider_ready(instrument, intent),
            authorized_at=intent.authorized_at,
            provider_authorized_at=intent.provider_authorized_at,
            paused_at=intent.paused_at,
            revoked_at=intent.revoked_at,
            last_failure_code=intent.last_failure_code,
            last_failure_message=intent.last_failure_message,
            created_at=intent.created_at,
            updated_at=intent.updated_at,
            runs=[self._run_out(run) for run in (runs or [])],
        )

    @staticmethod
    def _run_out(run: ScheduledPurchaseRun) -> ScheduledPurchaseRunOut:
        return ScheduledPurchaseRunOut(
            id=run.id,
            scheduled_for=run.scheduled_for,
            status=run.status,
            attempt_count=run.attempt_count,
            checkout_id=run.checkout_id,
            order_id=run.order_id,
            payment_id=run.payment_id,
            amount_minor=run.amount_minor,
            currency=run.currency,
            provider_payment_after=run.provider_payment_after,
            provider_order_id=run.provider_order_id,
            provider_payment_id=run.provider_payment_id,
            failure_code=run.failure_code,
            failure_message=run.failure_message,
            evidence=run.evidence,
            created_at=run.created_at,
            updated_at=run.updated_at,
        )

    @staticmethod
    def _action_out(intent: ScheduledPurchaseIntent) -> ScheduledPurchaseActionOut:
        return ScheduledPurchaseActionOut(
            id=intent.id, status=intent.status, next_run_at=intent.next_run_at
        )

    def _challenge_out(
        self, intent: ScheduledPurchaseIntent, options: dict
    ) -> ScheduledPurchaseChallengeOut:
        return ScheduledPurchaseChallengeOut(
            intent_id=intent.id,
            nonce=self._required(intent.authorization_nonce, "authorization nonce"),
            display_sha256=self._required(intent.display_sha256, "display hash"),
            display=self._required(intent.display_payload, "display payload"),
            expires_at=self._required(
                intent.authorization_challenge_expires_at,
                "authorization challenge expiry",
            ),
            agent_public_jwk=self._required(intent.agent_public_jwk, "agent public key"),
            webauthn_options=options,
        )

    @staticmethod
    def _provider_ready(
        instrument: PaymentInstrument, intent: ScheduledPurchaseIntent | None = None
    ) -> bool:
        return recurring_instrument_ready(instrument, intent)

    def _notification_at(self, execution_at: datetime) -> datetime:
        return as_utc(execution_at) - timedelta(
            hours=self.settings.razorpay_recurring_notification_lead_hours
        )

    def _has_notification_lead(self, execution_at: datetime | None) -> bool:
        return execution_at is not None and self._notification_at(execution_at) > utc_now()

    def _master_key(self) -> str:
        value = self.settings.ap2_autonomous_agent_master_key
        if value is None:
            raise DomainError(
                "ap2_autonomous_not_configured",
                "AP2 autonomous agent key derivation is not configured.",
                503,
            )
        return value.get_secret_value()

    @staticmethod
    def _required(value, name: str):
        if value is None:
            raise DomainError(
                "scheduled_purchase_evidence_missing",
                f"Scheduled purchase {name} is missing.",
                500,
            )
        return value

    def _audit(
        self,
        intent: ScheduledPurchaseIntent,
        event_type: str,
        actor_type: str,
        actor_id: str | None,
        payload: dict,
    ) -> None:
        self.db.add(
            AuditEvent(
                merchant_id=intent.merchant_id,
                actor_type=actor_type,
                actor_id=actor_id,
                event_type=event_type,
                aggregate_type="scheduled_purchase",
                aggregate_id=str(intent.id),
                payload=payload,
            )
        )
