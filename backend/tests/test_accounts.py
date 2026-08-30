import httpx
import pytest


async def _register(client: httpx.AsyncClient, email: str = "alok@example.com") -> dict:
    response = await client.post(
        "/api/v1/auth/register",
        json={
            "email": email,
            "password": "correct-horse-battery-staple",
            "full_name": "Alok Dangre",
            "phone": "+91 98765 43210",
        },
    )
    assert response.status_code == 201
    return response.json()


@pytest.mark.anyio
async def test_register_profile_and_logout(client: httpx.AsyncClient) -> None:
    registered = await _register(client)
    headers = {"Authorization": f"Bearer {registered['access_token']}"}

    profile = await client.get("/api/v1/me", headers=headers)
    assert profile.status_code == 200
    assert profile.json()["email"] == "alok@example.com"
    assert profile.json()["role"] == "customer"

    updated = await client.patch(
        "/api/v1/me",
        headers=headers,
        json={"full_name": "Alok D.", "phone": "+91 90000 00000"},
    )
    assert updated.status_code == 200
    assert updated.json()["full_name"] == "Alok D."

    logged_out = await client.post("/api/v1/auth/logout", headers=headers)
    assert logged_out.status_code == 204
    denied = await client.get("/api/v1/me", headers=headers)
    assert denied.status_code == 401
    assert denied.json()["error"]["code"] == "invalid_session"


@pytest.mark.anyio
async def test_duplicate_registration_and_bad_login_are_safe(client: httpx.AsyncClient) -> None:
    await _register(client)
    duplicate = await client.post(
        "/api/v1/auth/register",
        json={
            "email": "ALOK@example.com",
            "password": "another-long-password",
            "full_name": "Someone Else",
        },
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "email_already_registered"

    bad_login = await client.post(
        "/api/v1/auth/login",
        json={"email": "alok@example.com", "password": "wrong"},
    )
    assert bad_login.status_code == 401
    assert bad_login.json()["error"]["code"] == "invalid_credentials"


@pytest.mark.anyio
async def test_addresses_are_owned_and_default_is_unique(client: httpx.AsyncClient) -> None:
    registered = await _register(client)
    headers = {"Authorization": f"Bearer {registered['access_token']}"}
    base = {
        "recipient_name": "Alok Dangre",
        "phone": "+91 98765 43210",
        "line_one": "12, 1st Main Road",
        "city": "Bengaluru",
        "region": "Karnataka",
        "postal_code": "560038",
    }
    home = await client.post(
        "/api/v1/me/addresses", headers=headers, json={**base, "label": "Home"}
    )
    assert home.status_code == 201
    assert home.json()["is_default"] is True

    work = await client.post(
        "/api/v1/me/addresses",
        headers=headers,
        json={
            **base,
            "label": "Work",
            "line_one": "45, CMH Road",
            "is_default": True,
        },
    )
    assert work.status_code == 201
    addresses = await client.get("/api/v1/me/addresses", headers=headers)
    assert sum(address["is_default"] for address in addresses.json()["addresses"]) == 1
    assert addresses.json()["addresses"][0]["label"] == "Work"

    removed = await client.delete(f"/api/v1/me/addresses/{work.json()['id']}", headers=headers)
    assert removed.status_code == 204
    remaining = await client.get("/api/v1/me/addresses", headers=headers)
    assert remaining.json()["addresses"][0]["label"] == "Home"
    assert remaining.json()["addresses"][0]["is_default"] is True
