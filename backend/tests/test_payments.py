import hashlib
import hmac
import json
import uuid
from dataclasses import replace

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings
from app.db.models import AuditEvent, Cart, InventoryItem, Order, Payment, WebhookEvent
from app.domain.enums import OrderStatus, PaymentStatus
from app.main import app
from app.payments.razorpay import (
    RazorpayOrder,
    RazorpayPayment,
    get_razorpay_gateway,
)


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


def _webhook_headers(raw: bytes, event_id: str, secret: str) -> dict[str, str]:
    signature = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return {
        "Content-Type": "application/json",
        "X-Razorpay-Signature": signature,
        "X-Razorpay-Event-Id": event_id,
    }


def _payment_event(event: str, order_id: str, payment_id: str, amount: int) -> bytes:
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
    return json.dumps(
        {"entity": "event", "event": event, "payload": {"payment": {"entity": entity}}},
        separators=(",", ":"),
    ).encode()


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
