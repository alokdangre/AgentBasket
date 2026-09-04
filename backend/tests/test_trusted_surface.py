import httpx
import pytest


def _auth(seeded: dict[str, object]) -> dict[str, str]:
    return {"Authorization": f"Bearer {seeded['customer_token']}"}


@pytest.mark.anyio
async def test_registration_options_and_cp_instruments_are_customer_scoped(
    client: httpx.AsyncClient, seeded: dict[str, object]
) -> None:
    empty = await client.get("/api/v1/me/passkeys", headers=_auth(seeded))
    options = await client.post("/api/v1/me/passkeys/registration/options", headers=_auth(seeded))
    instruments = await client.get(
        "/api/v1/credential-provider/payment-instruments", headers=_auth(seeded)
    )

    assert empty.status_code == 200
    assert empty.json() == {"passkeys": []}
    assert options.status_code == 201
    assert options.json()["public_key"]["rp"]["id"] == "localhost"
    assert options.json()["public_key"]["authenticatorSelection"]["userVerification"] == "required"
    assert instruments.status_code == 200
    assert instruments.json()["payment_instruments"] == [
        {
            "id": str(seeded["payment_instrument_id"]),
            "provider": "razorpay_test",
            "instrument_type": "com.razorpay.standard.test",
            "alias": "Razorpay Test Checkout",
            "network": None,
            "last4": None,
            "is_default": True,
            "requires_provider_checkout": True,
            "recurring_ready": False,
            "recurring_status": None,
        }
    ]
