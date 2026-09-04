import hashlib
import hmac
import json
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings
from app.db.models import (
    Ap2Receipt,
    AuditEvent,
    Cart,
    Checkout,
    InventoryItem,
    InventoryReservation,
    Order,
    Payment,
    PaymentInstrument,
    ScheduledPurchaseIntent,
    ScheduledPurchaseRun,
    WebhookEvent,
)
from app.domain.enums import (
    CheckoutStatus,
    OrderStatus,
    PaymentStatus,
    PurchaseIntentStatus,
    ReservationStatus,
    ScheduledRunStatus,
)
from app.main import app
from app.payments.razorpay import (
    RazorpayOrder,
    RazorpayPayment,
    get_razorpay_gateway,
)
from app.protocols.ap2.crypto import AP2KeySet, AP2Signer


class FakeRazorpayGateway:
    key_id = "rzp_test_agentbasket"
    secret = "checkout-test-secret"

    def __init__(self) -> None:
        self.orders_by_receipt: dict[str, RazorpayOrder] = {}
        self.orders_by_id: dict[str, RazorpayOrder] = {}
        self.payments: dict[str, RazorpayPayment] = {}
        self.create_calls = 0

    def create_order(
        self, *, amount: int, currency: str, receipt: str, notes: dict[str, str]
    ) -> RazorpayOrder:
        self.create_calls += 1
        order = RazorpayOrder(
            id=f"order_test_{self.create_calls}",
            amount=amount,
            currency=currency,
            receipt=receipt,
            status="created",
        )
        self.orders_by_receipt[receipt] = order
        self.orders_by_id[order.id] = order
        return order

    def find_order_by_receipt(self, receipt: str) -> RazorpayOrder | None:
        return self.orders_by_receipt.get(receipt)

    def fetch_order(self, order_id: str) -> RazorpayOrder:
        return self.orders_by_id[order_id]

    def fetch_payment(self, payment_id: str) -> RazorpayPayment:
        return self.payments[payment_id]

    def verify_checkout_signature(self, *, order_id: str, payment_id: str, signature: str) -> bool:
        expected = hmac.new(
            self.secret.encode(), f"{order_id}|{payment_id}".encode(), hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(expected, signature)

    def set_payment(
        self,
        order_id: str,
        *,
        payment_id: str = "pay_test_captured",
        status: str = "captured",
        captured: bool = True,
    ) -> RazorpayPayment:
        order = self.orders_by_id[order_id]
        payment = RazorpayPayment(
            id=payment_id,
            order_id=order_id,
            amount=order.amount,
            currency=order.currency,
            status=status,
            captured=captured,
        )
        self.payments[payment.id] = payment
        if captured:
            paid_order = replace(order, status="paid", amount_paid=order.amount)
            self.orders_by_id[order_id] = paid_order
            self.orders_by_receipt[order.receipt] = paid_order
        return payment

    def signature(self, order_id: str, payment_id: str) -> str:
        return hmac.new(
            self.secret.encode(), f"{order_id}|{payment_id}".encode(), hashlib.sha256
        ).hexdigest()


def _auth(seeded: dict[str, object]) -> dict[str, str]:
    return {"Authorization": f"Bearer {seeded['customer_token']}"}


async def _checkout_from_cart(client: httpx.AsyncClient, seeded: dict[str, object]) -> dict:
    headers = _auth(seeded)
    added = await client.post(
        "/api/v1/cart/items",
        headers=headers,
        json={
            "variant_id": str(seeded["bean_variant_id"]),
            "quantity": 1,
            "modifier_option_ids": [str(seeded["whole_id"])],
        },
    )
    assert added.status_code == 201
    quoted = await client.post(
        "/api/v1/checkouts/from-cart",
        headers={**headers, "Idempotency-Key": "cart-checkout-payment-001"},
        json={
            "fulfillment_type": "local_delivery",
            "address_id": str(seeded["customer_address_id"]),
        },
    )
    assert quoted.status_code == 201
    return quoted.json()


async def _approve(client: httpx.AsyncClient, seeded: dict[str, object], checkout: dict) -> dict:
    response = await client.post(
        f"/api/v1/checkouts/{checkout['id']}/approve",
        headers=_auth(seeded),
        json={
            "expected_total_minor": checkout["total_minor"],
            "quote_version": checkout["quote_version"],
        },
    )
    assert response.status_code == 200
    return response.json()


async def _session(
    client: httpx.AsyncClient,
    seeded: dict[str, object],
    checkout: dict,
    gateway: FakeRazorpayGateway,
) -> dict:
    app.dependency_overrides[get_razorpay_gateway] = lambda: gateway
    response = await client.post(
        f"/api/v1/checkouts/{checkout['id']}/payment-session",
        headers=_auth(seeded),
    )
    assert response.status_code == 200
    return response.json()


def _attach_scheduled_purchase(
    session_factory: sessionmaker[Session],
    seeded: dict[str, object],
    checkout_id: str,
    order_id: str,
    intent_status: PurchaseIntentStatus,
    *,
    token_id: str = "token_scheduled_test",
    provider_payment_id: str | None = None,
    debit_submission_started: bool = True,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    scheduled_for = datetime.now(UTC) + timedelta(hours=1)
    suffix = intent_status.value
    with session_factory() as db, db.begin():
        checkout = db.get(Checkout, uuid.UUID(checkout_id))
        order = db.get(Order, uuid.UUID(order_id))
        assert checkout is not None and order is not None
        payment = db.scalar(select(Payment).where(Payment.order_id == order.id))
        assert payment is not None
        instrument = db.get(PaymentInstrument, seeded["payment_instrument_id"])
        assert instrument is not None
        instrument.provider = "razorpay_test"
        instrument.instrument_type = "com.razorpay.upi.autopay"
        instrument.provider_customer_id = "cust_scheduled_test"
        instrument.provider_token_reference = token_id
        instrument.instrument_metadata = {
            "token_status": "confirmed",
            "registration_status": "confirmed",
        }
        checkout.source = "scheduled_agent"
        payment.provider_payment_id = provider_payment_id
        intent = ScheduledPurchaseIntent(
            merchant_id=checkout.merchant_id,
            customer_id=checkout.customer_id,
            address_id=seeded["customer_address_id"],
            payment_instrument_id=seeded["payment_instrument_id"],
            location_id=checkout.location_id,
            customer_reference=f"scheduled-failure-{suffix}",
            idempotency_key=f"scheduled-failure-{suffix}",
            request_sha256=f"request-{suffix}",
            status=intent_status,
            constraints={"items": [], "first_run_at": scheduled_for.isoformat()},
            fulfillment_type=checkout.fulfillment_type,
            frequency="weekly",
            interval_count=1,
            max_occurrences=4,
            max_amount_minor=checkout.total_minor,
            max_total_minor=checkout.total_minor * 4,
            currency=checkout.currency,
            payment_token_reference=token_id,
            next_execution_at=scheduled_for,
            expires_at=scheduled_for + timedelta(days=30),
            last_failure_code=(
                "customer_controlled_schedule"
                if intent_status != PurchaseIntentStatus.ACTIVE
                else None
            ),
            last_failure_message=(
                "The customer controls this schedule."
                if intent_status != PurchaseIntentStatus.ACTIVE
                else None
            ),
        )
        db.add(intent)
        db.flush()
        run = ScheduledPurchaseRun(
            intent_id=intent.id,
            scheduled_for=scheduled_for,
            status=ScheduledRunStatus.PAYMENT_PENDING,
            idempotency_key=f"scheduled-run-failure-{suffix}",
            checkout_id=checkout.id,
            order_id=order.id,
            payment_id=payment.id,
            amount_minor=payment.amount_minor,
            currency=payment.currency,
            provider_order_id=payment.provider_order_id,
            provider_payment_id=provider_payment_id,
            merchant_checkout_jwt="merchant-checkout-jwt",
            closed_checkout_mandate="closed-checkout-mandate",
            closed_payment_mandate="closed-payment-mandate",
            evidence=(
                {"debit_submission_started_at": datetime.now(UTC).isoformat()}
                if debit_submission_started
                else {}
            ),
        )
        db.add(run)
        db.flush()
        reservations = [reservation for line in checkout.lines for reservation in line.reservations]
        assert len(reservations) == 1
        return intent.id, run.id, payment.id, reservations[0].id


def _webhook_headers(raw: bytes, event_id: str, secret: str) -> dict[str, str]:
    signature = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return {
        "Content-Type": "application/json",
        "X-Razorpay-Signature": signature,
        "X-Razorpay-Event-Id": event_id,
    }


def _payment_event(
    event: str,
    order_id: str,
    payment_id: str,
    amount: int,
    *,
    token_id: str | None = None,
) -> bytes:
    entity = {
        "id": payment_id,
        "entity": "payment",
        "amount": amount,
        "currency": "INR",
        "status": "captured" if event != "payment.failed" else "failed",
        "order_id": order_id,
        "captured": event != "payment.failed",
        "error_code": "BAD_REQUEST_ERROR" if event == "payment.failed" else None,
        "error_description": "The payment attempt failed" if event == "payment.failed" else None,
    }
    if token_id is not None:
        entity["token_id"] = token_id
    return json.dumps(
        {"entity": "event", "event": event, "payload": {"payment": {"entity": entity}}},
        separators=(",", ":"),
    ).encode()


def _scheduled_ap2_keys() -> AP2KeySet:
    return AP2KeySet(
        merchant=AP2Signer.generate("urn:test:merchant", "merchant-key-1"),
        trusted_surface=AP2Signer.generate("urn:test:agent-provider", "agent-provider-key-1"),
        credentials_provider=AP2Signer.generate("urn:test:cp", "cp-key-1"),
        payment_processor=AP2Signer.generate("urn:test:processor", "processor-key-1"),
        audience="agentbasket-commerce",
    )


@pytest.mark.anyio
async def test_checkout_requires_authentication(
    client: httpx.AsyncClient, seeded: dict[str, object]
) -> None:
    response = await client.post(
        "/api/v1/checkouts",
        headers={"Idempotency-Key": "unauthenticated-checkout"},
        json={
            "merchant_slug": "ember-and-leaf",
            "fulfillment_type": "local_delivery",
            "postal_code": "560038",
            "items": [],
        },
    )
    assert response.status_code == 401


@pytest.mark.anyio
async def test_cart_checkout_and_approval_are_exact_and_idempotent(
    client: httpx.AsyncClient, seeded: dict[str, object]
) -> None:
    checkout = await _checkout_from_cart(client, seeded)
    assert checkout["customer_id"] == str(seeded["customer_id"])
    assert checkout["total_minor"] == 119000
    mismatch = await client.post(
        f"/api/v1/checkouts/{checkout['id']}/approve",
        headers=_auth(seeded),
        json={"expected_total_minor": 118999, "quote_version": checkout["quote_version"]},
    )
    assert mismatch.status_code == 409
    assert mismatch.json()["error"]["code"] == "quote_changed"
    first = await _approve(client, seeded, checkout)
    second = await _approve(client, seeded, checkout)
    assert first["id"] == second["id"]
    assert first["approved_total_minor"] == checkout["total_minor"]
    assert len(first["evidence_sha256"]) == 64


@pytest.mark.anyio
async def test_payment_session_requires_approval(
    client: httpx.AsyncClient, seeded: dict[str, object]
) -> None:
    checkout = await _checkout_from_cart(client, seeded)
    gateway = FakeRazorpayGateway()
    app.dependency_overrides[get_razorpay_gateway] = lambda: gateway
    response = await client.post(
        f"/api/v1/checkouts/{checkout['id']}/payment-session",
        headers=_auth(seeded),
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "checkout_not_payment_ready"
    assert gateway.create_calls == 0


@pytest.mark.anyio
async def test_payment_session_retries_reuse_the_provider_order(
    client: httpx.AsyncClient, seeded: dict[str, object]
) -> None:
    checkout = await _checkout_from_cart(client, seeded)
    await _approve(client, seeded, checkout)
    gateway = FakeRazorpayGateway()
    first = await _session(client, seeded, checkout, gateway)
    second = await _session(client, seeded, checkout, gateway)
    assert first["provider_order_id"] == second["provider_order_id"]
    assert first["order_id"] == second["order_id"]
    assert gateway.create_calls == 1


@pytest.mark.anyio
async def test_invalid_checkout_signature_is_audited_and_not_paid(
    client: httpx.AsyncClient,
    seeded: dict[str, object],
    session_factory: sessionmaker[Session],
) -> None:
    checkout = await _checkout_from_cart(client, seeded)
    await _approve(client, seeded, checkout)
    gateway = FakeRazorpayGateway()
    session = await _session(client, seeded, checkout, gateway)
    response = await client.post(
        "/api/v1/payments/razorpay/verify",
        headers=_auth(seeded),
        json={
            "checkout_id": checkout["id"],
            "razorpay_order_id": session["provider_order_id"],
            "razorpay_payment_id": "pay_tampered",
            "razorpay_signature": "invalid",
        },
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_payment_signature"
    with session_factory() as db:
        order = db.get(Order, uuid.UUID(session["order_id"]))
        assert order is not None and order.status == OrderStatus.AWAITING_PAYMENT
        audit = db.scalar(
            select(AuditEvent).where(AuditEvent.event_type == "payment.signature_rejected")
        )
        assert audit is not None


@pytest.mark.anyio
async def test_callback_requires_captured_payment_and_paid_provider_order(
    client: httpx.AsyncClient, seeded: dict[str, object]
) -> None:
    checkout = await _checkout_from_cart(client, seeded)
    await _approve(client, seeded, checkout)
    gateway = FakeRazorpayGateway()
    session = await _session(client, seeded, checkout, gateway)
    provider_payment = gateway.set_payment(
        session["provider_order_id"],
        payment_id="pay_authorized",
        status="authorized",
        captured=False,
    )
    response = await client.post(
        "/api/v1/payments/razorpay/verify",
        headers=_auth(seeded),
        json={
            "checkout_id": checkout["id"],
            "razorpay_order_id": session["provider_order_id"],
            "razorpay_payment_id": provider_payment.id,
            "razorpay_signature": gateway.signature(
                session["provider_order_id"], provider_payment.id
            ),
        },
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "payment_not_captured"


@pytest.mark.anyio
async def test_valid_callback_completes_order_consumes_inventory_and_cart(
    client: httpx.AsyncClient,
    seeded: dict[str, object],
    session_factory: sessionmaker[Session],
) -> None:
    checkout = await _checkout_from_cart(client, seeded)
    await _approve(client, seeded, checkout)
    gateway = FakeRazorpayGateway()
    session = await _session(client, seeded, checkout, gateway)
    provider_payment = gateway.set_payment(session["provider_order_id"])
    response = await client.post(
        "/api/v1/payments/razorpay/verify",
        headers=_auth(seeded),
        json={
            "checkout_id": checkout["id"],
            "razorpay_order_id": session["provider_order_id"],
            "razorpay_payment_id": provider_payment.id,
            "razorpay_signature": gateway.signature(
                session["provider_order_id"], provider_payment.id
            ),
        },
    )
    assert response.status_code == 200
    receipt = response.json()
    assert receipt["status"] == "paid"
    assert receipt["checkout_status"] == "completed"
    assert receipt["payment"]["status"] == "captured"
    assert [event["event_type"] for event in receipt["timeline"]][-3:] == [
        "payment.captured",
        "order.paid",
        "checkout.completed",
    ]
    with session_factory() as db:
        inventory = db.get(InventoryItem, seeded["inventory_id"])
        assert inventory is not None
        assert inventory.on_hand_quantity == 1
        assert inventory.reserved_quantity == 0
        cart = db.scalar(select(Cart).where(Cart.user_id == seeded["customer_id"]))
        assert cart is not None and cart.items == []


@pytest.mark.anyio
async def test_webhook_rejects_invalid_signature_without_storing_event(
    client: httpx.AsyncClient,
    seeded: dict[str, object],
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(get_settings(), "razorpay_webhook_secret", "webhook-test-secret")
    raw = _payment_event("payment.captured", "order_unknown", "pay_unknown", 100)
    response = await client.post(
        "/api/v1/webhooks/razorpay",
        content=raw,
        headers={
            "Content-Type": "application/json",
            "X-Razorpay-Signature": "invalid",
            "X-Razorpay-Event-Id": "evt-invalid",
        },
    )
    assert response.status_code == 400
    with session_factory() as db:
        assert db.scalar(select(WebhookEvent)) is None


@pytest.mark.anyio
async def test_webhooks_handle_failed_then_captured_duplicates_and_late_failure(
    client: httpx.AsyncClient,
    seeded: dict[str, object],
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "webhook-test-secret"
    monkeypatch.setattr(get_settings(), "razorpay_webhook_secret", secret)
    checkout = await _checkout_from_cart(client, seeded)
    await _approve(client, seeded, checkout)
    gateway = FakeRazorpayGateway()
    session = await _session(client, seeded, checkout, gateway)

    failed = _payment_event(
        "payment.failed", session["provider_order_id"], "pay_failed_first", 119000
    )
    failed_response = await client.post(
        "/api/v1/webhooks/razorpay",
        content=failed,
        headers=_webhook_headers(failed, "evt-failed-first", secret),
    )
    assert failed_response.status_code == 200

    captured = _payment_event(
        "payment.captured", session["provider_order_id"], "pay_captured_late", 119000
    )
    captured_response = await client.post(
        "/api/v1/webhooks/razorpay",
        content=captured,
        headers=_webhook_headers(captured, "evt-captured", secret),
    )
    duplicate_response = await client.post(
        "/api/v1/webhooks/razorpay",
        content=captured,
        headers=_webhook_headers(captured, "evt-captured", secret),
    )
    assert captured_response.status_code == 200
    assert captured_response.json()["status"] == "processed"
    assert duplicate_response.json()["status"] == "duplicate"

    late_failed = _payment_event(
        "payment.failed", session["provider_order_id"], "pay_failed_late", 119000
    )
    late_response = await client.post(
        "/api/v1/webhooks/razorpay",
        content=late_failed,
        headers=_webhook_headers(late_failed, "evt-failed-late", secret),
    )
    assert late_response.status_code == 200
    with session_factory() as db:
        payment = db.scalar(select(Payment))
        order = db.get(Order, uuid.UUID(session["order_id"]))
        inventory = db.get(InventoryItem, seeded["inventory_id"])
        assert payment is not None and payment.status == PaymentStatus.CAPTURED
        assert payment.provider_payment_id == "pay_captured_late"
        assert order is not None and order.status == OrderStatus.PAID
        assert inventory is not None
        assert inventory.on_hand_quantity == 1
        assert inventory.reserved_quantity == 0


@pytest.mark.anyio
@pytest.mark.parametrize("provider_response_recorded", [True, False])
async def test_scheduled_capture_binds_exact_debit_including_early_webhook_race(
    provider_response_recorded: bool,
    client: httpx.AsyncClient,
    seeded: dict[str, object],
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "webhook-test-secret"
    monkeypatch.setattr(get_settings(), "razorpay_webhook_secret", secret)
    monkeypatch.setattr("app.services.ap2.get_ap2_key_set", _scheduled_ap2_keys)
    checkout = await _checkout_from_cart(client, seeded)
    await _approve(client, seeded, checkout)
    gateway = FakeRazorpayGateway()
    payment_session = await _session(client, seeded, checkout, gateway)
    provider_payment_id = (
        "pay_scheduled_known" if provider_response_recorded else "pay_scheduled_early_webhook"
    )
    intent_id, run_id, payment_id, _ = _attach_scheduled_purchase(
        session_factory,
        seeded,
        checkout["id"],
        payment_session["order_id"],
        PurchaseIntentStatus.ACTIVE,
        provider_payment_id=(provider_payment_id if provider_response_recorded else None),
    )
    captured = _payment_event(
        "payment.captured",
        payment_session["provider_order_id"],
        provider_payment_id,
        119000,
        token_id="token_scheduled_test",
    )

    response = await client.post(
        "/api/v1/webhooks/razorpay",
        content=captured,
        headers=_webhook_headers(
            captured,
            f"evt-scheduled-captured-{provider_response_recorded}",
            secret,
        ),
    )

    assert response.status_code == 200
    assert response.json()["status"] == "processed"
    with session_factory() as db:
        intent = db.get(ScheduledPurchaseIntent, intent_id)
        run = db.get(ScheduledPurchaseRun, run_id)
        payment = db.get(Payment, payment_id)
        receipts = list(
            db.scalars(select(Ap2Receipt).where(Ap2Receipt.checkout_id == run.checkout_id))
        )
        assert intent is not None and intent.successful_occurrences == 1
        assert run is not None and run.status == ScheduledRunStatus.SUCCEEDED
        assert run.provider_payment_id == provider_payment_id
        assert payment is not None and payment.status == PaymentStatus.CAPTURED
        assert payment.provider_payment_id == provider_payment_id
        assert len(receipts) == 2


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("stored_payment_id", "webhook_payment_id", "webhook_token_id", "expected_code"),
    [
        (
            "pay_scheduled_expected",
            "pay_scheduled_unexpected",
            "token_scheduled_test",
            "scheduled_payment_binding_mismatch",
        ),
        (
            "pay_scheduled_expected",
            "pay_scheduled_expected",
            "token_other_mandate",
            "scheduled_payment_token_mismatch",
        ),
    ],
)
async def test_scheduled_capture_rejects_payment_or_token_mismatch_and_keeps_evidence(
    stored_payment_id: str,
    webhook_payment_id: str,
    webhook_token_id: str,
    expected_code: str,
    client: httpx.AsyncClient,
    seeded: dict[str, object],
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "webhook-test-secret"
    monkeypatch.setattr(get_settings(), "razorpay_webhook_secret", secret)
    checkout = await _checkout_from_cart(client, seeded)
    await _approve(client, seeded, checkout)
    gateway = FakeRazorpayGateway()
    payment_session = await _session(client, seeded, checkout, gateway)
    _, run_id, payment_id, _ = _attach_scheduled_purchase(
        session_factory,
        seeded,
        checkout["id"],
        payment_session["order_id"],
        PurchaseIntentStatus.ACTIVE,
        provider_payment_id=stored_payment_id,
    )
    captured = _payment_event(
        "payment.captured",
        payment_session["provider_order_id"],
        webhook_payment_id,
        119000,
        token_id=webhook_token_id,
    )
    event_id = f"evt-{expected_code}"

    response = await client.post(
        "/api/v1/webhooks/razorpay",
        content=captured,
        headers=_webhook_headers(captured, event_id, secret),
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == expected_code
    with session_factory() as db:
        run = db.get(ScheduledPurchaseRun, run_id)
        payment = db.get(Payment, payment_id)
        webhook = db.scalar(select(WebhookEvent).where(WebhookEvent.event_id == event_id))
        assert run is not None and run.status == ScheduledRunStatus.PAYMENT_PENDING
        assert run.provider_payment_id == stored_payment_id
        assert payment is not None and payment.status == PaymentStatus.CREATED
        assert payment.provider_payment_id == stored_payment_id
        assert webhook is not None and not webhook.processed
        assert expected_code in (webhook.failure_message or "")


@pytest.mark.anyio
async def test_scheduled_capture_without_debit_marker_is_rejected_and_audited(
    client: httpx.AsyncClient,
    seeded: dict[str, object],
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "webhook-test-secret"
    monkeypatch.setattr(get_settings(), "razorpay_webhook_secret", secret)
    checkout = await _checkout_from_cart(client, seeded)
    await _approve(client, seeded, checkout)
    gateway = FakeRazorpayGateway()
    payment_session = await _session(client, seeded, checkout, gateway)
    _, run_id, payment_id, _ = _attach_scheduled_purchase(
        session_factory,
        seeded,
        checkout["id"],
        payment_session["order_id"],
        PurchaseIntentStatus.ACTIVE,
        provider_payment_id="pay_without_submission_marker",
        debit_submission_started=False,
    )
    captured = _payment_event(
        "payment.captured",
        payment_session["provider_order_id"],
        "pay_without_submission_marker",
        119000,
        token_id="token_scheduled_test",
    )
    event_id = "evt-scheduled-missing-debit-marker"

    response = await client.post(
        "/api/v1/webhooks/razorpay",
        content=captured,
        headers=_webhook_headers(captured, event_id, secret),
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "scheduled_debit_submission_missing"
    with session_factory() as db:
        run = db.get(ScheduledPurchaseRun, run_id)
        payment = db.get(Payment, payment_id)
        webhook = db.scalar(select(WebhookEvent).where(WebhookEvent.event_id == event_id))
        assert run is not None and run.status == ScheduledRunStatus.PAYMENT_PENDING
        assert payment is not None and payment.status == PaymentStatus.CREATED
        assert webhook is not None and not webhook.processed
        assert "scheduled_debit_submission_missing" in (webhook.failure_message or "")


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("intent_status", "expected_status"),
    [
        (PurchaseIntentStatus.ACTIVE, PurchaseIntentStatus.NEEDS_ATTENTION),
        (PurchaseIntentStatus.PAUSED, PurchaseIntentStatus.PAUSED),
        (PurchaseIntentStatus.REVOKED, PurchaseIntentStatus.REVOKED),
    ],
)
async def test_scheduled_payment_failure_releases_stock_and_preserves_control_state(
    intent_status: PurchaseIntentStatus,
    expected_status: PurchaseIntentStatus,
    client: httpx.AsyncClient,
    seeded: dict[str, object],
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "webhook-test-secret"
    monkeypatch.setattr(get_settings(), "razorpay_webhook_secret", secret)
    checkout = await _checkout_from_cart(client, seeded)
    await _approve(client, seeded, checkout)
    gateway = FakeRazorpayGateway()
    session = await _session(client, seeded, checkout, gateway)
    intent_id, run_id, payment_id, reservation_id = _attach_scheduled_purchase(
        session_factory,
        seeded,
        checkout["id"],
        session["order_id"],
        intent_status,
    )
    failed = _payment_event(
        "payment.failed",
        session["provider_order_id"],
        f"pay_scheduled_failed_{intent_status.value}",
        119000,
    )

    response = await client.post(
        "/api/v1/webhooks/razorpay",
        content=failed,
        headers=_webhook_headers(failed, f"evt-scheduled-failed-{intent_status.value}", secret),
    )

    assert response.status_code == 200
    assert response.json()["status"] == "processed"
    late_capture = _payment_event(
        "payment.captured",
        session["provider_order_id"],
        f"pay_scheduled_late_capture_{intent_status.value}",
        119000,
    )
    late_event_id = f"evt-scheduled-late-capture-{intent_status.value}"
    late_response = await client.post(
        "/api/v1/webhooks/razorpay",
        content=late_capture,
        headers=_webhook_headers(
            late_capture,
            late_event_id,
            secret,
        ),
    )
    assert late_response.status_code == 409
    assert late_response.json()["error"]["code"] == "scheduled_payment_terminal"
    late_retry = await client.post(
        "/api/v1/webhooks/razorpay",
        content=late_capture,
        headers=_webhook_headers(late_capture, late_event_id, secret),
    )
    assert late_retry.status_code == 409
    assert late_retry.json()["error"]["code"] == "scheduled_payment_terminal"
    with session_factory() as db:
        intent = db.get(ScheduledPurchaseIntent, intent_id)
        run = db.get(ScheduledPurchaseRun, run_id)
        checkout_row = db.get(Checkout, uuid.UUID(checkout["id"]))
        order = db.get(Order, uuid.UUID(session["order_id"]))
        payment = db.get(Payment, payment_id)
        reservation = db.get(InventoryReservation, reservation_id)
        inventory = db.get(InventoryItem, seeded["inventory_id"])
        failure_audit = db.scalar(
            select(AuditEvent).where(
                AuditEvent.event_type == "payment.failed",
                AuditEvent.aggregate_id == str(payment_id),
            )
        )
        unreconciled_event = db.scalar(
            select(WebhookEvent).where(WebhookEvent.event_id == late_event_id)
        )
        unreconciled_count = len(
            list(db.scalars(select(WebhookEvent).where(WebhookEvent.event_id == late_event_id)))
        )
        assert intent is not None and intent.status == expected_status
        if intent_status == PurchaseIntentStatus.ACTIVE:
            assert intent.last_failure_code == "BAD_REQUEST_ERROR"
        else:
            assert intent.last_failure_code == "customer_controlled_schedule"
        assert run is not None and run.status == ScheduledRunStatus.REQUIRES_HUMAN_ACTION
        assert checkout_row is not None and checkout_row.status == CheckoutStatus.CANCELED
        assert order is not None and order.status == OrderStatus.CANCELED
        assert payment is not None and payment.status == PaymentStatus.FAILED
        assert reservation is not None and reservation.status == ReservationStatus.RELEASED
        assert inventory is not None
        assert inventory.on_hand_quantity == 2
        assert inventory.reserved_quantity == 0
        assert failure_audit is not None
        assert failure_audit.payload["released_reservations"] == 1
        assert failure_audit.payload["checkout_status"] == CheckoutStatus.CANCELED.value
        assert failure_audit.payload["order_status"] == OrderStatus.CANCELED.value
        assert unreconciled_event is not None and not unreconciled_event.processed
        assert "scheduled_payment_terminal" in unreconciled_event.failure_message
        assert unreconciled_count == 1
