import httpx
import pytest

pytestmark = pytest.mark.anyio

UCP_HEADERS = {
    "Request-Id": "buyer-agent-request-1",
    "UCP-Agent": 'profile="https://buyer.example/.well-known/ucp"',
}


async def test_ucp_discovery_profile_is_versioned_cacheable_and_conditional(
    client: httpx.AsyncClient,
) -> None:
    response = await client.get("/.well-known/ucp")

    assert response.status_code == 200
    profile = response.json()["ucp"]
    assert profile["version"] == "2026-08-25"
    assert profile["payment_handlers"] == {}
    service = profile["services"]["dev.ucp.shopping"][0]
    assert service["transport"] == "rest"
    assert service["endpoint"].endswith("/ucp")
    assert service["schema"].endswith("/services/shopping/rest.openapi.json")
    assert set(profile["capabilities"]) == {
        "dev.ucp.shopping.catalog.search",
        "dev.ucp.shopping.catalog.lookup",
        "dev.ucp.shopping.checkout",
    }
    assert response.headers["cache-control"].startswith("public")
    etag = response.headers["etag"]

    unchanged = await client.get("/.well-known/ucp", headers={"If-None-Match": etag})
    assert unchanged.status_code == 304
    assert unchanged.headers["etag"] == etag


async def test_ucp_catalog_search_filters_prices_and_paginates(
    client: httpx.AsyncClient,
) -> None:
    search = await client.post(
        "/ucp/catalog/search",
        headers=UCP_HEADERS,
        json={
            "query": "smooth refreshing cold coffee",
            "context": {"postal_code": "560038", "currency": "INR"},
            "filters": {
                "categories": ["prepared_beverage"],
                "price": {"max": 30000},
            },
            "pagination": {"limit": 10},
        },
    )

    assert search.status_code == 200
    payload = search.json()
    assert payload["ucp"] == {
        "version": "2026-08-25",
        "capabilities": {"dev.ucp.shopping.catalog.search": [{"version": "2026-08-25"}]},
    }
    assert payload["pagination"] == {"has_next_page": False, "total_count": 1}
    product = payload["products"][0]
    assert product["title"] == "House Cold Brew"
    assert product["categories"] == [{"value": "prepared_beverage", "taxonomy": "merchant"}]
    assert product["variants"][0]["id"]
    assert product["variants"][0]["availability"] == {"available": True}
    assert product["variants"][0]["seller"] == {"name": "Ember & Leaf"}

    first_page = await client.post(
        "/ucp/catalog/search",
        headers={**UCP_HEADERS, "Request-Id": "buyer-agent-page-1"},
        json={"pagination": {"limit": 1}},
    )
    assert first_page.status_code == 200
    first_payload = first_page.json()
    assert first_payload["pagination"]["has_next_page"] is True
    cursor = first_payload["pagination"]["cursor"]

    second_page = await client.post(
        "/ucp/catalog/search",
        headers={**UCP_HEADERS, "Request-Id": "buyer-agent-page-2"},
        json={"pagination": {"limit": 1, "cursor": cursor}},
    )
    assert second_page.status_code == 200
    assert second_page.json()["products"][0]["id"] != first_payload["products"][0]["id"]


async def test_ucp_lookup_correlates_ids_and_product_detail_handles_missing_items(
    client: httpx.AsyncClient,
    seeded: dict[str, object],
) -> None:
    variant_id = str(seeded["drink_variant_id"])
    lookup = await client.post(
        "/ucp/catalog/lookup",
        headers=UCP_HEADERS,
        json={"ids": [variant_id, "DRINK-CB-REG", "unknown-item"]},
    )

    assert lookup.status_code == 200
    payload = lookup.json()
    assert len(payload["products"]) == 1
    variant = payload["products"][0]["variants"][0]
    assert variant["id"] == variant_id
    assert variant["inputs"] == [
        {"id": variant_id, "match": "exact"},
        {"id": "DRINK-CB-REG", "match": "exact"},
    ]
    assert payload["messages"] == [{"type": "info", "code": "not_found", "content": "unknown-item"}]

    detail = await client.post(
        "/ucp/catalog/product",
        headers={**UCP_HEADERS, "Request-Id": "buyer-agent-detail-1"},
        json={"id": variant_id},
    )
    assert detail.status_code == 200
    detail_payload = detail.json()
    assert detail_payload["product"]["variants"][0]["id"] == variant_id
    assert detail_payload["product"]["selected"] == [{"name": "Size", "label": "Regular"}]

    invalid_selection = await client.post(
        "/ucp/catalog/product",
        headers={**UCP_HEADERS, "Request-Id": "buyer-agent-detail-invalid"},
        json={
            "id": detail_payload["product"]["id"],
            "selected": [{"name": "Size", "label": "Imaginary size"}],
        },
    )
    assert invalid_selection.status_code == 200
    assert "selected" not in invalid_selection.json()["product"]
    assert invalid_selection.json()["messages"][0]["code"] == "invalid_selection"

    missing = await client.post(
        "/ucp/catalog/product",
        headers={**UCP_HEADERS, "Request-Id": "buyer-agent-detail-2"},
        json={"id": "unknown-item"},
    )
    assert missing.status_code == 200
    assert missing.json()["ucp"]["status"] == "error"
    assert missing.json()["messages"][0]["severity"] == "unrecoverable"


async def test_ucp_requires_protocol_agent_headers(client: httpx.AsyncClient) -> None:
    missing = await client.post("/ucp/catalog/search", json={})
    assert missing.status_code == 422

    malformed = await client.post(
        "/ucp/catalog/search",
        headers={
            "Request-Id": "buyer-agent-request-2",
            "UCP-Agent": 'profile="http://private.local/.well-known/ucp"',
        },
        json={},
    )
    assert malformed.status_code == 400


async def test_ucp_unserviceable_context_is_a_safe_empty_result(
    client: httpx.AsyncClient,
) -> None:
    response = await client.post(
        "/ucp/catalog/search",
        headers={**UCP_HEADERS, "Request-Id": "buyer-agent-request-3"},
        json={"context": {"postal_code": "400001"}},
    )

    assert response.status_code == 200
    assert response.json()["products"] == []
    assert response.json()["messages"][0]["code"] == "address_not_serviceable"


async def test_ucp_checkout_is_idempotent_and_requires_trusted_payment_handoff(
    client: httpx.AsyncClient,
    seeded: dict[str, object],
) -> None:
    headers = {**UCP_HEADERS, "Idempotency-Key": "ucp-checkout-create-1"}
    request = {"line_items": [{"item": {"id": str(seeded["accessory_variant_id"])}, "quantity": 2}]}
    created = await client.post("/ucp/checkout-sessions", headers=headers, json=request)

    assert created.status_code == 201
    payload = created.json()
    assert payload["status"] == "requires_escalation"
    assert payload["ucp"]["payment_handlers"] == {}
    assert payload["ucp"]["capabilities"] == {
        "dev.ucp.shopping.checkout": [{"version": "2026-08-25"}]
    }
    assert payload["totals"][-1] == {"type": "total", "amount": 90000}
    assert payload["continue_url"].endswith(f"/checkout/handoff/{payload['id']}")
    assert payload["messages"][0]["severity"] == "requires_buyer_review"

    replay = await client.post("/ucp/checkout-sessions", headers=headers, json=request)
    assert replay.status_code == 201
    assert replay.json()["id"] == payload["id"]

    collision = await client.post(
        "/ucp/checkout-sessions",
        headers=headers,
        json={"line_items": [{"item": {"id": str(seeded["accessory_variant_id"])}, "quantity": 1}]},
    )
    assert collision.status_code == 409
    assert collision.json()["error"]["code"] == "idempotency_key_reused"


async def test_ucp_checkout_lifecycle_is_agent_scoped(
    client: httpx.AsyncClient,
    seeded: dict[str, object],
) -> None:
    created = await client.post(
        "/ucp/checkout-sessions",
        headers={**UCP_HEADERS, "Idempotency-Key": "ucp-checkout-lifecycle-1"},
        json={"line_items": [{"item": {"id": str(seeded["accessory_variant_id"])}, "quantity": 1}]},
    )
    checkout_id = created.json()["id"]

    hidden = await client.get(
        f"/ucp/checkout-sessions/{checkout_id}",
        headers={
            "Request-Id": "other-agent-read",
            "UCP-Agent": 'profile="https://other.example/.well-known/ucp"',
        },
    )
    assert hidden.status_code == 404

    updated = await client.put(
        f"/ucp/checkout-sessions/{checkout_id}",
        headers={**UCP_HEADERS, "Request-Id": "ucp-update-1"},
        json={"line_items": [{"item": {"id": "FILTER-V60-100"}, "quantity": 3}]},
    )
    assert updated.status_code == 200
    assert updated.json()["totals"][-1]["amount"] == 135000

    completion = await client.post(
        f"/ucp/checkout-sessions/{checkout_id}/complete",
        headers={**UCP_HEADERS, "Request-Id": "ucp-complete-1"},
    )
    assert completion.status_code == 200
    assert completion.json()["status"] == "requires_escalation"
    assert completion.json()["messages"][-1]["code"] == "buyer_handoff_required"

    canceled = await client.post(
        f"/ucp/checkout-sessions/{checkout_id}/cancel",
        headers={**UCP_HEADERS, "Request-Id": "ucp-cancel-1"},
    )
    assert canceled.status_code == 200
    assert canceled.json()["status"] == "canceled"
    assert "continue_url" not in canceled.json()


async def test_customer_claim_imports_ready_items_and_preserves_configuration_choice(
    client: httpx.AsyncClient,
    seeded: dict[str, object],
) -> None:
    created = await client.post(
        "/ucp/checkout-sessions",
        headers={**UCP_HEADERS, "Idempotency-Key": "ucp-checkout-claim-1"},
        json={
            "line_items": [
                {"item": {"id": str(seeded["accessory_variant_id"])}, "quantity": 2},
                {"item": {"id": str(seeded["drink_variant_id"])}, "quantity": 1},
            ]
        },
    )
    payload = created.json()
    assert [message["code"] for message in payload["messages"]] == [
        "buyer_review_required",
        "item_configuration_required",
    ]

    auth = {"Authorization": f"Bearer {seeded['customer_token']}"}
    claimed = await client.post(
        f"/api/v1/ucp/checkout-handoffs/{payload['id']}/claim",
        headers=auth,
    )
    assert claimed.status_code == 200
    result = claimed.json()
    assert result["imported_item_count"] == 2
    assert result["configuration_required"][0]["product_name"] == "House Cold Brew"
    assert result["next_url"] == f"/cart?ucp_handoff={payload['id']}"

    replay = await client.post(
        f"/api/v1/ucp/checkout-handoffs/{payload['id']}/claim",
        headers=auth,
    )
    assert replay.status_code == 200
    assert replay.json() == result

    cart = await client.get("/api/v1/cart", headers=auth)
    assert cart.status_code == 200
    assert cart.json()["item_count"] == 2
    assert cart.json()["items"][0]["product_name"] == "V60 Filter Papers"

    checkout = await client.post(
        f"/api/v1/ucp/checkout-handoffs/{payload['id']}/checkout",
        headers={**auth, "Idempotency-Key": "ucp-trusted-checkout-1"},
        json={
            "merchant_slug": "ember-and-leaf",
            "fulfillment_type": "local_delivery",
            "address_id": str(seeded["customer_address_id"]),
        },
    )
    assert checkout.status_code == 201
    assert checkout.json()["source"] == "ucp_agent"
    assert checkout.json()["status"] == "ready_for_approval"
