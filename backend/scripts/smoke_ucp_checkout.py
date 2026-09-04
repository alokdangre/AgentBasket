"""Exercise UCP discovery, catalog selection, checkout handoff, and optional claim."""

from __future__ import annotations

import argparse
import uuid

import httpx


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument(
        "--agent-profile",
        default="https://buyer.example/.well-known/ucp",
    )
    parser.add_argument("--customer-token")
    parser.add_argument("--address-id")
    return parser.parse_args()


def request_headers(agent_profile: str) -> dict[str, str]:
    return {
        "Request-Id": str(uuid.uuid4()),
        "UCP-Agent": f'profile="{agent_profile}"',
    }


def checked(response: httpx.Response) -> dict:
    if response.is_error:
        raise SystemExit(f"{response.request.method} {response.request.url}: {response.text}")
    return response.json()


def main() -> None:
    args = parse_args()
    base_url = args.base_url.rstrip("/")
    with httpx.Client(base_url=base_url, timeout=10) as client:
        profile = checked(client.get("/.well-known/ucp"))["ucp"]
        capabilities = profile["capabilities"]
        assert "dev.ucp.shopping.checkout" in capabilities
        print(f"PASS discovery {profile['version']} with UCP checkout")

        catalog = checked(
            client.post(
                "/ucp/catalog/search",
                headers=request_headers(args.agent_profile),
                json={"query": "V60 filter papers", "pagination": {"limit": 5}},
            )
        )
        if not catalog["products"]:
            raise SystemExit("No V60 filter-paper example found. Run the database seed first.")
        variant = catalog["products"][0]["variants"][0]
        print(f"PASS catalog selected {variant['sku']} ({variant['id']})")

        create_headers = request_headers(args.agent_profile) | {
            "Idempotency-Key": f"smoke-{uuid.uuid4()}"
        }
        checkout = checked(
            client.post(
                "/ucp/checkout-sessions",
                headers=create_headers,
                json={"line_items": [{"item": {"id": variant["id"]}, "quantity": 1}]},
            )
        )
        assert checkout["status"] == "requires_escalation"
        assert checkout["ucp"]["payment_handlers"] == {}
        print(f"PASS checkout handoff {checkout['id']}")
        print(f"OPEN {checkout['continue_url']}")

        restored = checked(
            client.get(
                f"/ucp/checkout-sessions/{checkout['id']}",
                headers=request_headers(args.agent_profile),
            )
        )
        assert restored["id"] == checkout["id"]
        print("PASS checkout recovery")

        completion = checked(
            client.post(
                f"/ucp/checkout-sessions/{checkout['id']}/complete",
                headers=request_headers(args.agent_profile),
            )
        )
        assert completion["messages"][-1]["code"] == "buyer_handoff_required"
        print("PASS agent payment blocked; trusted buyer handoff required")

        if args.customer_token:
            auth = {"Authorization": f"Bearer {args.customer_token}"}
            claim = checked(
                client.post(
                    f"/api/v1/ucp/checkout-handoffs/{checkout['id']}/claim",
                    headers=auth,
                )
            )
            print(f"PASS claimed; imported {claim['imported_item_count']} item(s)")
            if args.address_id:
                exact = checked(
                    client.post(
                        f"/api/v1/ucp/checkout-handoffs/{checkout['id']}/checkout",
                        headers=auth | {"Idempotency-Key": f"smoke-quote-{uuid.uuid4()}"},
                        json={
                            "merchant_slug": "ember-and-leaf",
                            "fulfillment_type": "local_delivery",
                            "address_id": args.address_id,
                        },
                    )
                )
                assert exact["source"] == "ucp_agent"
                print(
                    "PASS exact UCP-sourced checkout "
                    f"{exact['id']} total={exact['total_minor']} {exact['currency']}"
                )
            else:
                print("SKIP exact quote: pass --address-id with --customer-token")


if __name__ == "__main__":
    main()
