from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import AuditEvent, Checkout, Order
from app.domain.enums import CheckoutStatus, FulfillmentType, OrderStatus


async def _merchant_headers(client: httpx.AsyncClient, seeded: dict[str, object]) -> dict[str, str]:
    response = await client.post(
        "/api/v1/auth/login",
        json={
            "email": seeded["merchant_admin_email"],
            "password": seeded["merchant_admin_password"],
        },
    )
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


@pytest.mark.anyio
async def test_operations_are_role_gated_and_report_low_stock(
    client: httpx.AsyncClient, seeded: dict[str, object]
) -> None:
    customer = await client.post(
        "/api/v1/auth/register",
        json={
            "email": "customer@example.com",
            "password": "correct-horse-battery-staple",
            "full_name": "Customer",
        },
    )
    denied = await client.get(
        "/api/v1/merchant/operations/dashboard",
        headers={"Authorization": f"Bearer {customer.json()['access_token']}"},
    )
    assert denied.status_code == 403

    dashboard = await client.get(
        "/api/v1/merchant/operations/dashboard",
        headers=await _merchant_headers(client, seeded),
    )
    assert dashboard.status_code == 200
    assert dashboard.json()["summary"]["low_stock_variants"] == 1
    assert dashboard.json()["inventory_attention"][0]["available_quantity"] == 2


@pytest.mark.anyio
async def test_inventory_adjustment_is_bounded_and_audited(
    client: httpx.AsyncClient,
    seeded: dict[str, object],
    session_factory: sessionmaker[Session],
) -> None:
    headers = await _merchant_headers(client, seeded)
    inventory = await client.get("/api/v1/merchant/operations/inventory", headers=headers)
    inventory_id = inventory.json()[0]["id"]
    adjusted = await client.patch(
        f"/api/v1/merchant/operations/inventory/{inventory_id}",
        headers=headers,
        json={"on_hand_quantity": 5, "reason": "Counted during opening stock check"},
    )
    assert adjusted.status_code == 200
    assert adjusted.json()["on_hand_quantity"] == 5
    with session_factory() as db:
        event = db.scalar(select(AuditEvent).where(AuditEvent.event_type == "inventory.adjusted"))
        assert event is not None
        assert event.payload["before_on_hand"] == 2
        assert event.payload["after_on_hand"] == 5


@pytest.mark.anyio
async def test_order_transitions_are_scoped_and_payment_state_is_not_manual(
    client: httpx.AsyncClient,
    seeded: dict[str, object],
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as db, db.begin():
        checkout = Checkout(
            merchant_id=seeded["merchant_id"],
            location_id=seeded["location_id"],
            customer_id=None,
            status=CheckoutStatus.COMPLETED,
            currency="INR",
            idempotency_key="operations-order-test",
            request_hash="0" * 64,
            fulfillment_type=FulfillmentType.LOCAL_DELIVERY,
            postal_code="560038",
            subtotal_minor=50000,
            delivery_minor=0,
            discount_minor=0,
            tax_minor=0,
            total_minor=50000,
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
        )
        db.add(checkout)
        db.flush()
        awaiting = Order(
            merchant_id=seeded["merchant_id"],
            checkout_id=checkout.id,
            public_number="EL-TEST-001",
            status=OrderStatus.AWAITING_PAYMENT,
            currency="INR",
            total_minor=50000,
        )
        db.add(awaiting)
        db.flush()
        order_id = awaiting.id

    headers = await _merchant_headers(client, seeded)
    manual_paid = await client.patch(
        f"/api/v1/merchant/operations/orders/{order_id}",
        headers=headers,
        json={"status": "paid"},
    )
    assert manual_paid.status_code == 409
    assert manual_paid.json()["error"]["code"] == "invalid_order_transition"

    canceled = await client.patch(
        f"/api/v1/merchant/operations/orders/{order_id}",
        headers=headers,
        json={"status": "canceled"},
    )
    assert canceled.status_code == 200
    assert canceled.json()["status"] == "canceled"
