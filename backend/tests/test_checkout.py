import httpx
import pytest
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import InventoryItem


def _headers(seeded: dict[str, object], idempotency_key: str | None = None) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {seeded['customer_token']}"}
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    return headers


def _drink_payload(seeded: dict[str, object]) -> dict:
    return {
        "merchant_slug": "ember-and-leaf",
        "fulfillment_type": "local_delivery",
        "postal_code": "560038",
        "items": [
            {
                "variant_id": str(seeded["drink_variant_id"]),
                "quantity": 1,
                "modifier_option_ids": [
                    str(seeded["unsweetened_id"]),
                    str(seeded["oat_id"]),
                ],
            }
        ],
    }


def _beans_payload(seeded: dict[str, object], quantity: int = 1) -> dict:
    return {
        "merchant_slug": "ember-and-leaf",
        "fulfillment_type": "local_delivery",
        "postal_code": "560038",
        "items": [
            {
                "variant_id": str(seeded["bean_variant_id"]),
                "quantity": quantity,
                "modifier_option_ids": [str(seeded["whole_id"])],
            }
        ],
    }


@pytest.mark.anyio
async def test_checkout_uses_minor_units_and_modifier_pricing(
    client: httpx.AsyncClient, seeded: dict[str, object]
) -> None:
    response = await client.post(
        "/api/v1/checkouts",
        json=_drink_payload(seeded),
        headers=_headers(seeded, "drink-checkout-001"),
    )
    assert response.status_code == 201
    body = response.json()
    assert body["subtotal_minor"] == 26000
    assert body["delivery_minor"] == 4900
    assert body["total_minor"] == 30900
    assert body["tax_included"] is True
    assert body["status"] == "ready_for_approval"


@pytest.mark.anyio
async def test_checkout_retry_is_idempotent(
    client: httpx.AsyncClient, seeded: dict[str, object]
) -> None:
    payload = _beans_payload(seeded)
    headers = _headers(seeded, "beans-checkout-001")
    first = await client.post("/api/v1/checkouts", json=payload, headers=headers)
    second = await client.post("/api/v1/checkouts", json=payload, headers=headers)
    assert first.status_code == second.status_code == 201
    assert first.json()["id"] == second.json()["id"]


@pytest.mark.anyio
async def test_idempotency_key_cannot_be_reused_for_a_different_request(
    client: httpx.AsyncClient, seeded: dict[str, object]
) -> None:
    headers = _headers(seeded, "checkout-conflict-001")
    first = await client.post("/api/v1/checkouts", json=_drink_payload(seeded), headers=headers)
    second = await client.post("/api/v1/checkouts", json=_beans_payload(seeded), headers=headers)
    assert first.status_code == 201
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "idempotency_key_reused"


@pytest.mark.anyio
async def test_free_delivery_and_inventory_reservation(
    client: httpx.AsyncClient,
    seeded: dict[str, object],
    session_factory: sessionmaker[Session],
) -> None:
    response = await client.post(
        "/api/v1/checkouts",
        json=_beans_payload(seeded),
        headers=_headers(seeded, "beans-free-delivery"),
    )
    assert response.status_code == 201
    assert response.json()["subtotal_minor"] == 119000
    assert response.json()["delivery_minor"] == 0
    with session_factory() as db:
        inventory = db.get(InventoryItem, seeded["inventory_id"])
        assert inventory is not None
        assert inventory.reserved_quantity == 1


@pytest.mark.anyio
async def test_cancel_releases_inventory(
    client: httpx.AsyncClient,
    seeded: dict[str, object],
    session_factory: sessionmaker[Session],
) -> None:
    created = await client.post(
        "/api/v1/checkouts",
        json=_beans_payload(seeded),
        headers=_headers(seeded, "beans-cancel-001"),
    )
    checkout_id = created.json()["id"]
    canceled = await client.post(
        f"/api/v1/checkouts/{checkout_id}/cancel", headers=_headers(seeded)
    )
    assert canceled.status_code == 200
    assert canceled.json()["status"] == "canceled"
    with session_factory() as db:
        inventory = db.get(InventoryItem, seeded["inventory_id"])
        assert inventory is not None
        assert inventory.reserved_quantity == 0


@pytest.mark.anyio
async def test_insufficient_inventory_is_a_conflict(
    client: httpx.AsyncClient, seeded: dict[str, object]
) -> None:
    response = await client.post(
        "/api/v1/checkouts",
        json=_beans_payload(seeded, quantity=3),
        headers=_headers(seeded, "beans-out-of-stock"),
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "insufficient_inventory"


@pytest.mark.anyio
async def test_unserviceable_delivery_is_not_quoted(
    client: httpx.AsyncClient, seeded: dict[str, object]
) -> None:
    payload = _drink_payload(seeded)
    payload["postal_code"] = "999999"
    response = await client.post(
        "/api/v1/checkouts",
        json=payload,
        headers=_headers(seeded, "unserviceable-address"),
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "address_not_serviceable"


@pytest.mark.anyio
async def test_checkout_rejects_a_variant_from_another_merchant(
    client: httpx.AsyncClient, seeded: dict[str, object]
) -> None:
    payload = _beans_payload(seeded)
    payload["items"][0]["variant_id"] = str(seeded["other_variant_id"])
    payload["items"][0]["modifier_option_ids"] = []
    response = await client.post(
        "/api/v1/checkouts",
        json=payload,
        headers=_headers(seeded, "cross-merchant-variant"),
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "variant_not_found"
