import httpx
import pytest


@pytest.mark.anyio
async def test_health(client: httpx.AsyncClient) -> None:
    response = await client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "commerce-core"}


@pytest.mark.anyio
async def test_catalog_is_location_aware(client: httpx.AsyncClient) -> None:
    response = await client.get(
        "/api/v1/merchants/ember-and-leaf/catalog",
        params={"postal_code": "560038", "query": "coffee"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["location_id"] is not None
    assert [product["name"] for product in body["products"]] == ["Citrus Bloom Coffee"]
    assert body["products"][0]["variants"][0]["available_quantity"] == 2


@pytest.mark.anyio
async def test_location_filter_rejects_unserviceable_postcode(
    client: httpx.AsyncClient,
) -> None:
    response = await client.get(
        "/api/v1/merchants/ember-and-leaf/locations",
        params={"postal_code": "999999"},
    )
    assert response.status_code == 200
    assert response.json()["locations"] == []
