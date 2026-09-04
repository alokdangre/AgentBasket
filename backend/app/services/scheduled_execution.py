from __future__ import annotations

import hashlib
import json
import secrets
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.core.config import get_settings
from app.core.errors import ConflictError, DomainError, NotFoundError
from app.db.models import (
    AuditEvent,
    Checkout,
    CheckoutLineItem,
    InventoryItem,
    Merchant,
    Order,
    Payment,
    PaymentInstrument,
    ProductVariant,
    ScheduledPurchaseIntent,
    ScheduledPurchaseRun,
    UserAccount,
)
from app.domain.enums import (
    CheckoutStatus,
    OrderStatus,
    PaymentStatus,
    PurchaseIntentStatus,
    ReservationStatus,
    ScheduledRunStatus,
)
from app.payments.razorpay import RazorpayRecurringGateway
from app.protocols.ap2.autonomous import (
    canonical_sha256,
    close_and_verify_mandates,
    derive_agent_key,
    verify_existing_closed_mandates,
)
from app.protocols.ap2.crypto import AP2KeySet, get_ap2_key_set
from app.schemas.checkout import CheckoutCreate, CheckoutItemCreate
from app.schemas.scheduled_purchase import ScheduledPurchaseRunOut, ScheduledWorkerResultOut
from app.services.checkout import CheckoutService
from app.services.credentials_provider import (
    CredentialsProviderService,
    recurring_instrument_ready,
)
from app.services.payment import PaymentService
from app.services.scheduled_purchase import ScheduledPurchaseService, as_utc, utc_now


class ScheduledPurchaseExecutor:
    """Durable PostgreSQL-backed HNP execution; no LLM makes authority decisions."""

    def __init__(self, db: Session, keys: AP2KeySet | None = None) -> None:
        self.db = db
        self.settings = get_settings()
        self._keys = keys

    @property
    def keys(self) -> AP2KeySet:
        if self._keys is None:
            self._keys = get_ap2_key_set()
        return self._keys

    def run_cycle(
        self, gateway: RazorpayRecurringGateway, now: datetime | None = None
    ) -> ScheduledWorkerResultOut:
        instant = as_utc(now or utc_now())
        self.recover_stale_claims(instant)
        run_ids = self.claim_due(instant)
        processed = succeeded = requires_human = failed = 0
        for run_id in run_ids:
            processed += 1
            result = self.execute_claimed(run_id, gateway)
            if result.status == ScheduledRunStatus.SUCCEEDED:
                succeeded += 1
            elif result.status == ScheduledRunStatus.REQUIRES_HUMAN_ACTION:
                requires_human += 1
            elif result.status == ScheduledRunStatus.FAILED:
                failed += 1
        for run_id in self.claim_due_debits(instant):
            processed += 1
            result = self.submit_debit(run_id, gateway)
            if result.status == ScheduledRunStatus.REQUIRES_HUMAN_ACTION:
                requires_human += 1
            elif result.status == ScheduledRunStatus.FAILED:
                failed += 1
        return ScheduledWorkerResultOut(
            claimed=len(run_ids),
            processed=processed,
            succeeded=succeeded,
            requires_human_action=requires_human,
            failed=failed,
        )

    def claim_due(self, now: datetime | None = None) -> list[uuid.UUID]:
        instant = as_utc(now or utc_now())
        claimed: list[uuid.UUID] = []
        with self.db.begin():
            intents = list(
                self.db.scalars(
                    select(ScheduledPurchaseIntent)
                    .where(
                        ScheduledPurchaseIntent.status == PurchaseIntentStatus.ACTIVE,
                        ScheduledPurchaseIntent.next_run_at.is_not(None),
                        ScheduledPurchaseIntent.next_run_at <= instant,
                    )
                    .order_by(ScheduledPurchaseIntent.next_run_at)
                    .limit(self.settings.scheduled_worker_batch_size)
                    .with_for_update(skip_locked=True)
                )
            )
            for intent in intents:
                if intent.expires_at is None or as_utc(intent.expires_at) <= instant:
                    intent.status = PurchaseIntentStatus.EXPIRED
                    intent.next_run_at = None
                    continue
                if intent.next_execution_at is None:
                    intent.status = PurchaseIntentStatus.NEEDS_ATTENTION
                    intent.last_failure_code = "execution_time_missing"
                    intent.next_run_at = None
                    continue
                run = self.db.scalar(
                    select(ScheduledPurchaseRun)
                    .where(
                        ScheduledPurchaseRun.intent_id == intent.id,
                        ScheduledPurchaseRun.scheduled_for == intent.next_execution_at,
                    )
                    .with_for_update()
                )
                if run is None:
                    run = ScheduledPurchaseRun(
                        intent_id=intent.id,
                        scheduled_for=intent.next_execution_at,
                        status=ScheduledRunStatus.PENDING,
                        idempotency_key=(
                            f"schedule:{intent.id}:"
                            f"{int(as_utc(intent.next_execution_at).timestamp())}"
                        ),
                        currency=intent.currency,
                    )
                    self.db.add(run)
                    self.db.flush()
                if run.status != ScheduledRunStatus.PENDING:
                    intent.next_run_at = None
                    continue
                run.status = ScheduledRunStatus.CLAIMED
                run.claimed_at = instant
                run.started_at = instant
                run.attempt_count += 1
                intent.next_run_at = None
                claimed.append(run.id)
                self._audit(
                    intent,
                    "scheduled_purchase.run_claimed",
                    {"run_id": str(run.id), "scheduled_for": run.scheduled_for.isoformat()},
                )
        return claimed

    def claim_for_test(
        self,
        intent_id: uuid.UUID,
        customer: UserAccount,
        request_idempotency_key: str,
    ) -> uuid.UUID:
        if self.settings.app_env.casefold() == "production":
            raise DomainError(
                "scheduled_run_now_disabled", "Run-now is unavailable in production.", 403
            )
        with self.db.begin():
            intent = self.db.scalar(
                select(ScheduledPurchaseIntent)
                .where(
                    ScheduledPurchaseIntent.id == intent_id,
                    ScheduledPurchaseIntent.customer_id == customer.id,
                )
                .with_for_update()
            )
            if intent is None:
                raise NotFoundError(
                    "scheduled_purchase_not_found", "Scheduled purchase was not found."
                )
            run_idempotency_key = (
                f"schedule-test:{intent.id}:"
                f"{hashlib.sha256(request_idempotency_key.encode()).hexdigest()}"
            )
            idempotent_run = self.db.scalar(
                select(ScheduledPurchaseRun)
                .where(
                    ScheduledPurchaseRun.intent_id == intent.id,
                    ScheduledPurchaseRun.idempotency_key == run_idempotency_key,
                )
                .with_for_update()
            )
            if idempotent_run is not None:
                return idempotent_run.id
            if intent.status != PurchaseIntentStatus.ACTIVE or intent.next_execution_at is None:
                raise ConflictError(
                    "scheduled_purchase_not_runnable",
                    "Only an active provider-ready schedule can be triggered.",
                )
            existing = self.db.scalar(
                select(ScheduledPurchaseRun).where(
                    ScheduledPurchaseRun.intent_id == intent.id,
                    ScheduledPurchaseRun.scheduled_for == intent.next_execution_at,
                )
            )
            if existing is not None:
                return existing.id
            run = ScheduledPurchaseRun(
                intent_id=intent.id,
                scheduled_for=intent.next_execution_at,
                status=ScheduledRunStatus.CLAIMED,
                idempotency_key=run_idempotency_key,
                attempt_count=1,
                claimed_at=utc_now(),
                started_at=utc_now(),
                currency=intent.currency,
            )
            intent.next_run_at = None
            self.db.add(run)
            self.db.flush()
            return run.id

    def execute_claimed(
        self, run_id: uuid.UUID, gateway: RazorpayRecurringGateway
    ) -> ScheduledPurchaseRunOut:
        checkout_id: uuid.UUID | None = None
        provider_submission_started = False
        try:
            snapshot = self._execution_snapshot(run_id)
            customer = snapshot["customer"]
            intent = snapshot["intent"]
            merchant = snapshot["merchant"]
            items = self._select_items(intent)
            checkout_payload = CheckoutCreate(
                merchant_slug=merchant.slug,
                fulfillment_type=intent.fulfillment_type,
                postal_code=(intent.address_snapshot or {}).get("postal_code"),
                location_id=intent.location_id
                if intent.fulfillment_type.value == "pickup"
                else None,
                delivery_address=intent.address_snapshot,
                items=items,
            )
            checkout_expiry = as_utc(snapshot["scheduled_for"]) + timedelta(
                minutes=self.settings.checkout_ttl_minutes
            )
            checkout_out = CheckoutService(self.db).create_scheduled(
                checkout_payload,
                f"scheduled-{run_id}",
                customer,
                expires_at=checkout_expiry,
            )
            checkout_id = checkout_out.id
            if (
                checkout_out.total_minor > intent.max_amount_minor
                or intent.spent_minor + checkout_out.total_minor > intent.max_total_minor
            ):
                CheckoutService(self.db).cancel(checkout_out.id, customer)
                raise ConflictError(
                    "scheduled_budget_exceeded",
                    "The current quote exceeds the signed schedule budget.",
                )
            checkout_jwt = self._merchant_checkout_jwt(
                checkout_out,
                merchant,
                intent,
                as_utc(snapshot["scheduled_for"]),
            )
            nonce = secrets.token_urlsafe(32)
            master = self.settings.ap2_autonomous_agent_master_key
            if master is None:
                raise DomainError(
                    "ap2_autonomous_not_configured",
                    "AP2 autonomous agent key derivation is not configured.",
                    503,
                )
            if not (
                intent.open_checkout_mandate
                and intent.open_payment_mandate
                and intent.open_checkout_hash
                and intent.payment_instrument_id
            ):
                raise ConflictError(
                    "ap2_evidence_incomplete", "Open AP2 authorization is incomplete."
                )
            instrument = snapshot["instrument"]
            bundle = close_and_verify_mandates(
                keys=self.keys,
                agent_key=derive_agent_key(master.get_secret_value(), intent.id),
                open_checkout_token=intent.open_checkout_mandate,
                open_payment_token=intent.open_payment_mandate,
                open_checkout_hash=intent.open_checkout_hash,
                merchant_checkout_jwt=checkout_jwt,
                amount_minor=checkout_out.total_minor,
                currency=checkout_out.currency,
                merchant_id=str(merchant.id),
                merchant_name=merchant.name,
                payment_instrument_id=str(instrument.id),
                payment_instrument_type=instrument.instrument_type,
                audience=self.keys.audience,
                nonce=nonce,
                successful_occurrences=intent.successful_occurrences,
                spent_minor=intent.spent_minor,
                last_used_at=intent.last_executed_at,
                execution_at=as_utc(snapshot["scheduled_for"]),
                strict_constraints=intent.constraints,
            )
            self._persist_closed_evidence(
                run_id,
                checkout_out.id,
                checkout_out.total_minor,
                checkout_jwt,
                bundle.checkout_hash,
                bundle.checkout_token,
                bundle.payment_token,
                nonce,
            )
            instrument, provider_customer_id, provider_token = self._release_credential(
                run_id, customer
            )
            payment_after = int(as_utc(snapshot["scheduled_for"]).timestamp())
            receipt = f"abs-{run_id.hex}"
            if not self._mark_notification_submission_started(run_id, receipt):
                return self.get_run(run_id)
            provider_submission_started = True
            provider = gateway.create_recurring_order(
                amount=checkout_out.total_minor,
                currency=checkout_out.currency,
                receipt=receipt,
                token_id=provider_token,
                payment_after=payment_after,
                notes={
                    "scheduled_purchase_id": str(intent.id),
                    "scheduled_run_id": str(run_id),
                    "checkout_id": str(checkout_out.id),
                },
            )
            if (
                provider.order.amount != checkout_out.total_minor
                or provider.order.currency.upper() != checkout_out.currency.upper()
                or provider.order.receipt != receipt
                or provider.order.status not in {"created", "attempted"}
            ):
                raise ConflictError(
                    "razorpay_recurring_order_mismatch",
                    "Razorpay recurring order does not match the AP2 checkout.",
                )
            self._persist_notification(
                run_id,
                checkout_out.id,
                customer,
                provider_customer_id,
                instrument,
                provider,
                receipt,
            )
        except DomainError as error:
            if checkout_id is not None and not provider_submission_started:
                self._cancel_checkout_safely(checkout_id)
            message = (
                "Razorpay may have received the notification request. Reconcile the provider "
                "receipt before retrying; no automatic retry was made."
                if provider_submission_started
                else error.message
            )
            self._fail_run(run_id, error.code, message, human_action=True)
        except (ValueError, TypeError, KeyError) as error:
            if checkout_id is not None:
                self._cancel_checkout_safely(checkout_id)
            self._fail_run(
                run_id,
                "ap2_autonomous_verification_failed",
                "The execution did not satisfy the signed AP2 constraints.",
                human_action=False,
                internal=str(error),
            )
        return self.get_run(run_id)

    def claim_due_debits(self, now: datetime | None = None) -> list[uuid.UUID]:
        instant = as_utc(now or utc_now())
        claimed: list[uuid.UUID] = []
        with self.db.begin():
            runs = list(
                self.db.scalars(
                    select(ScheduledPurchaseRun)
                    .where(
                        ScheduledPurchaseRun.status == ScheduledRunStatus.NOTIFICATION_PENDING,
                        ScheduledPurchaseRun.provider_payment_after.is_not(None),
                        ScheduledPurchaseRun.provider_payment_after <= instant,
                    )
                    .order_by(ScheduledPurchaseRun.provider_payment_after)
                    .limit(self.settings.scheduled_worker_batch_size)
                    .with_for_update(skip_locked=True)
                )
            )
            for run in runs:
                run.status = ScheduledRunStatus.PAYMENT_PENDING
                run.attempt_count += 1
                run.claimed_at = instant
                run.started_at = instant
                evidence = dict(run.evidence or {})
                evidence["debit_claimed_at"] = instant.isoformat()
                run.evidence = evidence
                claimed.append(run.id)
        return claimed

    def submit_debit(
        self, run_id: uuid.UUID, gateway: RazorpayRecurringGateway
    ) -> ScheduledPurchaseRunOut:
        provider_submission_started = False
        payment_record_id: uuid.UUID | None = None
        try:
            with self.db.begin():
                run = self._run(run_id, lock=True)
                if run.status != ScheduledRunStatus.PAYMENT_PENDING:
                    return ScheduledPurchaseService._run_out(run)
                if run.provider_payment_id or (run.evidence or {}).get("razorpay_debit"):
                    return ScheduledPurchaseService._run_out(run)
                if (run.evidence or {}).get("debit_submission_started_at"):
                    self._require_provider_reconciliation(
                        run,
                        "scheduled_debit_submission_ambiguous",
                        "A prior debit attempt may have reached Razorpay; reconcile it before "
                        "any retry.",
                    )
                    return ScheduledPurchaseService._run_out(run)
                intent = self.db.scalar(
                    select(ScheduledPurchaseIntent)
                    .where(ScheduledPurchaseIntent.id == run.intent_id)
                    .with_for_update()
                )
                checkout = self.db.scalar(
                    select(Checkout)
                    .where(Checkout.id == run.checkout_id)
                    .options(
                        selectinload(Checkout.lines).selectinload(CheckoutLineItem.reservations)
                    )
                    .with_for_update()
                )
                order = self.db.scalar(
                    select(Order).where(Order.id == run.order_id).with_for_update()
                )
                payment = self.db.scalar(
                    select(Payment).where(Payment.id == run.payment_id).with_for_update()
                )
                customer = self.db.get(UserAccount, intent.customer_id if intent else None)
                instrument = self.db.get(
                    PaymentInstrument, intent.payment_instrument_id if intent else None
                )
                if not all((intent, checkout, order, payment, customer, instrument)):
                    raise ConflictError(
                        "scheduled_execution_incomplete",
                        "Scheduled debit records are incomplete.",
                    )
                blocker = self._pre_debit_blocker(
                    run, intent, checkout, order, payment, customer, instrument
                )
                if blocker is not None:
                    code, message, next_intent_status = blocker
                    self._terminalize_pre_debit(
                        run,
                        intent,
                        checkout,
                        order,
                        payment,
                        code,
                        message,
                        next_intent_status=next_intent_status,
                    )
                    return ScheduledPurchaseService._run_out(run)
                contact = customer.phone or (checkout.delivery_address or {}).get("phone")
                if not contact:
                    self._terminalize_pre_debit(
                        run,
                        intent,
                        checkout,
                        order,
                        payment,
                        "customer_contact_required",
                        "A phone number is required for Razorpay Autopay.",
                        next_intent_status=PurchaseIntentStatus.NEEDS_ATTENTION,
                    )
                    return ScheduledPurchaseService._run_out(run)
                try:
                    provider_customer_id, provider_token = self._revalidate_debit_authority(
                        run, intent, checkout, customer, instrument
                    )
                except DomainError as error:
                    self._terminalize_pre_debit(
                        run,
                        intent,
                        checkout,
                        order,
                        payment,
                        error.code,
                        error.message,
                        next_intent_status=PurchaseIntentStatus.NEEDS_ATTENTION,
                    )
                    return ScheduledPurchaseService._run_out(run)
                request = {
                    "email": customer.email,
                    "contact": contact,
                    "amount": payment.amount_minor,
                    "currency": payment.currency,
                    "order_id": run.provider_order_id,
                    "customer_id": provider_customer_id,
                    "token_id": provider_token,
                    "description": f"Scheduled Ember & Leaf order {order.public_number}",
                    "notes": {
                        "scheduled_purchase_id": str(intent.id),
                        "scheduled_run_id": str(run.id),
                    },
                }
                evidence = dict(run.evidence or {})
                evidence["debit_submission_started_at"] = utc_now().isoformat()
                run.evidence = evidence
                payment_record_id = payment.id
            provider_submission_started = True
            debit = gateway.create_recurring_payment(**request)
            # The provider call ran outside the transaction; discard the identity-map
            # snapshot so an early webhook binding is observed before any write.
            self.db.expire_all()
            with self.db.begin():
                # Webhook capture locks Payment before ScheduledPurchaseRun. Keep the
                # response path in the same order to avoid a PostgreSQL deadlock when
                # an early webhook races this transaction.
                payment = self.db.scalar(
                    select(Payment).where(Payment.id == payment_record_id).with_for_update()
                )
                run = self._run(run_id, lock=True)
                if (
                    payment is None
                    or run.payment_id != payment.id
                    or debit.order_id != run.provider_order_id
                ):
                    raise ConflictError(
                        "razorpay_recurring_payment_mismatch",
                        "Razorpay payment does not match the scheduled order.",
                    )
                if (
                    run.provider_payment_id is not None
                    and run.provider_payment_id != debit.payment_id
                ) or (
                    payment.provider_payment_id is not None
                    and payment.provider_payment_id != debit.payment_id
                ):
                    self._require_provider_reconciliation(
                        run,
                        "scheduled_payment_binding_mismatch",
                        "An early webhook bound a different payment identifier; reconcile "
                        "the provider order before continuing.",
                    )
                else:
                    # The same-ID case is an expected early-webhook race and is
                    # idempotent. Never overwrite a different verified binding.
                    if run.provider_payment_id is None:
                        run.provider_payment_id = debit.payment_id
                    if payment.provider_payment_id is None:
                        payment.provider_payment_id = debit.payment_id
                    evidence = dict(run.evidence or {})
                    evidence["razorpay_debit"] = {
                        "payment_id": debit.payment_id,
                        "order_id": debit.order_id,
                        "submitted_not_captured": payment.status != PaymentStatus.CAPTURED,
                    }
                    run.evidence = evidence
                    intent = self.db.get(ScheduledPurchaseIntent, run.intent_id)
                    if intent is not None:
                        self._audit(
                            intent,
                            "scheduled_purchase.debit_submitted",
                            {
                                "run_id": str(run.id),
                                "provider_payment_id": debit.payment_id,
                            },
                        )
        except DomainError as error:
            message = (
                "Razorpay may have received the recurring debit. Reconcile the provider "
                "order before any retry."
                if provider_submission_started
                else error.message
            )
            self._fail_run(run_id, error.code, message, human_action=True)
        except Exception as error:
            # The durable submission marker deliberately prevents a blind retry after an
            # unexpected transport/process boundary failure.
            self._fail_run(
                run_id,
                "scheduled_debit_submission_ambiguous",
                "Razorpay may have received the recurring debit. Reconcile the provider "
                "order before any retry.",
                human_action=True,
                # Provider exceptions may render request bodies. Persist only the class,
                # never a recurring provider token or other released credential.
                internal=type(error).__name__,
            )
        return self.get_run(run_id)

    def _pre_debit_blocker(
        self,
        run: ScheduledPurchaseRun,
        intent: ScheduledPurchaseIntent,
        checkout: Checkout,
        order: Order,
        payment: Payment,
        customer: UserAccount,
        instrument: PaymentInstrument,
    ) -> tuple[str, str, PurchaseIntentStatus | None] | None:
        """Re-check every revocable boundary immediately before provider submission."""
        instant = utc_now()
        if intent.status != PurchaseIntentStatus.ACTIVE:
            return (
                "scheduled_purchase_not_active",
                f"The schedule is {intent.status.value}; no debit was submitted.",
                None,
            )
        if intent.expires_at is None or as_utc(intent.expires_at) <= instant:
            return (
                "scheduled_purchase_expired",
                "The signed schedule expired before debit submission.",
                PurchaseIntentStatus.EXPIRED,
            )
        if (
            intent.next_execution_at is None
            or as_utc(intent.next_execution_at) != as_utc(run.scheduled_for)
            or as_utc(run.scheduled_for) >= as_utc(intent.expires_at)
        ):
            return (
                "scheduled_execution_out_of_scope",
                "The run no longer matches the signed schedule occurrence.",
                PurchaseIntentStatus.NEEDS_ATTENTION,
            )
        if checkout.expires_at is None or as_utc(checkout.expires_at) <= instant:
            return (
                "scheduled_checkout_expired",
                "The checkout expired before recurring debit submission.",
                PurchaseIntentStatus.NEEDS_ATTENTION,
            )
        amounts = {
            run.amount_minor,
            checkout.total_minor,
            order.total_minor,
            payment.amount_minor,
        }
        currencies = {
            str(run.currency or "").upper(),
            str(checkout.currency or "").upper(),
            str(order.currency or "").upper(),
            str(payment.currency or "").upper(),
            str(intent.currency or "").upper(),
        }
        if None in amounts or len(amounts) != 1 or len(currencies) != 1 or "" in currencies:
            return (
                "scheduled_execution_terms_changed",
                "The run, checkout, order, and payment terms no longer match.",
                PurchaseIntentStatus.NEEDS_ATTENTION,
            )
        if (
            intent.successful_occurrences >= intent.max_occurrences
            or intent.spent_minor + payment.amount_minor > intent.max_total_minor
            or payment.amount_minor > intent.max_amount_minor
        ):
            return (
                "scheduled_constraints_exhausted",
                "The signed occurrence or spending limit no longer permits this debit.",
                PurchaseIntentStatus.COMPLETED,
            )
        if (
            checkout.customer_id != customer.id
            or instrument.user_id != customer.id
            or intent.payment_instrument_id != instrument.id
            or run.checkout_id != checkout.id
            or run.order_id != order.id
            or run.payment_id != payment.id
            or checkout.merchant_id != intent.merchant_id
            or order.merchant_id != intent.merchant_id
            or order.customer_id != customer.id
            or checkout.status != CheckoutStatus.PAYMENT_PENDING
            or order.status != OrderStatus.AWAITING_PAYMENT
            or payment.status != PaymentStatus.CREATED
            or order.checkout_id != checkout.id
            or payment.order_id != order.id
        ):
            return (
                "scheduled_execution_state_changed",
                "The checkout, order, payment, or credential scope changed before debit.",
                PurchaseIntentStatus.NEEDS_ATTENTION,
            )
        if not (
            recurring_instrument_ready(instrument, intent)
            and instrument.provider_customer_id
            and instrument.provider_token_reference
            and intent.payment_token_reference == instrument.provider_token_reference
            and run.provider_order_id
            and payment.provider_order_id == run.provider_order_id
        ):
            return (
                "recurring_payment_authorization_required",
                "The Razorpay recurring token is no longer confirmed or in scope.",
                PurchaseIntentStatus.PENDING_PROVIDER_AUTHORIZATION,
            )
        if not (
            run.merchant_checkout_jwt and run.closed_checkout_mandate and run.closed_payment_mandate
        ):
            return (
                "ap2_evidence_incomplete",
                "The closed AP2 mandate chain is incomplete; no debit was submitted.",
                PurchaseIntentStatus.NEEDS_ATTENTION,
            )
        return None

    def _revalidate_debit_authority(
        self,
        run: ScheduledPurchaseRun,
        intent: ScheduledPurchaseIntent,
        checkout: Checkout,
        customer: UserAccount,
        instrument: PaymentInstrument,
    ) -> tuple[str, str]:
        """Re-verify AP2 and an existing CP grant at the final provider boundary."""
        evidence = dict(run.evidence or {})
        ap2_evidence = evidence.get("ap2")
        if not isinstance(ap2_evidence, dict):
            raise ConflictError(
                "ap2_evidence_incomplete",
                "The stored AP2 verification evidence is incomplete.",
            )
        nonce = ap2_evidence.get("nonce")
        if not isinstance(nonce, str) or not nonce:
            raise ConflictError(
                "ap2_evidence_incomplete",
                "The stored AP2 nonce is missing.",
            )
        if not (
            run.closed_checkout_mandate
            and run.closed_payment_mandate
            and run.merchant_checkout_jwt
            and run.merchant_checkout_hash
            and intent.open_checkout_hash
        ):
            raise ConflictError(
                "ap2_evidence_incomplete",
                "The stored AP2 mandate chain is incomplete.",
            )
        try:
            verify_existing_closed_mandates(
                keys=self.keys,
                checkout_token=run.closed_checkout_mandate,
                payment_token=run.closed_payment_mandate,
                open_checkout_hash=intent.open_checkout_hash,
                merchant_checkout_jwt=run.merchant_checkout_jwt,
                audience=self.keys.audience,
                nonce=nonce,
                merchant_id=str(intent.merchant_id),
                payment_instrument_id=str(instrument.id),
                successful_occurrences=intent.successful_occurrences,
                spent_minor=intent.spent_minor,
                last_used_at=intent.last_executed_at,
                strict_constraints=intent.constraints,
            )
        except ValueError as error:
            raise ConflictError(
                "ap2_autonomous_verification_failed",
                "The stored AP2 mandate chain no longer satisfies its signed bounds.",
            ) from error

        token = instrument.provider_token_reference
        provider_customer_id = instrument.provider_customer_id
        cp_evidence = evidence.get("credentials_provider")
        if (
            not isinstance(cp_evidence, dict)
            or not isinstance(cp_evidence.get("signed_grant"), str)
            or token is None
            or provider_customer_id is None
        ):
            raise ConflictError(
                "credential_scope_mismatch",
                "The stored Credentials Provider release is incomplete.",
            )
        signed_grant = cp_evidence["signed_grant"]
        grant = self.keys.credentials_provider.verify(signed_grant, self.keys.audience)
        expected_scope = {
            "run_id": str(run.id),
            "checkout_hash": run.merchant_checkout_hash,
            "amount_minor": checkout.total_minor,
            "currency": checkout.currency,
        }
        scope_sha256 = hashlib.sha256(
            json.dumps(expected_scope, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        expected_claims = {
            "sub": str(customer.id),
            "scheduled_purchase_id": str(intent.id),
            "scheduled_run_id": str(run.id),
            "checkout_hash": run.merchant_checkout_hash,
            "payment_instrument_id": str(instrument.id),
            "amount_minor": checkout.total_minor,
            "currency": checkout.currency,
            "provider_token_sha256": hashlib.sha256(token.encode()).hexdigest(),
        }
        if any(grant.get(key) != value for key, value in expected_claims.items()) or not (
            secrets.compare_digest(
                str(cp_evidence.get("grant_sha256") or ""),
                hashlib.sha256(signed_grant.encode()).hexdigest(),
            )
            and secrets.compare_digest(str(cp_evidence.get("scope_sha256") or ""), scope_sha256)
        ):
            raise ConflictError(
                "credential_scope_mismatch",
                "The stored Credentials Provider release no longer matches this debit.",
            )
        cp_evidence = dict(cp_evidence)
        cp_evidence["last_revalidated_at"] = utc_now().isoformat()
        evidence["credentials_provider"] = cp_evidence
        run.evidence = evidence
        return provider_customer_id, token

    def _terminalize_pre_debit(
        self,
        run: ScheduledPurchaseRun,
        intent: ScheduledPurchaseIntent,
        checkout: Checkout,
        order: Order,
        payment: Payment,
        code: str,
        message: str,
        *,
        next_intent_status: PurchaseIntentStatus | None,
    ) -> None:
        """Stop a definitely-unsubmitted debit and unwind all local commerce state."""
        instant = utc_now()
        self._cancel_pre_debit_checkout(checkout)
        if order.status == OrderStatus.AWAITING_PAYMENT:
            order.status = OrderStatus.CANCELED
        if payment.status == PaymentStatus.CREATED:
            payment.status = PaymentStatus.FAILED
            payment.failure_code = code[:120]
            payment.failure_description = message
        run.status = ScheduledRunStatus.SKIPPED
        run.failure_code = code[:120]
        run.failure_message = message
        run.completed_at = instant
        if next_intent_status is not None and intent.status == PurchaseIntentStatus.ACTIVE:
            intent.status = next_intent_status
            intent.next_run_at = None
            intent.last_failure_code = run.failure_code
            intent.last_failure_message = message
        self._audit(
            intent,
            "scheduled_purchase.debit_skipped",
            {"run_id": str(run.id), "failure_code": run.failure_code},
        )

    def _cancel_pre_debit_checkout(self, checkout: Checkout) -> None:
        for line in checkout.lines:
            for reservation in line.reservations:
                if reservation.status != ReservationStatus.ACTIVE:
                    continue
                inventory = self.db.scalar(
                    select(InventoryItem)
                    .where(InventoryItem.id == reservation.inventory_item_id)
                    .with_for_update()
                )
                if inventory is not None:
                    inventory.reserved_quantity = max(
                        0, inventory.reserved_quantity - reservation.quantity
                    )
                reservation.status = ReservationStatus.RELEASED
        if checkout.status != CheckoutStatus.COMPLETED:
            checkout.status = CheckoutStatus.CANCELED

    def _require_provider_reconciliation(
        self,
        run: ScheduledPurchaseRun,
        code: str,
        message: str,
        *,
        intent: ScheduledPurchaseIntent | None = None,
    ) -> None:
        if intent is None:
            intent = self.db.scalar(
                select(ScheduledPurchaseIntent)
                .where(ScheduledPurchaseIntent.id == run.intent_id)
                .with_for_update()
            )
        run.status = ScheduledRunStatus.REQUIRES_HUMAN_ACTION
        run.failure_code = code[:120]
        run.failure_message = message
        run.completed_at = utc_now()
        if intent is not None:
            if intent.status not in {
                PurchaseIntentStatus.PAUSED,
                PurchaseIntentStatus.REVOKED,
                PurchaseIntentStatus.COMPLETED,
                PurchaseIntentStatus.EXPIRED,
            }:
                intent.status = PurchaseIntentStatus.NEEDS_ATTENTION
                intent.next_run_at = None
            intent.last_failure_code = run.failure_code
            intent.last_failure_message = message
            self._audit(
                intent,
                "scheduled_purchase.provider_reconciliation_required",
                {"run_id": str(run.id), "failure_code": run.failure_code},
            )

    def _mark_notification_submission_started(self, run_id: uuid.UUID, receipt: str) -> bool:
        """Persist the provider boundary so CHECKOUT_CREATED recovery is never ambiguous."""
        with self.db.begin():
            run = self._run(run_id, lock=True)
            if run.status != ScheduledRunStatus.CHECKOUT_CREATED:
                return False
            intent = self.db.scalar(
                select(ScheduledPurchaseIntent)
                .where(ScheduledPurchaseIntent.id == run.intent_id)
                .with_for_update()
            )
            instrument = self.db.get(
                PaymentInstrument, intent.payment_instrument_id if intent else None
            )
            if (
                intent is None
                or intent.status != PurchaseIntentStatus.ACTIVE
                or intent.expires_at is None
                or as_utc(intent.expires_at) <= utc_now()
                or instrument is None
                or not recurring_instrument_ready(instrument, intent)
                or intent.payment_token_reference != instrument.provider_token_reference
            ):
                checkout = self.db.scalar(
                    select(Checkout)
                    .where(Checkout.id == run.checkout_id)
                    .options(
                        selectinload(Checkout.lines).selectinload(CheckoutLineItem.reservations)
                    )
                    .with_for_update()
                )
                if checkout is not None:
                    self._cancel_pre_debit_checkout(checkout)
                run.status = ScheduledRunStatus.SKIPPED
                run.failure_code = "scheduled_authorization_changed"
                run.failure_message = (
                    "The schedule or recurring authorization changed before provider contact."
                )
                run.completed_at = utc_now()
                return False
            evidence = dict(run.evidence or {})
            evidence["notification_submission_started_at"] = utc_now().isoformat()
            evidence["notification_receipt"] = receipt
            run.evidence = evidence
            return True

    def recover_stale_claims(self, now: datetime | None = None) -> int:
        instant = as_utc(now or utc_now())
        stale_before = instant - timedelta(minutes=15)
        recovered = 0
        with self.db.begin():
            runs = list(
                self.db.scalars(
                    select(ScheduledPurchaseRun)
                    .where(
                        ScheduledPurchaseRun.status.in_(
                            [
                                ScheduledRunStatus.CLAIMED,
                                ScheduledRunStatus.CHECKOUT_CREATED,
                                ScheduledRunStatus.PAYMENT_PENDING,
                            ]
                        ),
                        ScheduledPurchaseRun.started_at < stale_before,
                    )
                    .with_for_update(skip_locked=True)
                )
            )
            for run in runs:
                intent = self.db.scalar(
                    select(ScheduledPurchaseIntent)
                    .where(ScheduledPurchaseIntent.id == run.intent_id)
                    .with_for_update()
                )
                checkout = (
                    self.db.scalar(
                        select(Checkout)
                        .where(Checkout.id == run.checkout_id)
                        .options(
                            selectinload(Checkout.lines).selectinload(CheckoutLineItem.reservations)
                        )
                        .with_for_update()
                    )
                    if run.checkout_id
                    else None
                )
                if intent is None:
                    run.status = ScheduledRunStatus.FAILED
                    run.failure_code = "scheduled_purchase_missing"
                    run.failure_message = "The scheduled run has no owning authorization."
                    run.completed_at = instant
                    continue
                if run.status == ScheduledRunStatus.PAYMENT_PENDING:
                    evidence = run.evidence or {}
                    if (
                        evidence.get("debit_submission_started_at")
                        or evidence.get("razorpay_debit")
                        or run.provider_payment_id
                    ):
                        self._require_provider_reconciliation(
                            run,
                            "scheduled_debit_submission_ambiguous",
                            "A recurring debit may have reached Razorpay; reconcile the "
                            "provider order before retrying.",
                            intent=intent,
                        )
                    else:
                        run.status = ScheduledRunStatus.NOTIFICATION_PENDING
                        run.claimed_at = None
                        run.started_at = None
                        recovered += 1
                    continue
                if run.status == ScheduledRunStatus.CHECKOUT_CREATED and (run.evidence or {}).get(
                    "notification_submission_started_at"
                ):
                    if checkout is not None:
                        self._cancel_pre_debit_checkout(checkout)
                    self._require_provider_reconciliation(
                        run,
                        "scheduled_notification_submission_ambiguous",
                        "A recurring notification may have reached Razorpay; reconcile the "
                        "provider receipt before retrying.",
                        intent=intent,
                    )
                    continue
                if intent.status != PurchaseIntentStatus.ACTIVE:
                    if checkout is not None:
                        self._cancel_pre_debit_checkout(checkout)
                    run.status = ScheduledRunStatus.SKIPPED
                    run.failure_code = "scheduled_purchase_not_active"
                    run.failure_message = "The schedule was no longer active during recovery."
                    run.completed_at = instant
                    continue
                run.status = ScheduledRunStatus.PENDING
                run.claimed_at = None
                run.started_at = None
                intent.next_run_at = instant
                recovered += 1
        return recovered

    def get_run(self, run_id: uuid.UUID) -> ScheduledPurchaseRunOut:
        run = self._run(run_id)
        output = ScheduledPurchaseService._run_out(run)
        self.db.rollback()
        return output

    def _execution_snapshot(self, run_id: uuid.UUID) -> dict:
        with self.db.begin():
            run = self._run(run_id, lock=True)
            if run.status != ScheduledRunStatus.CLAIMED:
                raise ConflictError(
                    "scheduled_run_not_claimed", "Scheduled run is not ready for execution."
                )
            intent = self.db.scalar(
                select(ScheduledPurchaseIntent)
                .where(ScheduledPurchaseIntent.id == run.intent_id)
                .with_for_update()
            )
            if intent is None or intent.status != PurchaseIntentStatus.ACTIVE:
                raise ConflictError(
                    "scheduled_purchase_not_active", "Scheduled purchase is not active."
                )
            if (
                intent.next_execution_at is None
                or as_utc(run.scheduled_for) != as_utc(intent.next_execution_at)
                or intent.successful_occurrences >= intent.max_occurrences
                or intent.spent_minor >= intent.max_total_minor
                or intent.expires_at is None
                or as_utc(run.scheduled_for) >= as_utc(intent.expires_at)
            ):
                raise ConflictError(
                    "scheduled_constraints_exhausted",
                    "The schedule is expired or its signed limits are exhausted.",
                )
            customer = self.db.get(UserAccount, intent.customer_id)
            merchant = self.db.get(Merchant, intent.merchant_id)
            instrument = self.db.get(PaymentInstrument, intent.payment_instrument_id)
            if customer is None or merchant is None or instrument is None:
                raise NotFoundError(
                    "scheduled_purchase_dependency_missing",
                    "Customer, merchant, or payment instrument was not found.",
                )
            return {
                "intent": intent,
                "customer": customer,
                "merchant": merchant,
                "instrument": instrument,
                "scheduled_for": run.scheduled_for,
            }

    def _select_items(self, intent: ScheduledPurchaseIntent) -> list[CheckoutItemCreate]:
        acceptable_ids = [
            uuid.UUID(value)
            for requirement in intent.constraints["items"]
            for value in requirement["acceptable_variant_ids"]
        ]
        variants = {
            row.id: row
            for row in self.db.scalars(
                select(ProductVariant).where(
                    ProductVariant.id.in_(acceptable_ids),
                    ProductVariant.merchant_id == intent.merchant_id,
                    ProductVariant.sellable.is_(True),
                )
            )
        }
        self.db.rollback()
        selected: list[CheckoutItemCreate] = []
        for requirement in intent.constraints["items"]:
            candidates = [
                variants.get(uuid.UUID(value)) for value in requirement["acceptable_variant_ids"]
            ]
            available = [candidate for candidate in candidates if candidate is not None]
            if not available:
                raise ConflictError(
                    "scheduled_item_unavailable", "No authorized scheduled item is sellable."
                )
            chosen = min(available, key=lambda value: (value.price_minor, str(value.id)))
            selected.append(
                CheckoutItemCreate(
                    variant_id=chosen.id,
                    quantity=int(requirement["quantity"]),
                    modifier_option_ids=[
                        uuid.UUID(value) for value in requirement["modifier_option_ids"]
                    ],
                )
            )
        return selected

    def _merchant_checkout_jwt(
        self,
        checkout,
        merchant: Merchant,
        intent: ScheduledPurchaseIntent,
        scheduled_for: datetime,
    ) -> str:
        address_sha256 = (
            canonical_sha256(intent.address_snapshot) if intent.address_snapshot else None
        )
        if address_sha256 != intent.constraints.get("address_sha256"):
            raise ConflictError(
                "scheduled_address_changed",
                "The delivery address no longer matches the signed schedule.",
            )
        claims = {
            "iss": self.keys.merchant.issuer,
            "aud": self.keys.audience,
            "iat": int(utc_now().timestamp()),
            "exp": int(as_utc(checkout.expires_at).timestamp()),
            "jti": f"ucp-checkout:{checkout.id}",
            "id": str(checkout.id),
            "merchant": {"id": str(merchant.id), "name": merchant.name},
            "line_items": [
                {
                    "id": str(line.id),
                    "item": {
                        "id": str(line.variant_id),
                        "title": f"{line.product_name} — {line.variant_name}",
                        "price": line.unit_price_minor
                        + (line.modifier_total_minor // line.quantity),
                    },
                    "quantity": line.quantity,
                    "totals": [{"type": "total", "amount": line.line_total_minor}],
                    "agentbasket": {
                        "modifier_option_ids": [
                            str(modifier.modifier_option_id) for modifier in line.modifiers
                        ]
                    },
                }
                for line in checkout.lines
            ],
            "status": "incomplete",
            "currency": checkout.currency,
            "totals": [
                {"type": "subtotal", "amount": checkout.subtotal_minor},
                {"type": "fulfillment", "amount": checkout.delivery_minor},
                {"type": "total", "amount": checkout.total_minor},
            ],
            "links": [
                {
                    "type": "privacy_policy",
                    "url": "https://agentbasket.local/privacy",
                    "title": "Privacy policy",
                }
            ],
            "expires_at": as_utc(checkout.expires_at).isoformat(),
            "agentbasket": {
                "source": "scheduled_agent",
                "quote_version": checkout.quote_version,
                "fulfillment_type": checkout.fulfillment_type.value,
                "location_id": str(checkout.location_id),
                "address_sha256": address_sha256,
                "scheduled_for": as_utc(scheduled_for).isoformat(),
            },
        }
        token = self.keys.merchant.sign(claims)
        self.keys.merchant.verify(token, self.keys.audience)
        return token

    def _persist_closed_evidence(
        self,
        run_id: uuid.UUID,
        checkout_id: uuid.UUID,
        amount_minor: int,
        checkout_jwt: str,
        checkout_hash: str,
        checkout_token: str,
        payment_token: str,
        nonce: str,
    ) -> None:
        with self.db.begin():
            run = self._run(run_id, lock=True)
            run.checkout_id = checkout_id
            run.amount_minor = amount_minor
            run.merchant_checkout_jwt = checkout_jwt
            run.merchant_checkout_hash = checkout_hash
            run.closed_checkout_mandate = checkout_token
            run.closed_payment_mandate = payment_token
            run.status = ScheduledRunStatus.CHECKOUT_CREATED
            run.evidence = {
                **(run.evidence or {}),
                "checkout_created_at": utc_now().isoformat(),
                "ap2": {
                    "nonce": nonce,
                    "audience": self.keys.audience,
                    "merchant_checkout_hash": checkout_hash,
                    "checkout_chain_sha256": hashlib.sha256(checkout_token.encode()).hexdigest(),
                    "payment_chain_sha256": hashlib.sha256(payment_token.encode()).hexdigest(),
                    "verification": "passed",
                },
            }

    def _release_credential(
        self, run_id: uuid.UUID, customer: UserAccount
    ) -> tuple[PaymentInstrument, str, str]:
        with self.db.begin():
            run = self._run(run_id, lock=True)
            intent = self.db.scalar(
                select(ScheduledPurchaseIntent)
                .where(ScheduledPurchaseIntent.id == run.intent_id)
                .with_for_update()
            )
            checkout = self.db.scalar(
                select(Checkout)
                .where(Checkout.id == run.checkout_id)
                .options(
                    selectinload(Checkout.lines).selectinload(CheckoutLineItem.modifiers),
                    selectinload(Checkout.fulfillment_options),
                )
            )
            if intent is None or checkout is None:
                raise ConflictError(
                    "scheduled_execution_incomplete", "Scheduled checkout is incomplete."
                )
            nonce = str((run.evidence or {}).get("ap2", {}).get("nonce") or "")
            return CredentialsProviderService(self.db, self.keys).release_recurring_token(
                intent, run, checkout, customer, nonce=nonce
            )

    def _persist_notification(
        self,
        run_id: uuid.UUID,
        checkout_id: uuid.UUID,
        customer: UserAccount,
        provider_customer_id: str,
        instrument: PaymentInstrument,
        provider,
        receipt: str,
    ) -> None:
        with self.db.begin():
            run = self._run(run_id, lock=True)
            intent = self.db.scalar(
                select(ScheduledPurchaseIntent)
                .where(ScheduledPurchaseIntent.id == run.intent_id)
                .with_for_update()
            )
            checkout = self.db.scalar(
                select(Checkout)
                .where(Checkout.id == checkout_id)
                .options(
                    selectinload(Checkout.lines).selectinload(CheckoutLineItem.modifiers),
                    selectinload(Checkout.lines).selectinload(CheckoutLineItem.reservations),
                    selectinload(Checkout.fulfillment_options),
                )
                .with_for_update()
            )
            if intent is None or checkout is None:
                raise ConflictError(
                    "scheduled_execution_incomplete", "Scheduled checkout is incomplete."
                )
            order = self.db.scalar(
                select(Order).where(Order.checkout_id == checkout.id).with_for_update()
            )
            if order is None:
                order = Order(
                    merchant_id=checkout.merchant_id,
                    checkout_id=checkout.id,
                    customer_id=customer.id,
                    public_number=PaymentService._public_number(checkout.id),
                    status=OrderStatus.AWAITING_PAYMENT,
                    currency=checkout.currency,
                    total_minor=checkout.total_minor,
                    fulfillment_snapshot=PaymentService._fulfillment_snapshot(checkout),
                )
                self.db.add(order)
                self.db.flush()
            payment = self.db.scalar(
                select(Payment)
                .where(Payment.order_id == order.id, Payment.provider == "razorpay")
                .with_for_update()
            )
            if payment is None:
                payment = Payment(
                    order_id=order.id,
                    provider="razorpay",
                    approval_id=None,
                    provider_receipt=receipt,
                    provider_order_id=provider.order.id,
                    status=PaymentStatus.CREATED,
                    amount_minor=checkout.total_minor,
                    currency=checkout.currency,
                )
                self.db.add(payment)
                self.db.flush()
            checkout.status = CheckoutStatus.PAYMENT_PENDING
            run.order_id = order.id
            run.payment_id = payment.id
            run.provider_notification_id = provider.notification.id
            run.provider_payment_after = datetime.fromtimestamp(
                provider.notification.payment_after, tz=UTC
            )
            run.provider_order_id = provider.order.id
            run.status = ScheduledRunStatus.NOTIFICATION_PENDING
            run.evidence = {
                **(run.evidence or {}),
                "razorpay_notification": {
                    "notification_id": provider.notification.id,
                    "provider_order_id": provider.order.id,
                    "payment_after": provider.notification.payment_after,
                    "instrument_id": str(instrument.id),
                    "provider_customer_sha256": hashlib.sha256(
                        provider_customer_id.encode()
                    ).hexdigest(),
                    "token_sha256": hashlib.sha256(
                        instrument.provider_token_reference.encode()
                    ).hexdigest(),
                },
            }
            self._audit(
                intent,
                "scheduled_purchase.notification_created",
                {
                    "run_id": str(run.id),
                    "checkout_id": str(checkout.id),
                    "provider_order_id": provider.order.id,
                    "provider_notification_id": provider.notification.id,
                    "payment_after": provider.notification.payment_after,
                },
            )

    def _cancel_checkout_safely(self, checkout_id: uuid.UUID) -> None:
        try:
            with self.db.begin():
                checkout = self.db.scalar(
                    select(Checkout)
                    .where(Checkout.id == checkout_id)
                    .options(
                        selectinload(Checkout.lines).selectinload(CheckoutLineItem.reservations)
                    )
                    .with_for_update()
                )
                if checkout is None or checkout.status not in {
                    CheckoutStatus.OPEN,
                    CheckoutStatus.READY_FOR_APPROVAL,
                    CheckoutStatus.APPROVED,
                }:
                    return
                for line in checkout.lines:
                    for reservation in line.reservations:
                        if reservation.status != ReservationStatus.ACTIVE:
                            continue
                        inventory = self.db.scalar(
                            select(InventoryItem)
                            .where(InventoryItem.id == reservation.inventory_item_id)
                            .with_for_update()
                        )
                        if inventory is not None:
                            inventory.reserved_quantity = max(
                                0, inventory.reserved_quantity - reservation.quantity
                            )
                        reservation.status = ReservationStatus.RELEASED
                checkout.status = CheckoutStatus.CANCELED
        except Exception:
            self.db.rollback()

    def _fail_run(
        self,
        run_id: uuid.UUID,
        code: str,
        message: str,
        *,
        human_action: bool,
        internal: str | None = None,
    ) -> None:
        self.db.rollback()
        # The provider call happens outside a transaction. A user action may have
        # changed the schedule through another session while it was in flight.
        self.db.expire_all()
        with self.db.begin():
            run = self._run(run_id, lock=True)
            if run.status == ScheduledRunStatus.SUCCEEDED:
                return
            intent = self.db.scalar(
                select(ScheduledPurchaseIntent)
                .where(ScheduledPurchaseIntent.id == run.intent_id)
                .with_for_update()
            )
            run.status = (
                ScheduledRunStatus.REQUIRES_HUMAN_ACTION
                if human_action
                else ScheduledRunStatus.FAILED
            )
            run.failure_code = code[:120]
            run.failure_message = message
            run.completed_at = utc_now()
            if internal and self.settings.app_env.casefold() != "production":
                run.evidence = {**(run.evidence or {}), "internal_error": internal[:1000]}
            if intent is not None:
                if intent.status not in {
                    PurchaseIntentStatus.PAUSED,
                    PurchaseIntentStatus.REVOKED,
                    PurchaseIntentStatus.COMPLETED,
                    PurchaseIntentStatus.EXPIRED,
                }:
                    intent.status = PurchaseIntentStatus.NEEDS_ATTENTION
                    intent.next_run_at = None
                    intent.last_failure_code = run.failure_code
                    intent.last_failure_message = message
                self._audit(
                    intent,
                    "scheduled_purchase.run_failed",
                    {"run_id": str(run.id), "failure_code": run.failure_code},
                )

    def _run(self, run_id: uuid.UUID, *, lock: bool = False) -> ScheduledPurchaseRun:
        statement = select(ScheduledPurchaseRun).where(ScheduledPurchaseRun.id == run_id)
        if lock:
            statement = statement.with_for_update()
        run = self.db.scalar(statement)
        if run is None:
            raise NotFoundError("scheduled_run_not_found", "Scheduled run was not found.")
        return run

    def _audit(self, intent: ScheduledPurchaseIntent, event_type: str, payload: dict) -> None:
        self.db.add(
            AuditEvent(
                merchant_id=intent.merchant_id,
                actor_type="agent",
                actor_id=f"scheduled:{intent.id}",
                event_type=event_type,
                aggregate_type="scheduled_purchase",
                aggregate_id=str(intent.id),
                payload=payload,
            )
        )
