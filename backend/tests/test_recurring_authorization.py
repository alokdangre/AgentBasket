from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.db.models import PaymentInstrument, ScheduledPurchaseIntent
from app.domain.enums import FulfillmentType, PurchaseIntentStatus
from app.main import app
from app.payments.razorpay import (
    RazorpayCustomer,
    RazorpayOrder,
    RazorpayPayment,
    get_razorpay_recurring_gateway,
)

WEBHOOK_SECRET = "recurring-authorization-webhook-secret"


class FakeRecurringGateway:
    key_id = "rzp_test_recurring"

    def __init__(self) -> None:
        self.customer_calls = 0
        self.mandate_order_calls = 0
        self.orders_by_receipt: dict[str, RazorpayOrder] = {}

    def create_customer(
        self,
        *,
        name: str,
        email: str,
        contact: str,
        notes: dict[str, str],
    ) -> RazorpayCustomer:
        assert name == "Aarav Mehta"
        assert email == "customer@emberandleaf.test"
        assert contact == "+919876543210"
        assert notes["agentbasket_customer_id"]
        self.customer_calls += 1
        return RazorpayCustomer(
            id="cust_recurring_001",
            name=name,
            email=email,
            contact=contact,
        )

    def create_mandate_order(
        self,
        *,
        amount: int,
        currency: str,
        receipt: str,
        customer_id: str,
        max_amount: int,
        frequency: str,
        expire_at: int,
        notes: dict[str, str],
    ) -> RazorpayOrder:
        assert amount == 100
        assert currency == "INR"
        assert customer_id == "cust_recurring_001"
        assert max_amount == 30_000
        assert frequency == "as_presented"
        assert expire_at > int(datetime.now(UTC).timestamp())
        assert notes["scheduled_purchase_id"]
        self.mandate_order_calls += 1
        order = RazorpayOrder(
            id="order_mandate_001",
            amount=amount,
            currency=currency,
            receipt=receipt,
            status="created",
        )
        self.orders_by_receipt[receipt] = order
        return order

    def find_order_by_receipt(self, receipt: str) -> RazorpayOrder | None:
        return self.orders_by_receipt.get(receipt)

    def verify_checkout_signature(self, *, order_id: str, payment_id: str, signature: str) -> bool:
        return (
            order_id == "order_mandate_001"
            and payment_id == "pay_mandate_001"
            and signature == "s" * 64
        )

    def fetch_payment(self, payment_id: str) -> RazorpayPayment:
        assert payment_id == "pay_mandate_001"
        return RazorpayPayment(
            id=payment_id,
            order_id="order_mandate_001",
            amount=100,
            currency="INR",
            status="authorized",
            captured=False,
            token_id="token_upi_001",
        )


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        app_env="test",
        razorpay_recurring_enabled=True,
        razorpay_recurring_notification_lead_hours=25,
        razorpay_webhook_secret=WEBHOOK_SECRET,
    )


def _headers(seeded: dict[str, object]) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {seeded['customer_token']}",
        "Idempotency-Key": "recurring-registration-001",
    }


def _pending_schedule(
    session_factory: sessionmaker[Session], seeded: dict[str, object]
) -> tuple[object, object]:
    first_run = datetime.now(UTC) + timedelta(days=3)
    with session_factory() as db, db.begin():
        instrument = PaymentInstrument(
            user_id=seeded["customer_id"],
            provider="razorpay_test",
            instrument_type="com.razorpay.upi.autopay",
            alias="Razorpay UPI Autopay",
            status="active",
            is_default=False,
            instrument_metadata={
                "mode": "test",
                "requires_provider_checkout": True,
                "token_status": "not_started",
            },
        )
        db.add(instrument)
        db.flush()
        intent = ScheduledPurchaseIntent(
            merchant_id=seeded["merchant_id"],
            customer_id=seeded["customer_id"],
            customer_reference=str(seeded["customer_id"]),
            idempotency_key="recurring-schedule-001",
            request_sha256=hashlib.sha256(b"recurring-schedule-001").hexdigest(),
            address_id=seeded["customer_address_id"],
            payment_instrument_id=instrument.id,
            location_id=seeded["location_id"],
            status=PurchaseIntentStatus.PENDING_PROVIDER_AUTHORIZATION,
            constraints={"items": []},
            fulfillment_type=FulfillmentType.LOCAL_DELIVERY,
            frequency="weekly",
            interval_count=1,
            timezone="Asia/Kolkata",
            max_occurrences=3,
            max_amount_minor=30_000,
            max_total_minor=90_000,
            currency="INR",
            next_execution_at=first_run,
            expires_at=first_run + timedelta(days=30),
            open_checkout_mandate="signed-open-checkout",
            open_payment_mandate="signed-open-payment",
            open_checkout_hash="open-checkout-hash",
            authorized_at=datetime.now(UTC),
        )
        db.add(intent)
        db.flush()
        return intent.id, instrument.id


def _second_pending_schedule(
    session_factory: sessionmaker[Session],
    seeded: dict[str, object],
    instrument_id: uuid.UUID,
) -> uuid.UUID:
    first_run = datetime.now(UTC) + timedelta(days=4)
    with session_factory() as db, db.begin():
        intent = ScheduledPurchaseIntent(
            merchant_id=seeded["merchant_id"],
            customer_id=seeded["customer_id"],
            customer_reference=str(seeded["customer_id"]),
            idempotency_key="recurring-schedule-002",
            request_sha256=hashlib.sha256(b"recurring-schedule-002").hexdigest(),
            address_id=seeded["customer_address_id"],
            payment_instrument_id=instrument_id,
            location_id=seeded["location_id"],
            status=PurchaseIntentStatus.PENDING_PROVIDER_AUTHORIZATION,
            constraints={"items": []},
            fulfillment_type=FulfillmentType.LOCAL_DELIVERY,
            frequency="weekly",
            interval_count=1,
            timezone="Asia/Kolkata",
            max_occurrences=2,
            max_amount_minor=30_000,
            max_total_minor=60_000,
            currency="INR",
            next_execution_at=first_run,
            expires_at=first_run + timedelta(days=20),
            open_checkout_mandate="signed-open-checkout-2",
            open_payment_mandate="signed-open-payment-2",
            open_checkout_hash="open-checkout-hash-2",
            authorized_at=datetime.now(UTC),
        )
        db.add(intent)
        db.flush()
        return intent.id


def _token_event(status: str = "confirmed", *, created_at: int | None = None) -> bytes:
    payload: dict[str, object] = {
        "entity": "event",
        "event": f"token.{status}",
        "payload": {
            "token": {
                "entity": {
                    "id": "token_upi_001",
                    "customer_id": "cust_recurring_001",
                    "status": status,
                }
            }
        },
    }
    if created_at is not None:
        payload["created_at"] = created_at
    return json.dumps(payload, separators=(",", ":")).encode()


async def _post_token_event(
    client: httpx.AsyncClient,
    *,
    status: str,
    event_id: str,
    created_at: int,
) -> httpx.Response:
    raw = _token_event(status, created_at=created_at)
    signature = hmac.new(WEBHOOK_SECRET.encode(), raw, hashlib.sha256).hexdigest()
    return await client.post(
        "/api/v1/webhooks/razorpay",
        headers={
            "Content-Type": "application/json",
            "X-Razorpay-Signature": signature,
            "X-Razorpay-Event-Id": event_id,
        },
        content=raw,
    )


@pytest.mark.anyio
async def test_registration_is_idempotent_and_webhook_is_the_activation_gate(
    client: httpx.AsyncClient,
    seeded: dict[str, object],
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings()
    gateway = FakeRecurringGateway()
    intent_id, instrument_id = _pending_schedule(session_factory, seeded)
    monkeypatch.setattr("app.services.recurring_authorization.get_settings", lambda: settings)
    monkeypatch.setattr("app.services.payment.get_settings", lambda: settings)
    monkeypatch.setattr("app.services.credentials_provider.get_settings", lambda: settings)
    monkeypatch.setattr("app.services.scheduled_purchase.get_settings", lambda: settings)
    app.dependency_overrides[get_razorpay_recurring_gateway] = lambda: gateway

    session_url = f"/api/v1/credential-provider/razorpay-upi-autopay/{intent_id}/session"
    first = await client.post(session_url, headers=_headers(seeded))
    repeated = await client.post(session_url, headers=_headers(seeded))

    assert first.status_code == 201
    assert repeated.status_code == 201
    assert first.json()["provider_order_id"] == "order_mandate_001"
    assert first.json()["amount_minor"] == 100
    assert first.json()["max_amount_minor"] == 30_000
    assert gateway.customer_calls == 1
    assert gateway.mandate_order_calls == 1

    second_intent_id = _second_pending_schedule(session_factory, seeded, instrument_id)
    conflict = await client.post(
        (f"/api/v1/credential-provider/razorpay-upi-autopay/{second_intent_id}/session"),
        headers={
            "Authorization": f"Bearer {seeded['customer_token']}",
            "Idempotency-Key": "recurring-registration-002",
        },
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "recurring_registration_in_progress"
    assert gateway.customer_calls == 1
    assert gateway.mandate_order_calls == 1

    verified = await client.post(
        f"/api/v1/credential-provider/razorpay-upi-autopay/{intent_id}/verify",
        headers={"Authorization": f"Bearer {seeded['customer_token']}"},
        json={
            "razorpay_order_id": "order_mandate_001",
            "razorpay_payment_id": "pay_mandate_001",
            "razorpay_signature": "s" * 64,
        },
    )
    assert verified.status_code == 200
    assert verified.json()["status"] == "token_confirmation_pending"

    with session_factory() as db:
        pending = db.get(ScheduledPurchaseIntent, intent_id)
        instrument = db.get(PaymentInstrument, instrument_id)
        assert pending is not None
        assert pending.status == PurchaseIntentStatus.PENDING_PROVIDER_AUTHORIZATION
        assert pending.next_run_at is None
        assert instrument is not None
        assert instrument.instrument_metadata["token_status"] == "pending"

    raw = _token_event()
    signature = hmac.new(WEBHOOK_SECRET.encode(), raw, hashlib.sha256).hexdigest()
    webhook = await client.post(
        "/api/v1/webhooks/razorpay",
        headers={
            "Content-Type": "application/json",
            "X-Razorpay-Signature": signature,
            "X-Razorpay-Event-Id": "token-confirmed-001",
        },
        content=raw,
    )
    assert webhook.status_code == 200
    assert webhook.json()["status"] == "processed"

    with session_factory() as db:
        active = db.get(ScheduledPurchaseIntent, intent_id)
        instrument = db.get(PaymentInstrument, instrument_id)
        assert active is not None and active.status == PurchaseIntentStatus.ACTIVE
        assert active.payment_token_reference == "token_upi_001"
        assert active.next_run_at is not None
        assert instrument is not None
        assert instrument.provider_token_reference == "token_upi_001"
        assert instrument.instrument_metadata["token_status"] == "confirmed"


@pytest.mark.anyio
async def test_checkout_verification_never_downgrades_an_early_confirmed_webhook(
    client: httpx.AsyncClient,
    seeded: dict[str, object],
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings()
    gateway = FakeRecurringGateway()
    intent_id, instrument_id = _pending_schedule(session_factory, seeded)
    monkeypatch.setattr("app.services.recurring_authorization.get_settings", lambda: settings)
    monkeypatch.setattr("app.services.payment.get_settings", lambda: settings)
    monkeypatch.setattr("app.services.credentials_provider.get_settings", lambda: settings)
    monkeypatch.setattr("app.services.scheduled_purchase.get_settings", lambda: settings)
    app.dependency_overrides[get_razorpay_recurring_gateway] = lambda: gateway

    session = await client.post(
        f"/api/v1/credential-provider/razorpay-upi-autopay/{intent_id}/session",
        headers=_headers(seeded),
    )
    assert session.status_code == 201

    raw = _token_event()
    signature = hmac.new(WEBHOOK_SECRET.encode(), raw, hashlib.sha256).hexdigest()
    confirmed = await client.post(
        "/api/v1/webhooks/razorpay",
        headers={
            "Content-Type": "application/json",
            "X-Razorpay-Signature": signature,
            "X-Razorpay-Event-Id": "token-confirmed-early-001",
        },
        content=raw,
    )
    assert confirmed.status_code == 200

    verified = await client.post(
        f"/api/v1/credential-provider/razorpay-upi-autopay/{intent_id}/verify",
        headers={"Authorization": f"Bearer {seeded['customer_token']}"},
        json={
            "razorpay_order_id": "order_mandate_001",
            "razorpay_payment_id": "pay_mandate_001",
            "razorpay_signature": "s" * 64,
        },
    )
    assert verified.status_code == 200
    assert verified.json()["status"] == "confirmed"
    assert verified.json()["token_confirmation_pending"] is False

    with session_factory() as db:
        instrument = db.get(PaymentInstrument, instrument_id)
        assert instrument is not None
        assert instrument.provider_token_reference == "token_upi_001"
        assert instrument.instrument_metadata["token_status"] == "confirmed"


@pytest.mark.anyio
async def test_stale_or_duplicate_token_event_cannot_downgrade_confirmation(
    client: httpx.AsyncClient,
    seeded: dict[str, object],
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings()
    gateway = FakeRecurringGateway()
    intent_id, instrument_id = _pending_schedule(session_factory, seeded)
    monkeypatch.setattr("app.services.recurring_authorization.get_settings", lambda: settings)
    monkeypatch.setattr("app.services.payment.get_settings", lambda: settings)
    monkeypatch.setattr("app.services.credentials_provider.get_settings", lambda: settings)
    monkeypatch.setattr("app.services.scheduled_purchase.get_settings", lambda: settings)
    app.dependency_overrides[get_razorpay_recurring_gateway] = lambda: gateway

    registration = await client.post(
        f"/api/v1/credential-provider/razorpay-upi-autopay/{intent_id}/session",
        headers=_headers(seeded),
    )
    assert registration.status_code == 201

    event_time = int(datetime.now(UTC).timestamp())
    confirmed = await _post_token_event(
        client,
        status="confirmed",
        event_id="token-confirmed-monotonic-001",
        created_at=event_time + 20,
    )
    assert confirmed.status_code == 200
    assert confirmed.json()["status"] == "processed"

    duplicate = await _post_token_event(
        client,
        status="confirmed",
        event_id="token-confirmed-monotonic-duplicate-001",
        created_at=event_time + 21,
    )
    stale_pause = await _post_token_event(
        client,
        status="paused",
        event_id="token-paused-stale-001",
        created_at=event_time + 10,
    )
    assert duplicate.status_code == 200
    assert duplicate.json()["status"] == "ignored"
    assert stale_pause.status_code == 200
    assert stale_pause.json()["status"] == "ignored"

    with session_factory() as db:
        intent = db.get(ScheduledPurchaseIntent, intent_id)
        instrument = db.get(PaymentInstrument, instrument_id)
        assert intent is not None
        assert intent.status == PurchaseIntentStatus.ACTIVE
        assert intent.next_run_at is not None
        assert instrument is not None
        assert instrument.instrument_metadata["token_status"] == "confirmed"


@pytest.mark.anyio
@pytest.mark.parametrize("restrictive_status", ["paused", "cancelled", "rejected"])
async def test_restrictive_token_state_cannot_be_reactivated_by_confirmation(
    restrictive_status: str,
    client: httpx.AsyncClient,
    seeded: dict[str, object],
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings()
    gateway = FakeRecurringGateway()
    intent_id, instrument_id = _pending_schedule(session_factory, seeded)
    monkeypatch.setattr("app.services.recurring_authorization.get_settings", lambda: settings)
    monkeypatch.setattr("app.services.payment.get_settings", lambda: settings)
    monkeypatch.setattr("app.services.credentials_provider.get_settings", lambda: settings)
    monkeypatch.setattr("app.services.scheduled_purchase.get_settings", lambda: settings)
    app.dependency_overrides[get_razorpay_recurring_gateway] = lambda: gateway

    registration = await client.post(
        f"/api/v1/credential-provider/razorpay-upi-autopay/{intent_id}/session",
        headers=_headers(seeded),
    )
    assert registration.status_code == 201

    event_time = int(datetime.now(UTC).timestamp())
    confirmed = await _post_token_event(
        client,
        status="confirmed",
        event_id=f"token-confirmed-before-{restrictive_status}",
        created_at=event_time,
    )
    restricted = await _post_token_event(
        client,
        status=restrictive_status,
        event_id=f"token-{restrictive_status}-001",
        created_at=event_time + 10,
    )
    reopened = await _post_token_event(
        client,
        status="confirmed",
        event_id=f"token-confirmed-after-{restrictive_status}",
        created_at=event_time + 20,
    )
    assert confirmed.status_code == 200
    assert confirmed.json()["status"] == "processed"
    assert restricted.status_code == 200
    assert restricted.json()["status"] == "processed"
    assert reopened.status_code == 200
    assert reopened.json()["status"] == "ignored"

    with session_factory() as db:
        intent = db.get(ScheduledPurchaseIntent, intent_id)
        instrument = db.get(PaymentInstrument, instrument_id)
        assert intent is not None
        assert intent.status == PurchaseIntentStatus.PENDING_PROVIDER_AUTHORIZATION
        assert intent.next_run_at is None
        assert instrument is not None
        assert instrument.instrument_metadata["token_status"] == restrictive_status
