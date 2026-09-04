from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.db.models import (
    Ap2Receipt,
    Checkout,
    CheckoutLineItem,
    InventoryItem,
    InventoryReservation,
    Order,
    Payment,
    PaymentInstrument,
    ScheduledPurchaseIntent,
    ScheduledPurchaseRun,
    UserAccount,
)
from app.domain.enums import (
    CheckoutStatus,
    FulfillmentType,
    OrderStatus,
    PaymentStatus,
    PurchaseIntentStatus,
    ReservationStatus,
    ScheduledRunStatus,
)
from app.payments.razorpay import RazorpayRecurringDebit
from app.protocols.ap2.autonomous import canonical_sha256
from app.protocols.ap2.crypto import AP2KeySet, AP2Signer
from app.services.scheduled_execution import ScheduledPurchaseExecutor
from app.services.scheduled_purchase import as_utc

WEBHOOK_SECRET = "scheduled-webhook-secret"


class FakeDebitGateway:
    def __init__(self) -> None:
        self.debit_requests: list[dict[str, object]] = []

    def create_recurring_payment(self, **request: object) -> RazorpayRecurringDebit:
        self.debit_requests.append(request)
        return RazorpayRecurringDebit(
            payment_id="pay_scheduled_debit",
            order_id=str(request["order_id"]),
        )


def _intent(
    seeded: dict[str, object],
    *,
    status: PurchaseIntentStatus,
    scheduled_for: datetime,
    next_run_at: datetime | None,
    expires_at: datetime,
    suffix: str,
) -> ScheduledPurchaseIntent:
    return ScheduledPurchaseIntent(
        merchant_id=seeded["merchant_id"],
        customer_id=seeded["customer_id"],
        customer_reference=str(seeded["customer_id"]),
        idempotency_key=f"scheduled-test-{suffix}",
        request_sha256=hashlib.sha256(suffix.encode()).hexdigest(),
        address_id=seeded["customer_address_id"],
        payment_instrument_id=seeded["payment_instrument_id"],
        location_id=seeded["location_id"],
        status=status,
        constraints={"items": [], "first_run_at": scheduled_for.isoformat()},
        fulfillment_type=FulfillmentType.LOCAL_DELIVERY,
        frequency="weekly",
        interval_count=1,
        timezone="Asia/Kolkata",
        max_occurrences=2,
        max_amount_minor=30_000,
        max_total_minor=60_000,
        currency="INR",
        next_run_at=next_run_at,
        next_execution_at=scheduled_for,
        expires_at=expires_at,
    )


def _scheduled_payment_graph(
    session_factory: sessionmaker[Session],
    seeded: dict[str, object],
    *,
    suffix: str,
    intent_status: PurchaseIntentStatus = PurchaseIntentStatus.ACTIVE,
    debit_submission_started: bool = False,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    now = datetime.now(UTC)
    scheduled_for = now + timedelta(hours=1)
    token_id = f"token_scheduled_{suffix}"
    with session_factory() as db, db.begin():
        instrument = db.get(PaymentInstrument, seeded["payment_instrument_id"])
        assert instrument is not None
        instrument.provider = "razorpay_test"
        instrument.instrument_type = "com.razorpay.upi.autopay"
        instrument.provider_customer_id = f"cust_scheduled_{suffix}"
        instrument.provider_token_reference = token_id
        instrument.status = "active"
        instrument.instrument_metadata = {
            "token_status": "confirmed",
            "registration_status": "confirmed",
        }
        checkout = Checkout(
            merchant_id=seeded["merchant_id"],
            customer_id=seeded["customer_id"],
            location_id=seeded["location_id"],
            status=CheckoutStatus.PAYMENT_PENDING,
            currency="INR",
            idempotency_key=f"scheduled-checkout-{suffix}",
            request_hash=hashlib.sha256(f"checkout-{suffix}".encode()).hexdigest(),
            fulfillment_type=FulfillmentType.LOCAL_DELIVERY,
            postal_code="560038",
            delivery_address={"postal_code": "560038"},
            subtotal_minor=25_000,
            total_minor=25_000,
            expires_at=scheduled_for + timedelta(minutes=10),
            source="scheduled_agent",
        )
        db.add(checkout)
        db.flush()
        order = Order(
            merchant_id=seeded["merchant_id"],
            checkout_id=checkout.id,
            customer_id=seeded["customer_id"],
            public_number=f"AB-SCHEDULED-{suffix}",
            status=OrderStatus.AWAITING_PAYMENT,
            currency="INR",
            total_minor=25_000,
            fulfillment_snapshot={},
        )
        db.add(order)
        db.flush()
        payment = Payment(
            order_id=order.id,
            provider="razorpay",
            provider_receipt=f"abs-{suffix}",
            provider_order_id=f"order_scheduled_{suffix}",
            status=PaymentStatus.CREATED,
            amount_minor=25_000,
            currency="INR",
        )
        db.add(payment)
        intent = _intent(
            seeded,
            status=intent_status,
            scheduled_for=scheduled_for,
            next_run_at=None,
            expires_at=scheduled_for + timedelta(days=30),
            suffix=suffix,
        )
        intent.payment_token_reference = token_id
        db.add(intent)
        db.flush()
        run = ScheduledPurchaseRun(
            intent_id=intent.id,
            scheduled_for=scheduled_for,
            status=ScheduledRunStatus.PAYMENT_PENDING,
            idempotency_key=f"scheduled-run-{suffix}",
            checkout_id=checkout.id,
            order_id=order.id,
            payment_id=payment.id,
            amount_minor=25_000,
            currency="INR",
            provider_order_id=payment.provider_order_id,
            closed_checkout_mandate=f"closed-checkout-{suffix}",
            closed_payment_mandate=f"closed-payment-{suffix}",
            evidence=(
                {"debit_submission_started_at": now.isoformat()} if debit_submission_started else {}
            ),
        )
        db.add(run)
        db.flush()
        return intent.id, run.id, checkout.id, payment.id


def _payment_event(event: str, *, suffix: str) -> bytes:
    failed = event == "payment.failed"
    entity = {
        "id": f"pay_scheduled_{suffix}",
        "entity": "payment",
        "amount": 25_000,
        "currency": "INR",
        "status": "failed" if failed else "captured",
        "order_id": f"order_scheduled_{suffix}",
        "token_id": f"token_scheduled_{suffix}",
        "captured": not failed,
        "error_code": "BAD_REQUEST_ERROR" if failed else None,
        "error_description": "Autopay debit failed" if failed else None,
    }
    return json.dumps(
        {"entity": "event", "event": event, "payload": {"payment": {"entity": entity}}},
        separators=(",", ":"),
    ).encode()


def _webhook_headers(raw: bytes, event_id: str) -> dict[str, str]:
    signature = hmac.new(WEBHOOK_SECRET.encode(), raw, hashlib.sha256).hexdigest()
    return {
        "Content-Type": "application/json",
        "X-Razorpay-Signature": signature,
        "X-Razorpay-Event-Id": event_id,
    }


def _enable_webhooks(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings(_env_file=None, app_env="test", razorpay_webhook_secret=WEBHOOK_SECRET)
    monkeypatch.setattr("app.services.payment.get_settings", lambda: settings)
    keys = AP2KeySet(
        merchant=AP2Signer.generate("urn:test:merchant", "merchant-key-1"),
        trusted_surface=AP2Signer.generate("urn:test:agent-provider", "agent-provider-key-1"),
        credentials_provider=AP2Signer.generate("urn:test:cp", "cp-key-1"),
        payment_processor=AP2Signer.generate("urn:test:processor", "processor-key-1"),
        audience="agentbasket-commerce",
    )
    monkeypatch.setattr("app.services.ap2.get_ap2_key_set", lambda: keys)


@pytest.mark.anyio
async def test_captured_webhook_advances_schedule_once(
    client: httpx.AsyncClient,
    seeded: dict[str, object],
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_webhooks(monkeypatch)
    intent_id, run_id, checkout_id, payment_id = _scheduled_payment_graph(
        session_factory,
        seeded,
        suffix="capture",
        debit_submission_started=True,
    )
    raw = _payment_event("payment.captured", suffix="capture")

    first = await client.post(
        "/api/v1/webhooks/razorpay",
        headers=_webhook_headers(raw, "scheduled-capture-event-001"),
        content=raw,
    )
    second = await client.post(
        "/api/v1/webhooks/razorpay",
        headers=_webhook_headers(raw, "scheduled-capture-event-002"),
        content=raw,
    )

    assert first.status_code == 200 and first.json()["status"] == "processed"
    assert second.status_code == 200 and second.json()["status"] == "processed"
    with session_factory() as db:
        intent = db.get(ScheduledPurchaseIntent, intent_id)
        run = db.get(ScheduledPurchaseRun, run_id)
        checkout = db.get(Checkout, checkout_id)
        payment = db.get(Payment, payment_id)
        assert intent is not None and intent.status == PurchaseIntentStatus.ACTIVE
        assert intent.successful_occurrences == 1
        assert intent.spent_minor == 25_000
        assert intent.next_execution_at == run.scheduled_for + timedelta(weeks=1)
        assert run is not None and run.status == ScheduledRunStatus.SUCCEEDED
        assert checkout is not None and checkout.status == CheckoutStatus.COMPLETED
        assert payment is not None and payment.status == PaymentStatus.CAPTURED
        receipts = list(db.query(Ap2Receipt).filter(Ap2Receipt.checkout_id == checkout_id).all())
        assert {receipt.receipt_type for receipt in receipts} == {"checkout", "payment"}


@pytest.mark.anyio
async def test_failed_webhook_requires_human_action_without_spending_budget(
    client: httpx.AsyncClient,
    seeded: dict[str, object],
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_webhooks(monkeypatch)
    intent_id, run_id, checkout_id, payment_id = _scheduled_payment_graph(
        session_factory, seeded, suffix="failure"
    )
    with session_factory() as db, db.begin():
        reservation_id = _add_inventory_reservation(db, seeded, checkout_id)
    raw = _payment_event("payment.failed", suffix="failure")

    response = await client.post(
        "/api/v1/webhooks/razorpay",
        headers=_webhook_headers(raw, "scheduled-failure-event-001"),
        content=raw,
    )

    assert response.status_code == 200 and response.json()["status"] == "processed"
    with session_factory() as db:
        intent = db.get(ScheduledPurchaseIntent, intent_id)
        run = db.get(ScheduledPurchaseRun, run_id)
        checkout = db.get(Checkout, checkout_id)
        payment = db.get(Payment, payment_id)
        order = db.get(Order, run.order_id if run else None)
        reservation = db.get(InventoryReservation, reservation_id)
        inventory = db.get(InventoryItem, seeded["inventory_id"])
        assert intent is not None and intent.status == PurchaseIntentStatus.NEEDS_ATTENTION
        assert intent.successful_occurrences == 0
        assert intent.spent_minor == 0
        assert intent.last_failure_code == "BAD_REQUEST_ERROR"
        assert intent.next_execution_at == run.scheduled_for + timedelta(weeks=1)
        assert run is not None and run.status == ScheduledRunStatus.REQUIRES_HUMAN_ACTION
        assert checkout is not None and checkout.status == CheckoutStatus.CANCELED
        assert order is not None and order.status == OrderStatus.CANCELED
        assert payment is not None and payment.status == PaymentStatus.FAILED
        assert reservation is not None and reservation.status == ReservationStatus.RELEASED
        assert inventory is not None and inventory.reserved_quantity == 0


@pytest.mark.anyio
@pytest.mark.parametrize(
    "protected_status",
    [
        PurchaseIntentStatus.PAUSED,
        PurchaseIntentStatus.REVOKED,
        PurchaseIntentStatus.EXPIRED,
        PurchaseIntentStatus.PENDING_PROVIDER_AUTHORIZATION,
    ],
)
async def test_late_capture_records_payment_without_reactivating_controlled_schedule(
    protected_status: PurchaseIntentStatus,
    client: httpx.AsyncClient,
    seeded: dict[str, object],
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_webhooks(monkeypatch)
    suffix = f"late-capture-{protected_status.value}"
    intent_id, run_id, _, _ = _scheduled_payment_graph(
        session_factory,
        seeded,
        suffix=suffix,
        intent_status=protected_status,
        debit_submission_started=True,
    )
    with session_factory() as db, db.begin():
        intent = db.get(ScheduledPurchaseIntent, intent_id)
        assert intent is not None
        intent.last_failure_code = "provider_token_cancelled"
        intent.last_failure_message = "Recurring authorization is disabled."
    raw = _payment_event("payment.captured", suffix=suffix)

    response = await client.post(
        "/api/v1/webhooks/razorpay",
        headers=_webhook_headers(raw, f"event-{suffix}"),
        content=raw,
    )

    assert response.status_code == 200
    with session_factory() as db:
        intent = db.get(ScheduledPurchaseIntent, intent_id)
        run = db.get(ScheduledPurchaseRun, run_id)
        assert intent is not None and intent.status == protected_status
        assert intent.next_run_at is None
        assert intent.successful_occurrences == 1
        assert intent.spent_minor == 25_000
        assert intent.last_failure_code == "provider_token_cancelled"
        assert run is not None and run.status == ScheduledRunStatus.SUCCEEDED


@pytest.mark.anyio
@pytest.mark.parametrize(
    "protected_status",
    [
        PurchaseIntentStatus.PAUSED,
        PurchaseIntentStatus.REVOKED,
        PurchaseIntentStatus.EXPIRED,
        PurchaseIntentStatus.PENDING_PROVIDER_AUTHORIZATION,
    ],
)
async def test_late_failure_records_run_without_overwriting_controlled_schedule(
    protected_status: PurchaseIntentStatus,
    client: httpx.AsyncClient,
    seeded: dict[str, object],
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_webhooks(monkeypatch)
    suffix = f"late-failure-{protected_status.value}"
    intent_id, run_id, _, _ = _scheduled_payment_graph(
        session_factory,
        seeded,
        suffix=suffix,
        intent_status=protected_status,
    )
    with session_factory() as db, db.begin():
        intent = db.get(ScheduledPurchaseIntent, intent_id)
        assert intent is not None
        intent.last_failure_code = "provider_token_cancelled"
        intent.last_failure_message = "Recurring authorization is disabled."
    raw = _payment_event("payment.failed", suffix=suffix)

    response = await client.post(
        "/api/v1/webhooks/razorpay",
        headers=_webhook_headers(raw, f"event-{suffix}"),
        content=raw,
    )

    assert response.status_code == 200
    with session_factory() as db:
        intent = db.get(ScheduledPurchaseIntent, intent_id)
        run = db.get(ScheduledPurchaseRun, run_id)
        assert intent is not None and intent.status == protected_status
        assert intent.next_run_at is None
        assert intent.successful_occurrences == 0
        assert intent.spent_minor == 0
        assert intent.last_failure_code == "provider_token_cancelled"
        assert run is not None
        assert run.status == ScheduledRunStatus.REQUIRES_HUMAN_ACTION
        assert run.failure_code == "BAD_REQUEST_ERROR"


def test_worker_claims_only_active_due_schedule_once(
    session_factory: sessionmaker[Session], seeded: dict[str, object]
) -> None:
    now = datetime.now(UTC)
    with session_factory() as db, db.begin():
        due = _intent(
            seeded,
            status=PurchaseIntentStatus.ACTIVE,
            scheduled_for=now + timedelta(hours=2),
            next_run_at=now - timedelta(minutes=1),
            expires_at=now + timedelta(days=10),
            suffix="due",
        )
        paused = _intent(
            seeded,
            status=PurchaseIntentStatus.PAUSED,
            scheduled_for=now + timedelta(hours=2),
            next_run_at=now - timedelta(minutes=1),
            expires_at=now + timedelta(days=10),
            suffix="paused",
        )
        expired = _intent(
            seeded,
            status=PurchaseIntentStatus.ACTIVE,
            scheduled_for=now - timedelta(hours=2),
            next_run_at=now - timedelta(minutes=1),
            expires_at=now - timedelta(minutes=1),
            suffix="expired",
        )
        db.add_all([due, paused, expired])
        db.flush()
        due_id, paused_id, expired_id = due.id, paused.id, expired.id

    with session_factory() as db:
        executor = ScheduledPurchaseExecutor(db)
        claimed = executor.claim_due(now)
        repeated = executor.claim_due(now)

    assert len(claimed) == 1
    assert repeated == []
    with session_factory() as db:
        due = db.get(ScheduledPurchaseIntent, due_id)
        paused = db.get(ScheduledPurchaseIntent, paused_id)
        expired = db.get(ScheduledPurchaseIntent, expired_id)
        run = db.get(ScheduledPurchaseRun, claimed[0])
        assert due is not None and due.next_run_at is None
        assert paused is not None and paused.status == PurchaseIntentStatus.PAUSED
        assert expired is not None and expired.status == PurchaseIntentStatus.EXPIRED
        assert run is not None and run.status == ScheduledRunStatus.CLAIMED
        assert run.attempt_count == 1


def test_run_now_idempotency_replays_original_run_after_schedule_advances(
    session_factory: sessionmaker[Session], seeded: dict[str, object]
) -> None:
    now = datetime.now(UTC)
    with session_factory() as db, db.begin():
        intent = _intent(
            seeded,
            status=PurchaseIntentStatus.ACTIVE,
            scheduled_for=now + timedelta(days=1),
            next_run_at=now,
            expires_at=now + timedelta(days=30),
            suffix="run-now-idempotency",
        )
        db.add(intent)
        db.flush()
        intent_id = intent.id

    with session_factory() as db:
        customer = db.get(UserAccount, seeded["customer_id"])
        assert customer is not None
        db.expunge(customer)
        db.rollback()
        executor = ScheduledPurchaseExecutor(db)
        executor.settings = Settings(_env_file=None, app_env="test")
        first_run_id = executor.claim_for_test(intent_id, customer, "run-now-key-0001")
        with db.begin():
            first_run = db.get(ScheduledPurchaseRun, first_run_id)
            intent = db.get(ScheduledPurchaseIntent, intent_id)
            assert first_run is not None and intent is not None
            first_run.status = ScheduledRunStatus.SUCCEEDED
            intent.next_execution_at = first_run.scheduled_for + timedelta(weeks=1)
            intent.next_run_at = intent.next_execution_at - timedelta(days=1)

        replayed_run_id = executor.claim_for_test(intent_id, customer, "run-now-key-0001")
        next_run_id = executor.claim_for_test(intent_id, customer, "run-now-key-0002")

    assert replayed_run_id == first_run_id
    assert next_run_id != first_run_id
    with session_factory() as db:
        runs = list(
            db.query(ScheduledPurchaseRun)
            .filter(ScheduledPurchaseRun.intent_id == intent_id)
            .order_by(ScheduledPurchaseRun.scheduled_for)
        )
        assert len(runs) == 2
        assert runs[1].scheduled_for == runs[0].scheduled_for + timedelta(weeks=1)
        assert runs[0].idempotency_key != runs[1].idempotency_key


def test_merchant_checkout_jwt_binds_scheduled_constraints() -> None:
    now = datetime.now(UTC)
    scheduled_for = now + timedelta(days=2)
    address = {"line_one": "12, 100 Feet Road", "postal_code": "560038"}
    address_hash = canonical_sha256(address)
    modifier_id = uuid.uuid4()
    location_id = uuid.uuid4()
    checkout = SimpleNamespace(
        id=uuid.uuid4(),
        expires_at=scheduled_for + timedelta(minutes=10),
        location_id=location_id,
        currency="INR",
        subtotal_minor=25_000,
        delivery_minor=4_900,
        total_minor=29_900,
        quote_version=1,
        fulfillment_type=FulfillmentType.LOCAL_DELIVERY,
        lines=[
            SimpleNamespace(
                id=uuid.uuid4(),
                variant_id=uuid.uuid4(),
                product_name="Ginger Tea",
                variant_name="Regular",
                unit_price_minor=25_000,
                modifier_total_minor=0,
                quantity=1,
                line_total_minor=25_000,
                modifiers=[SimpleNamespace(modifier_option_id=modifier_id)],
            )
        ],
    )
    merchant = SimpleNamespace(id=uuid.uuid4(), name="Ember & Leaf")
    intent = SimpleNamespace(
        address_snapshot=address,
        constraints={"address_sha256": address_hash},
    )
    keys = AP2KeySet(
        merchant=AP2Signer.generate("urn:test:merchant", "merchant-key-1"),
        trusted_surface=AP2Signer.generate("urn:test:agent-provider", "agent-key-1"),
        credentials_provider=AP2Signer.generate("urn:test:cp", "cp-key-1"),
        payment_processor=AP2Signer.generate("urn:test:processor", "processor-key-1"),
        audience="agentbasket-commerce",
    )

    executor = ScheduledPurchaseExecutor.__new__(ScheduledPurchaseExecutor)
    executor._keys = keys
    token = executor._merchant_checkout_jwt(checkout, merchant, intent, scheduled_for)
    claims = keys.merchant.verify(token, keys.audience)
    assert claims["line_items"][0]["agentbasket"]["modifier_option_ids"] == [str(modifier_id)]
    assert claims["agentbasket"] == {
        "source": "scheduled_agent",
        "quote_version": 1,
        "fulfillment_type": FulfillmentType.LOCAL_DELIVERY.value,
        "location_id": str(location_id),
        "address_sha256": address_hash,
        "scheduled_for": scheduled_for.isoformat(),
    }


def _add_inventory_reservation(
    db: Session,
    seeded: dict[str, object],
    checkout_id: uuid.UUID,
) -> uuid.UUID:
    line = CheckoutLineItem(
        checkout_id=checkout_id,
        variant_id=seeded["bean_variant_id"],
        quantity=1,
        unit_price_minor=25_000,
        modifier_total_minor=0,
        line_total_minor=25_000,
        product_snapshot={"product_name": "Citrus Bloom Coffee", "variant_name": "500 g"},
    )
    db.add(line)
    db.flush()
    inventory = db.get(InventoryItem, seeded["inventory_id"])
    assert inventory is not None
    inventory.reserved_quantity += 1
    reservation = InventoryReservation(
        checkout_line_item_id=line.id,
        inventory_item_id=inventory.id,
        quantity=1,
        status=ReservationStatus.ACTIVE,
        expires_at=datetime.now(UTC) + timedelta(hours=2),
    )
    db.add(reservation)
    db.flush()
    return reservation.id


def test_paused_schedule_is_terminalized_without_submitting_debit(
    session_factory: sessionmaker[Session], seeded: dict[str, object]
) -> None:
    intent_id, run_id, checkout_id, payment_id = _scheduled_payment_graph(
        session_factory, seeded, suffix="paused-before-debit"
    )
    with session_factory() as db, db.begin():
        intent = db.get(ScheduledPurchaseIntent, intent_id)
        assert intent is not None
        intent.status = PurchaseIntentStatus.PAUSED
        reservation_id = _add_inventory_reservation(db, seeded, checkout_id)

    gateway = FakeDebitGateway()
    with session_factory() as db:
        result = ScheduledPurchaseExecutor(db).submit_debit(run_id, gateway)

    assert result.status == ScheduledRunStatus.SKIPPED
    assert gateway.debit_requests == []
    with session_factory() as db:
        intent = db.get(ScheduledPurchaseIntent, intent_id)
        run = db.get(ScheduledPurchaseRun, run_id)
        checkout = db.get(Checkout, checkout_id)
        order = db.get(Order, run.order_id if run else None)
        payment = db.get(Payment, payment_id)
        reservation = db.get(InventoryReservation, reservation_id)
        inventory = db.get(InventoryItem, seeded["inventory_id"])
        assert intent is not None and intent.status == PurchaseIntentStatus.PAUSED
        assert run is not None and run.failure_code == "scheduled_purchase_not_active"
        assert checkout is not None and checkout.status == CheckoutStatus.CANCELED
        assert order is not None and order.status == OrderStatus.CANCELED
        assert payment is not None and payment.status == PaymentStatus.FAILED
        assert reservation is not None and reservation.status == ReservationStatus.RELEASED
        assert inventory is not None and inventory.reserved_quantity == 0


def test_cancelled_provider_token_blocks_debit_and_requires_new_authorization(
    session_factory: sessionmaker[Session], seeded: dict[str, object]
) -> None:
    intent_id, run_id, checkout_id, payment_id = _scheduled_payment_graph(
        session_factory, seeded, suffix="cancelled-token"
    )
    with session_factory() as db, db.begin():
        intent = db.get(ScheduledPurchaseIntent, intent_id)
        run = db.get(ScheduledPurchaseRun, run_id)
        instrument = db.get(PaymentInstrument, seeded["payment_instrument_id"])
        assert intent is not None and run is not None and instrument is not None
        instrument.instrument_type = "com.razorpay.upi.autopay"
        instrument.provider_customer_id = "cust_cancelled"
        instrument.provider_token_reference = "token_cancelled"
        instrument.instrument_metadata = {
            "token_status": "cancelled",
            "mandate_max_amount_minor": 30_000,
            "mandate_expires_at": int(intent.expires_at.timestamp()),
        }
        intent.payment_token_reference = instrument.provider_token_reference

    gateway = FakeDebitGateway()
    with session_factory() as db:
        result = ScheduledPurchaseExecutor(db).submit_debit(run_id, gateway)

    assert result.status == ScheduledRunStatus.SKIPPED
    assert gateway.debit_requests == []
    with session_factory() as db:
        intent = db.get(ScheduledPurchaseIntent, intent_id)
        checkout = db.get(Checkout, checkout_id)
        payment = db.get(Payment, payment_id)
        assert intent is not None
        assert intent.status == PurchaseIntentStatus.PENDING_PROVIDER_AUTHORIZATION
        assert intent.last_failure_code == "recurring_payment_authorization_required"
        assert checkout is not None and checkout.status == CheckoutStatus.CANCELED
        assert payment is not None and payment.status == PaymentStatus.FAILED


def test_expired_checkout_is_rejected_before_debit_submission(
    session_factory: sessionmaker[Session], seeded: dict[str, object]
) -> None:
    intent_id, run_id, checkout_id, _ = _scheduled_payment_graph(
        session_factory, seeded, suffix="expired-checkout-before-debit"
    )
    with session_factory() as db, db.begin():
        checkout = db.get(Checkout, checkout_id)
        assert checkout is not None
        checkout.expires_at = datetime.now(UTC) - timedelta(seconds=1)

    gateway = FakeDebitGateway()
    with session_factory() as db:
        result = ScheduledPurchaseExecutor(db).submit_debit(run_id, gateway)

    assert result.status == ScheduledRunStatus.SKIPPED
    assert result.failure_code == "scheduled_checkout_expired"
    assert gateway.debit_requests == []
    with session_factory() as db:
        intent = db.get(ScheduledPurchaseIntent, intent_id)
        run = db.get(ScheduledPurchaseRun, run_id)
        checkout = db.get(Checkout, checkout_id)
        assert intent is not None and intent.status == PurchaseIntentStatus.NEEDS_ATTENTION
        assert run is not None
        assert "debit_submission_started_at" not in (run.evidence or {})
        assert checkout is not None and checkout.status == CheckoutStatus.CANCELED


def test_changed_checkout_terms_are_rejected_before_debit_submission(
    session_factory: sessionmaker[Session], seeded: dict[str, object]
) -> None:
    intent_id, run_id, checkout_id, payment_id = _scheduled_payment_graph(
        session_factory, seeded, suffix="changed-terms-before-debit"
    )
    with session_factory() as db, db.begin():
        payment = db.get(Payment, payment_id)
        assert payment is not None
        payment.amount_minor += 1

    gateway = FakeDebitGateway()
    with session_factory() as db:
        result = ScheduledPurchaseExecutor(db).submit_debit(run_id, gateway)

    assert result.status == ScheduledRunStatus.SKIPPED
    assert result.failure_code == "scheduled_execution_terms_changed"
    assert gateway.debit_requests == []
    with session_factory() as db:
        intent = db.get(ScheduledPurchaseIntent, intent_id)
        run = db.get(ScheduledPurchaseRun, run_id)
        checkout = db.get(Checkout, checkout_id)
        assert intent is not None and intent.status == PurchaseIntentStatus.NEEDS_ATTENTION
        assert run is not None
        assert "debit_submission_started_at" not in (run.evidence or {})
        assert checkout is not None and checkout.status == CheckoutStatus.CANCELED


def test_tampered_ap2_chain_is_reverified_and_rejected_before_debit(
    session_factory: sessionmaker[Session],
    seeded: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    intent_id, run_id, checkout_id, _ = _scheduled_payment_graph(
        session_factory, seeded, suffix="tampered-ap2-before-debit"
    )
    with session_factory() as db, db.begin():
        intent = db.get(ScheduledPurchaseIntent, intent_id)
        run = db.get(ScheduledPurchaseRun, run_id)
        instrument = db.get(PaymentInstrument, seeded["payment_instrument_id"])
        assert intent is not None and run is not None and instrument is not None
        instrument.instrument_type = "com.razorpay.upi.autopay"
        instrument.provider_customer_id = "cust_tampered"
        instrument.provider_token_reference = "token_tampered"
        instrument.instrument_metadata = {"token_status": "confirmed"}
        intent.payment_token_reference = instrument.provider_token_reference
        intent.open_checkout_hash = "open-checkout-hash"
        run.merchant_checkout_jwt = "tampered-merchant-checkout"
        run.merchant_checkout_hash = "merchant-checkout-hash"
        run.evidence = {"ap2": {"nonce": "stored-run-nonce"}}
    monkeypatch.setattr(
        "app.services.scheduled_execution.recurring_instrument_ready",
        lambda instrument, intent: True,
    )

    def reject_tampered_chain(**_: object) -> None:
        raise ValueError("closed checkout signature changed")

    monkeypatch.setattr(
        "app.services.scheduled_execution.verify_existing_closed_mandates",
        reject_tampered_chain,
    )
    keys = AP2KeySet(
        merchant=AP2Signer.generate("urn:test:merchant", "merchant-key-1"),
        trusted_surface=AP2Signer.generate("urn:test:agent-provider", "agent-key-1"),
        credentials_provider=AP2Signer.generate("urn:test:cp", "cp-key-1"),
        payment_processor=AP2Signer.generate("urn:test:processor", "processor-key-1"),
        audience="agentbasket-commerce",
    )
    gateway = FakeDebitGateway()
    with session_factory() as db:
        result = ScheduledPurchaseExecutor(db, keys).submit_debit(run_id, gateway)

    assert result.status == ScheduledRunStatus.SKIPPED
    assert result.failure_code == "ap2_autonomous_verification_failed"
    assert gateway.debit_requests == []
    with session_factory() as db:
        run = db.get(ScheduledPurchaseRun, run_id)
        checkout = db.get(Checkout, checkout_id)
        assert run is not None
        assert "debit_submission_started_at" not in (run.evidence or {})
        assert checkout is not None and checkout.status == CheckoutStatus.CANCELED


def test_debit_revalidates_existing_credentials_provider_release(
    session_factory: sessionmaker[Session],
    seeded: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    intent_id, run_id, checkout_id, _ = _scheduled_payment_graph(
        session_factory, seeded, suffix="cp-revalidation-before-debit"
    )
    keys = AP2KeySet(
        merchant=AP2Signer.generate("urn:test:merchant", "merchant-key-1"),
        trusted_surface=AP2Signer.generate("urn:test:agent-provider", "agent-key-1"),
        credentials_provider=AP2Signer.generate("urn:test:cp", "cp-key-1"),
        payment_processor=AP2Signer.generate("urn:test:processor", "processor-key-1"),
        audience="agentbasket-commerce",
    )
    with session_factory() as db, db.begin():
        intent = db.get(ScheduledPurchaseIntent, intent_id)
        run = db.get(ScheduledPurchaseRun, run_id)
        checkout = db.get(Checkout, checkout_id)
        customer = db.get(UserAccount, seeded["customer_id"])
        instrument = db.get(PaymentInstrument, seeded["payment_instrument_id"])
        assert all((intent, run, checkout, customer, instrument))
        instrument.instrument_type = "com.razorpay.upi.autopay"
        instrument.provider_customer_id = "cust_cp_revalidation"
        instrument.provider_token_reference = "token_cp_revalidation"
        instrument.instrument_metadata = {"token_status": "confirmed"}
        intent.payment_token_reference = instrument.provider_token_reference
        intent.open_checkout_hash = "open-checkout-hash"
        run.merchant_checkout_jwt = "merchant-checkout-cp"
        run.merchant_checkout_hash = "merchant-checkout-hash-cp"
        signed_grant = keys.credentials_provider.sign(
            {
                "iss": keys.credentials_provider.issuer,
                "sub": str(customer.id),
                "aud": keys.audience,
                "jti": f"ap2-autonomous-cp-grant:{run.id}",
                "iat": int(datetime.now(UTC).timestamp()),
                "exp": int(intent.expires_at.timestamp()),
                "scheduled_purchase_id": str(intent.id),
                "scheduled_run_id": str(run.id),
                "checkout_hash": run.merchant_checkout_hash,
                "payment_instrument_id": str(instrument.id),
                "amount_minor": checkout.total_minor,
                "currency": checkout.currency,
                "provider_token_sha256": hashlib.sha256(
                    instrument.provider_token_reference.encode()
                ).hexdigest(),
            }
        )
        scope_sha256 = hashlib.sha256(
            json.dumps(
                {
                    "run_id": str(run.id),
                    "checkout_hash": run.merchant_checkout_hash,
                    "amount_minor": checkout.total_minor,
                    "currency": checkout.currency,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        run.evidence = {
            "ap2": {"nonce": "cp-revalidation-nonce"},
            "credentials_provider": {
                "signed_grant": signed_grant,
                "grant_sha256": hashlib.sha256(signed_grant.encode()).hexdigest(),
                "scope_sha256": scope_sha256,
            },
        }
    monkeypatch.setattr(
        "app.services.scheduled_execution.recurring_instrument_ready",
        lambda instrument, intent: True,
    )
    monkeypatch.setattr(
        "app.services.scheduled_execution.verify_existing_closed_mandates",
        lambda **kwargs: None,
    )

    gateway = FakeDebitGateway()
    with session_factory() as db:
        result = ScheduledPurchaseExecutor(db, keys).submit_debit(run_id, gateway)

    assert result.status == ScheduledRunStatus.PAYMENT_PENDING
    assert len(gateway.debit_requests) == 1
    with session_factory() as db:
        run = db.get(ScheduledPurchaseRun, run_id)
        assert run is not None
        assert (run.evidence or {})["credentials_provider"]["last_revalidated_at"]
        assert "token_cp_revalidation" not in json.dumps(run.evidence)


def test_submitted_or_ambiguous_payment_pending_run_is_never_blindly_retried(
    session_factory: sessionmaker[Session],
    seeded: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    intent_id, run_id, _, _ = _scheduled_payment_graph(session_factory, seeded, suffix="debit-once")
    with session_factory() as db, db.begin():
        intent = db.get(ScheduledPurchaseIntent, intent_id)
        run = db.get(ScheduledPurchaseRun, run_id)
        instrument = db.get(PaymentInstrument, seeded["payment_instrument_id"])
        assert intent is not None and run is not None and instrument is not None
        instrument.instrument_type = "com.razorpay.upi.autopay"
        instrument.provider_customer_id = "cust_ready"
        instrument.provider_token_reference = "token_ready"
        instrument.instrument_metadata = {"token_status": "confirmed"}
        intent.payment_token_reference = instrument.provider_token_reference
        run.merchant_checkout_jwt = "merchant-checkout"
        run.closed_checkout_mandate = "closed-checkout"
        run.closed_payment_mandate = "closed-payment"
    monkeypatch.setattr(
        "app.services.scheduled_execution.recurring_instrument_ready",
        lambda instrument, intent: True,
    )
    monkeypatch.setattr(
        ScheduledPurchaseExecutor,
        "_revalidate_debit_authority",
        lambda self, run, intent, checkout, customer, instrument: (
            "cust_ready",
            "token_ready",
        ),
    )

    gateway = FakeDebitGateway()
    with session_factory() as db:
        first = ScheduledPurchaseExecutor(db).submit_debit(run_id, gateway)
        second = ScheduledPurchaseExecutor(db).submit_debit(run_id, gateway)

    assert first.status == ScheduledRunStatus.PAYMENT_PENDING
    assert second.status == ScheduledRunStatus.PAYMENT_PENDING
    assert len(gateway.debit_requests) == 1

    _, ambiguous_run_id, _, _ = _scheduled_payment_graph(
        session_factory, seeded, suffix="debit-ambiguous"
    )
    with session_factory() as db, db.begin():
        run = db.get(ScheduledPurchaseRun, ambiguous_run_id)
        assert run is not None
        run.evidence = {"debit_submission_started_at": datetime.now(UTC).isoformat()}
    with session_factory() as db:
        ambiguous = ScheduledPurchaseExecutor(db).submit_debit(ambiguous_run_id, gateway)

    assert ambiguous.status == ScheduledRunStatus.REQUIRES_HUMAN_ACTION
    assert ambiguous.failure_code == "scheduled_debit_submission_ambiguous"
    assert len(gateway.debit_requests) == 1


def test_early_webhook_payment_binding_is_never_overwritten_by_rest_response(
    session_factory: sessionmaker[Session],
    seeded: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    intent_id, run_id, _, payment_id = _scheduled_payment_graph(
        session_factory, seeded, suffix="early-webhook-binding-race"
    )
    with session_factory() as db, db.begin():
        intent = db.get(ScheduledPurchaseIntent, intent_id)
        run = db.get(ScheduledPurchaseRun, run_id)
        instrument = db.get(PaymentInstrument, seeded["payment_instrument_id"])
        assert intent is not None and run is not None and instrument is not None
        instrument.instrument_type = "com.razorpay.upi.autopay"
        instrument.provider_customer_id = "cust_early_webhook"
        instrument.provider_token_reference = "token_early_webhook"
        instrument.instrument_metadata = {"token_status": "confirmed"}
        intent.payment_token_reference = instrument.provider_token_reference
        run.merchant_checkout_jwt = "merchant-checkout-early-webhook"
    monkeypatch.setattr(
        "app.services.scheduled_execution.recurring_instrument_ready",
        lambda instrument, intent: True,
    )
    monkeypatch.setattr(
        ScheduledPurchaseExecutor,
        "_revalidate_debit_authority",
        lambda self, run, intent, checkout, customer, instrument: (
            "cust_early_webhook",
            "token_early_webhook",
        ),
    )

    class EarlyWebhookGateway(FakeDebitGateway):
        def create_recurring_payment(self, **request: object) -> RazorpayRecurringDebit:
            self.debit_requests.append(request)
            with session_factory() as webhook_db, webhook_db.begin():
                raced_run = webhook_db.get(ScheduledPurchaseRun, run_id)
                raced_payment = webhook_db.get(Payment, payment_id)
                assert raced_run is not None and raced_payment is not None
                raced_run.provider_payment_id = "pay_verified_by_early_webhook"
                raced_payment.provider_payment_id = "pay_verified_by_early_webhook"
            return RazorpayRecurringDebit(
                payment_id="pay_different_rest_response",
                order_id=str(request["order_id"]),
            )

    gateway = EarlyWebhookGateway()
    with session_factory() as db:
        result = ScheduledPurchaseExecutor(db).submit_debit(run_id, gateway)

    assert len(gateway.debit_requests) == 1
    assert result.status == ScheduledRunStatus.REQUIRES_HUMAN_ACTION
    assert result.failure_code == "scheduled_payment_binding_mismatch"
    with session_factory() as db:
        intent = db.get(ScheduledPurchaseIntent, intent_id)
        run = db.get(ScheduledPurchaseRun, run_id)
        payment = db.get(Payment, payment_id)
        assert intent is not None and intent.status == PurchaseIntentStatus.NEEDS_ATTENTION
        assert run is not None
        assert run.provider_payment_id == "pay_verified_by_early_webhook"
        assert payment is not None
        assert payment.provider_payment_id == "pay_verified_by_early_webhook"


@pytest.mark.parametrize(
    "protected_status",
    [
        PurchaseIntentStatus.PAUSED,
        PurchaseIntentStatus.REVOKED,
        PurchaseIntentStatus.COMPLETED,
        PurchaseIntentStatus.EXPIRED,
    ],
)
def test_provider_error_race_preserves_user_controlled_schedule_state(
    protected_status: PurchaseIntentStatus,
    session_factory: sessionmaker[Session],
    seeded: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    suffix = f"provider-race-{protected_status.value}"
    intent_id, run_id, _, _ = _scheduled_payment_graph(session_factory, seeded, suffix=suffix)
    with session_factory() as db, db.begin():
        intent = db.get(ScheduledPurchaseIntent, intent_id)
        run = db.get(ScheduledPurchaseRun, run_id)
        instrument = db.get(PaymentInstrument, seeded["payment_instrument_id"])
        assert intent is not None and run is not None and instrument is not None
        instrument.instrument_type = "com.razorpay.upi.autopay"
        instrument.provider_customer_id = "cust_race"
        instrument.provider_token_reference = "token_race"
        instrument.instrument_metadata = {"token_status": "confirmed"}
        intent.payment_token_reference = instrument.provider_token_reference
        run.merchant_checkout_jwt = "merchant-checkout-race"
    monkeypatch.setattr(
        "app.services.scheduled_execution.recurring_instrument_ready",
        lambda instrument, intent: True,
    )
    monkeypatch.setattr(
        ScheduledPurchaseExecutor,
        "_revalidate_debit_authority",
        lambda self, run, intent, checkout, customer, instrument: (
            "cust_race",
            "token_race",
        ),
    )

    class ProviderRaceGateway(FakeDebitGateway):
        def create_recurring_payment(self, **request: object) -> RazorpayRecurringDebit:
            self.debit_requests.append(request)
            with session_factory() as race_db, race_db.begin():
                raced_intent = race_db.get(ScheduledPurchaseIntent, intent_id)
                assert raced_intent is not None
                raced_intent.status = protected_status
                raced_intent.next_run_at = None
                raced_intent.last_failure_code = "user_controlled_state"
            raise RuntimeError("provider transport failed after user action")

    gateway = ProviderRaceGateway()
    with session_factory() as db:
        result = ScheduledPurchaseExecutor(db).submit_debit(run_id, gateway)

    assert len(gateway.debit_requests) == 1
    assert result.status == ScheduledRunStatus.REQUIRES_HUMAN_ACTION
    assert result.failure_code == "scheduled_debit_submission_ambiguous"
    with session_factory() as db:
        intent = db.get(ScheduledPurchaseIntent, intent_id)
        assert intent is not None and intent.status == protected_status
        assert intent.next_run_at is None
        assert intent.last_failure_code == "user_controlled_state"


def test_recovery_requeues_only_checkout_created_before_provider_contact(
    session_factory: sessionmaker[Session], seeded: dict[str, object]
) -> None:
    now = datetime.now(UTC)
    intent_id, recoverable_id, _, _ = _scheduled_payment_graph(
        session_factory, seeded, suffix="recoverable-checkout"
    )
    _, ambiguous_id, ambiguous_checkout_id, _ = _scheduled_payment_graph(
        session_factory, seeded, suffix="ambiguous-notification"
    )
    _, payment_pending_id, _, _ = _scheduled_payment_graph(
        session_factory, seeded, suffix="payment-not-retried"
    )
    _, safe_payment_pending_id, _, _ = _scheduled_payment_graph(
        session_factory, seeded, suffix="payment-safe-to-reclaim"
    )
    with session_factory() as db, db.begin():
        recoverable = db.get(ScheduledPurchaseRun, recoverable_id)
        ambiguous = db.get(ScheduledPurchaseRun, ambiguous_id)
        payment_pending = db.get(ScheduledPurchaseRun, payment_pending_id)
        safe_payment_pending = db.get(ScheduledPurchaseRun, safe_payment_pending_id)
        assert (
            recoverable is not None
            and ambiguous is not None
            and payment_pending is not None
            and safe_payment_pending is not None
        )
        recoverable.status = ScheduledRunStatus.CHECKOUT_CREATED
        recoverable.started_at = now - timedelta(minutes=20)
        recoverable.evidence = {"checkout_created_at": (now - timedelta(minutes=19)).isoformat()}
        ambiguous.status = ScheduledRunStatus.CHECKOUT_CREATED
        ambiguous.started_at = now - timedelta(minutes=20)
        ambiguous.evidence = {
            "notification_submission_started_at": (now - timedelta(minutes=19)).isoformat()
        }
        payment_pending.started_at = now - timedelta(minutes=20)
        payment_pending.evidence = {
            "debit_submission_started_at": (now - timedelta(minutes=19)).isoformat()
        }
        safe_payment_pending.started_at = now - timedelta(minutes=20)
        safe_payment_pending.claimed_at = now - timedelta(minutes=20)
        safe_payment_pending.evidence = {
            "debit_claimed_at": (now - timedelta(minutes=20)).isoformat()
        }

    with session_factory() as db:
        recovered = ScheduledPurchaseExecutor(db).recover_stale_claims(now)

    assert recovered == 2
    with session_factory() as db:
        intent = db.get(ScheduledPurchaseIntent, intent_id)
        recoverable = db.get(ScheduledPurchaseRun, recoverable_id)
        ambiguous = db.get(ScheduledPurchaseRun, ambiguous_id)
        ambiguous_checkout = db.get(Checkout, ambiguous_checkout_id)
        payment_pending = db.get(ScheduledPurchaseRun, payment_pending_id)
        safe_payment_pending = db.get(ScheduledPurchaseRun, safe_payment_pending_id)
        assert intent is not None and as_utc(intent.next_run_at) == as_utc(now)
        assert recoverable is not None and recoverable.status == ScheduledRunStatus.PENDING
        assert ambiguous is not None
        assert ambiguous.status == ScheduledRunStatus.REQUIRES_HUMAN_ACTION
        assert ambiguous.failure_code == "scheduled_notification_submission_ambiguous"
        assert ambiguous_checkout is not None
        assert ambiguous_checkout.status == CheckoutStatus.CANCELED
        assert payment_pending is not None
        assert payment_pending.status == ScheduledRunStatus.REQUIRES_HUMAN_ACTION
        assert payment_pending.failure_code == "scheduled_debit_submission_ambiguous"
        assert safe_payment_pending is not None
        assert safe_payment_pending.status == ScheduledRunStatus.NOTIFICATION_PENDING
        assert safe_payment_pending.claimed_at is None
        assert safe_payment_pending.started_at is None


def test_claim_due_debits_persists_staleness_marker(
    session_factory: sessionmaker[Session], seeded: dict[str, object]
) -> None:
    now = datetime.now(UTC)
    _, run_id, _, _ = _scheduled_payment_graph(session_factory, seeded, suffix="debit-claim-marker")
    with session_factory() as db, db.begin():
        run = db.get(ScheduledPurchaseRun, run_id)
        assert run is not None
        run.status = ScheduledRunStatus.NOTIFICATION_PENDING
        run.provider_payment_after = now - timedelta(seconds=1)
        run.claimed_at = None
        run.started_at = None

    with session_factory() as db:
        claimed = ScheduledPurchaseExecutor(db).claim_due_debits(now)

    assert claimed == [run_id]
    with session_factory() as db:
        run = db.get(ScheduledPurchaseRun, run_id)
        assert run is not None and run.status == ScheduledRunStatus.PAYMENT_PENDING
        assert run.claimed_at is not None and as_utc(run.claimed_at) == as_utc(now)
        assert run.started_at is not None and as_utc(run.started_at) == as_utc(now)
