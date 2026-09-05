from __future__ import annotations

import argparse
import uuid

import httpx

from app.core.config import get_settings


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Smoke-test public agent boundaries and authenticated memory reads."
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    return parser.parse_args()


def require_status(response: httpx.Response, expected: int, label: str) -> None:
    if response.status_code != expected:
        raise RuntimeError(f"{label}: expected {expected}, received {response.status_code}")
    print(f"PASS {label} status={response.status_code}")


def main() -> int:
    args = parse_args()
    settings = get_settings()
    profile = 'profile="https://smoke-buyer.example/.well-known/ucp"'

    with httpx.Client(base_url=args.base_url, timeout=10.0) as client:
        health = client.get("/health")
        require_status(health, 200, "health")
        if health.json().get("status") != "ok":
            raise RuntimeError("health: service did not report ok")

        discovery = client.get("/.well-known/ucp")
        require_status(discovery, 200, "ucp_discovery")
        ucp = discovery.json().get("ucp", {})
        if ucp.get("version") != "2026-08-25" or ucp.get("payment_handlers") != {}:
            raise RuntimeError("ucp_discovery: unsafe version or payment-handler advertisement")

        request_headers = {
            "Request-Id": f"security-smoke-{uuid.uuid4()}",
            "UCP-Agent": profile,
        }
        catalog = client.post(
            "/ucp/catalog/search",
            headers=request_headers,
            json={"query": "prepared beverage", "pagination": {"limit": 3}},
        )
        require_status(catalog, 200, "ucp_catalog")
        if not catalog.json().get("products"):
            raise RuntimeError("ucp_catalog: seeded catalog returned no products")

        missing_headers = client.post("/ucp/catalog/search", json={})
        require_status(missing_headers, 422, "ucp_missing_headers_denied")

        unsafe_profile = client.post(
            "/ucp/catalog/search",
            headers={
                "Request-Id": f"security-smoke-{uuid.uuid4()}",
                "UCP-Agent": 'profile="http://private.local/.well-known/ucp"',
            },
            json={},
        )
        require_status(unsafe_profile, 400, "ucp_insecure_profile_denied")

        if settings.demo_customer_email is None or settings.demo_customer_password is None:
            print("SKIP authenticated_memory demo credentials are not configured")
            return 0

        login = client.post(
            "/api/v1/auth/login",
            json={
                "email": settings.demo_customer_email,
                "password": settings.demo_customer_password.get_secret_value(),
            },
        )
        require_status(login, 200, "demo_login")
        access_token = login.json().get("access_token")
        if not access_token:
            raise RuntimeError("demo_login: access token missing")

        memory = client.get(
            "/api/v1/agent/memory/ember-and-leaf",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        require_status(memory, 200, "authenticated_memory_read")
        body = memory.json()
        if body.get("merchant_slug") != "ember-and-leaf" or "enabled" not in body:
            raise RuntimeError("authenticated_memory_read: invalid response shape")

    print("PASS security_smoke no payment capability was advertised")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
