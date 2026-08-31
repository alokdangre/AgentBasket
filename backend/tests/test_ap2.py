import hashlib
import hmac
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import (
    Ap2ConsentChallenge,
    Ap2Mandate,
    Ap2Receipt,
    Checkout,
    CheckoutApproval,
    UserAccount,
)
from app.main import app
from app.payments.razorpay import RazorpayOrder, RazorpayPayment, get_razorpay_gateway
from app.protocols.ap2.crypto import AP2KeySet, AP2Signer, digest_b64url, get_ap2_key_set
from app.schemas.checkout import CheckoutFromCartCreate
from app.services.checkout import CheckoutService


class FakeRazorpayGateway:
    key_id = "rzp_test_ap2"
    secret = "ap2-checkout-secret"

    def __init__(self) -> None:
        self.orders_by_receipt: dict[str, RazorpayOrder] = {}
        self.orders_by_id: dict[str, RazorpayOrder] = {}
        self.payments: dict[str, RazorpayPayment] = {}
        self.create_calls = 0

    def create_order(
        self, *, amount: int, currency: str, receipt: str, notes: dict[str, str]
    ) -> RazorpayOrder:
        del notes
        self.create_calls += 1
        order = RazorpayOrder(
            id=f"order_ap2_{self.create_calls}",
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
        return hmac.compare_digest(self.signature(order_id, payment_id), signature)

    def capture(self, order_id: str) -> RazorpayPayment:
        order = self.orders_by_id[order_id]
        payment = RazorpayPayment(
            id="pay_ap2_captured",
            order_id=order_id,
            amount=order.amount,
            currency=order.currency,
            status="captured",
            captured=True,
            network_confirmation_id="rrn_ap2_test_001",
        )
        self.payments[payment.id] = payment
        paid = replace(order, status="paid", amount_paid=order.amount)
        self.orders_by_id[order_id] = paid
        self.orders_by_receipt[order.receipt] = paid
        return payment

    def signature(self, order_id: str, payment_id: str) -> str:
        return hmac.new(
            self.secret.encode(), f"{order_id}|{payment_id}".encode(), hashlib.sha256
        ).hexdigest()


def _auth(seeded: dict[str, object]) -> dict[str, str]:
    return {"Authorization": f"Bearer {seeded['customer_token']}"}


def _keys() -> AP2KeySet:
    return AP2KeySet(
        merchant=AP2Signer.generate("urn:test:merchant", "merchant-key-1"),
        trusted_surface=AP2Signer.generate("urn:test:trusted-surface", "surface-key-1"),
        payment_processor=AP2Signer.generate("urn:test:processor", "processor-key-1"),
        audience="agentbasket-commerce",
    )


async def _agent_checkout(
    client: httpx.AsyncClient,
    seeded: dict[str, object],
    session_factory: sessionmaker[Session],
) -> dict[str, Any]:
    added = await client.post(
        "/api/v1/cart/items",
        headers=_auth(seeded),
        json={
            "variant_id": str(seeded["bean_variant_id"]),
            "quantity": 1,
            "modifier_option_ids": [str(seeded["whole_id"])],
        },
    )
    assert added.status_code == 201
    with session_factory() as db:
        customer = db.get(UserAccount, seeded["customer_id"])
        assert customer is not None
        db.rollback()
        checkout = CheckoutService(db).create_from_cart(
            CheckoutFromCartCreate(
                fulfillment_type="local_delivery",
                address_id=seeded["customer_address_id"],
            ),
            f"agent-ap2-{uuid.uuid4()}",
            customer,
            source="agent",
        )
        return checkout.model_dump(mode="json")


async def _challenge(
    client: httpx.AsyncClient,
    seeded: dict[str, object],
    checkout: dict[str, Any],
) -> dict[str, Any]:
    response = await client.post(
        f"/api/v1/checkouts/{checkout['id']}/ap2/challenge",
        headers={**_auth(seeded), "Idempotency-Key": "ap2-challenge-request-001"},
    )
    assert response.status_code == 201
    return response.json()


def _approval_body(checkout: dict[str, Any], challenge: dict[str, Any]) -> dict[str, Any]:
    return {
        "challenge_id": challenge["id"],
        "nonce": challenge["nonce"],
        "checkout_hash": challenge["checkout_hash"],
        "display_sha256": challenge["display_sha256"],
        "expected_total_minor": checkout["total_minor"],
        "currency": checkout["currency"],
        "quote_version": checkout["quote_version"],
    }


@pytest.mark.anyio
async def test_agent_checkout_requires_verified_ap2_pair_before_payment(
    client: httpx.AsyncClient,
    seeded: dict[str, object],
    session_factory: sessionmaker[Session],
) -> None:
    checkout = await _agent_checkout(client, seeded, session_factory)
    legacy = await client.post(
        f"/api/v1/checkouts/{checkout['id']}/approve",
        headers=_auth(seeded),
        json={
            "expected_total_minor": checkout["total_minor"],
            "quote_version": checkout["quote_version"],
        },
    )
    assert legacy.status_code == 200
    gateway = FakeRazorpayGateway()
    app.dependency_overrides[get_razorpay_gateway] = lambda: gateway
    payment = await client.post(
        f"/api/v1/checkouts/{checkout['id']}/payment-session", headers=_auth(seeded)
    )
    assert payment.status_code == 409
    assert payment.json()["error"]["code"] == "ap2_approval_required"
    assert gateway.create_calls == 0


@pytest.mark.anyio
async def test_ap2_rejects_changed_terms_and_consumes_challenge(
    client: httpx.AsyncClient,
    seeded: dict[str, object],
    session_factory: sessionmaker[Session],
) -> None:
    keys = _keys()
    app.dependency_overrides[get_ap2_key_set] = lambda: keys
    checkout = await _agent_checkout(client, seeded, session_factory)
    challenge = await _challenge(client, seeded, checkout)
    assert challenge["checkout_mandate"]["vct"] == "mandate.checkout.1"
    assert challenge["payment_mandate"]["vct"] == "mandate.payment.1"
    assert challenge["checkout_hash"] == digest_b64url(
        challenge["checkout_mandate"]["checkout_jwt"]
    )
    body = _approval_body(checkout, challenge)
    body["expected_total_minor"] -= 1
    rejected = await client.post(
        f"/api/v1/checkouts/{checkout['id']}/ap2/approve",
        headers={**_auth(seeded), "Idempotency-Key": "ap2-approve-tampered-001"},
        json=body,
    )
    assert rejected.status_code == 409
    assert rejected.json()["error"]["code"] == "ap2_terms_mismatch"
    with session_factory() as db:
        stored = db.get(Ap2ConsentChallenge, uuid.UUID(challenge["id"]))
        assert stored is not None and stored.status == "rejected"
        assert db.scalar(select(func.count()).select_from(CheckoutApproval)) == 0
        assert db.scalar(select(func.count()).select_from(Ap2Mandate)) == 0


@pytest.mark.anyio
async def test_ap2_approval_payment_and_signed_receipts_are_end_to_end(
    client: httpx.AsyncClient,
    seeded: dict[str, object],
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    keys = _keys()
    app.dependency_overrides[get_ap2_key_set] = lambda: keys
    monkeypatch.setattr("app.services.ap2.get_ap2_key_set", lambda: keys)
    checkout = await _agent_checkout(client, seeded, session_factory)
    challenge = await _challenge(client, seeded, checkout)
    headers = {**_auth(seeded), "Idempotency-Key": "ap2-approve-success-001"}
    approved = await client.post(
        f"/api/v1/checkouts/{checkout['id']}/ap2/approve",
        headers=headers,
        json=_approval_body(checkout, challenge),
    )
    repeated = await client.post(
        f"/api/v1/checkouts/{checkout['id']}/ap2/approve",
        headers=headers,
        json=_approval_body(checkout, challenge),
    )
    assert approved.status_code == 200
    assert repeated.status_code == 200
    assert repeated.json()["approval"]["id"] == approved.json()["approval"]["id"]
    assert {item["verification_status"] for item in approved.json()["mandates"]} == {"verified"}

    gateway = FakeRazorpayGateway()
    app.dependency_overrides[get_razorpay_gateway] = lambda: gateway
    session_response = await client.post(
        f"/api/v1/checkouts/{checkout['id']}/payment-session", headers=_auth(seeded)
    )
    assert session_response.status_code == 200
    payment_session = session_response.json()
    provider_payment = gateway.capture(payment_session["provider_order_id"])
    verified = await client.post(
        "/api/v1/payments/razorpay/verify",
        headers=_auth(seeded),
        json={
            "checkout_id": checkout["id"],
            "razorpay_order_id": payment_session["provider_order_id"],
            "razorpay_payment_id": provider_payment.id,
            "razorpay_signature": gateway.signature(
                payment_session["provider_order_id"], provider_payment.id
            ),
        },
    )
    assert verified.status_code == 200
    assert verified.json()["status"] == "paid"

    evidence_response = await client.get(
        f"/api/v1/checkouts/{checkout['id']}/ap2/evidence", headers=_auth(seeded)
    )
    assert evidence_response.status_code == 200
    evidence = evidence_response.json()
    assert {item["receipt_type"] for item in evidence["receipts"]} == {
        "checkout",
        "payment",
    }
    assert {item["order_id"] for item in evidence["receipts"]} == {
        verified.json()["id"]
    }
    for receipt in evidence["receipts"]:
        signer = keys.merchant if receipt["receipt_type"] == "checkout" else keys.payment_processor
        claims = signer.verify(receipt["signed_jwt"], keys.audience, require_expiration=False)
        assert claims["status"] == "Success"
        assert claims["reference"] == receipt["reference"]
    with session_factory() as db:
        assert db.scalar(select(func.count()).select_from(Ap2Receipt)) == 2


@pytest.mark.anyio
async def test_expired_ap2_challenge_releases_checkout_without_approval(
    client: httpx.AsyncClient,
    seeded: dict[str, object],
    session_factory: sessionmaker[Session],
) -> None:
    keys = _keys()
    app.dependency_overrides[get_ap2_key_set] = lambda: keys
    checkout = await _agent_checkout(client, seeded, session_factory)
    challenge = await _challenge(client, seeded, checkout)
    with session_factory() as db, db.begin():
        stored = db.get(Ap2ConsentChallenge, uuid.UUID(challenge["id"]))
        assert stored is not None
        stored.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    response = await client.post(
        f"/api/v1/checkouts/{checkout['id']}/ap2/approve",
        headers={**_auth(seeded), "Idempotency-Key": "ap2-approve-expired-001"},
        json=_approval_body(checkout, challenge),
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "ap2_challenge_expired"
    with session_factory() as db:
        stored = db.get(Ap2ConsentChallenge, uuid.UUID(challenge["id"]))
        exact_checkout = db.get(Checkout, uuid.UUID(checkout["id"]))
        assert stored is not None and stored.status == "rejected"
        assert exact_checkout is not None and exact_checkout.status.value == "expired"
        assert db.scalar(select(func.count()).select_from(CheckoutApproval)) == 0


@pytest.mark.anyio
async def test_tampered_merchant_checkout_jwt_is_rejected_before_approval(
    client: httpx.AsyncClient,
    seeded: dict[str, object],
    session_factory: sessionmaker[Session],
) -> None:
    keys = _keys()
    app.dependency_overrides[get_ap2_key_set] = lambda: keys
    checkout = await _agent_checkout(client, seeded, session_factory)
    challenge = await _challenge(client, seeded, checkout)
    with session_factory() as db, db.begin():
        stored = db.get(Ap2ConsentChallenge, uuid.UUID(challenge["id"]))
        assert stored is not None
        stored.checkout_jwt = f"{stored.checkout_jwt}tampered"
    response = await client.post(
        f"/api/v1/checkouts/{checkout['id']}/ap2/approve",
        headers={**_auth(seeded), "Idempotency-Key": "ap2-approve-signature-001"},
        json=_approval_body(checkout, challenge),
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "ap2_invalid_signature"
    with session_factory() as db:
        assert db.scalar(select(func.count()).select_from(CheckoutApproval)) == 0
        assert db.scalar(select(func.count()).select_from(Ap2Mandate)) == 0
