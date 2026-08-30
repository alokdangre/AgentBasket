import httpx
import pytest


async def _auth_headers(client: httpx.AsyncClient, email: str) -> dict[str, str]:
    response = await client.post(
        "/api/v1/auth/register",
        json={
            "email": email,
            "password": "correct-horse-battery-staple",
            "full_name": "Cart Customer",
        },
    )
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


@pytest.mark.anyio
async def test_cart_prices_modifiers_and_merges_configuration(
    client: httpx.AsyncClient, seeded: dict[str, object]
) -> None:
    headers = await _auth_headers(client, "cart@example.com")
    payload = {
        "variant_id": str(seeded["drink_variant_id"]),
        "quantity": 1,
        "modifier_option_ids": [str(seeded["unsweetened_id"]), str(seeded["oat_id"])],
    }
    first = await client.post("/api/v1/cart/items", headers=headers, json=payload)
    assert first.status_code == 201
    assert first.json()["item_count"] == 1
    assert first.json()["subtotal_minor"] == 26000

    second = await client.post("/api/v1/cart/items", headers=headers, json=payload)
    assert second.status_code == 201
    assert second.json()["item_count"] == 2
    assert second.json()["subtotal_minor"] == 52000
    assert len(second.json()["items"]) == 1


@pytest.mark.anyio
async def test_cart_is_private_and_inventory_is_bounded(
    client: httpx.AsyncClient, seeded: dict[str, object]
) -> None:
    first_headers = await _auth_headers(client, "first@example.com")
    second_headers = await _auth_headers(client, "second@example.com")
    payload = {
        "variant_id": str(seeded["bean_variant_id"]),
        "quantity": 3,
        "modifier_option_ids": [str(seeded["whole_id"])],
    }
    unavailable = await client.post("/api/v1/cart/items", headers=first_headers, json=payload)
    assert unavailable.status_code == 409
    assert unavailable.json()["error"]["code"] == "insufficient_inventory"

    payload["quantity"] = 1
    added = await client.post("/api/v1/cart/items", headers=first_headers, json=payload)
    assert added.status_code == 201
    other_cart = await client.get("/api/v1/cart", headers=second_headers)
    assert other_cart.status_code == 200
    assert other_cart.json()["items"] == []


@pytest.mark.anyio
async def test_cart_rejects_modifier_from_another_product(
    client: httpx.AsyncClient, seeded: dict[str, object]
) -> None:
    headers = await _auth_headers(client, "modifier@example.com")
    response = await client.post(
        "/api/v1/cart/items",
        headers=headers,
        json={
            "variant_id": str(seeded["bean_variant_id"]),
            "quantity": 1,
            "modifier_option_ids": [str(seeded["unsweetened_id"])],
        },
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_modifier"
