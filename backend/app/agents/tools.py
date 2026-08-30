from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import DomainError
from app.db.models import (
    AgentToolCall,
    AuditEvent,
    CustomerAddress,
    Location,
    UserAccount,
)
from app.domain.enums import FulfillmentType
from app.schemas.cart import CartItemCreateRequest, CartItemUpdateRequest
from app.schemas.checkout import CheckoutFromCartCreate
from app.services.cart import CartService
from app.services.catalog import CatalogService
from app.services.checkout import CheckoutService

_TOKEN_PATTERN = re.compile(r"[a-z0-9]+")


def utc_now() -> datetime:
    return datetime.now(UTC)


class AgentToolbox:
    """Identity-bound commerce tools for one agent run.

    Customer and merchant identity are captured server-side and never appear as
    LLM-controlled tool parameters.
    """

    def __init__(
        self,
        db: Session,
        customer: UserAccount,
        merchant_id: uuid.UUID,
        merchant_slug: str,
        conversation_id: uuid.UUID,
        run_id: uuid.UUID,
    ) -> None:
        self.db = db
        self.customer = customer
        self.merchant_id = merchant_id
        self.merchant_slug = merchant_slug
        self.conversation_id = conversation_id
        self.run_id = run_id
        self.outcomes: list[dict[str, Any]] = []

    def adk_tools(self) -> list[Callable[..., dict[str, Any]]]:
        def search_catalog(
            query: str = "", postal_code: str = "", max_price_minor: int = 0
        ) -> dict[str, Any]:
            """Search the real merchant catalog.

            Use query for product, flavor, brew method, or category terms. Set
            postal_code when the customer has supplied one. max_price_minor is
            an optional per-variant budget in INR paise; use 0 for no limit.
            """
            return self.search_catalog(query, postal_code, max_price_minor)

        def recommend_products(
            preferences: str, budget_minor: int = 0, postal_code: str = ""
        ) -> dict[str, Any]:
            """Return deterministic catalog-backed recommendations.

            preferences should contain the customer's stated taste, format,
            temperature, dietary, or brew-method needs. budget_minor is an
            optional per-product budget in INR paise; use 0 when unspecified.
            """
            return self.recommend_products(preferences, budget_minor, postal_code)

        def get_cart() -> dict[str, Any]:
            """Read the signed-in customer's current cart and authoritative subtotal."""
            return self.get_cart()

        def add_to_cart(
            variant_id: str, quantity: int, modifier_option_ids: list[str]
        ) -> dict[str, Any]:
            """Add an exact catalog configuration to the customer's cart.

            Call only after the customer's latest message explicitly asks to add
            it. Use variant and modifier IDs returned by catalog tools. Required
            modifier groups must be satisfied.
            """
            return self.add_to_cart(variant_id, quantity, modifier_option_ids)

        def update_cart_item(cart_item_id: str, quantity: int) -> dict[str, Any]:
            """Change a current cart line quantity after an explicit customer request."""
            return self.update_cart_item(cart_item_id, quantity)

        def remove_cart_item(cart_item_id: str) -> dict[str, Any]:
            """Remove a current cart line after an explicit customer request."""
            return self.remove_cart_item(cart_item_id)

        def list_fulfillment_destinations() -> dict[str, Any]:
            """List the customer's saved delivery addresses and available pickup locations."""
            return self.list_fulfillment_destinations()

        def prepare_checkout(
            fulfillment_type: str, address_id: str = "", location_id: str = ""
        ) -> dict[str, Any]:
            """Prepare an exact, reserving checkout without approving or paying.

            Call only after the latest customer message explicitly asks to buy or
            check out. For local_delivery or shipping use a saved address_id. For
            pickup use a location_id. The returned quote always requires separate
            customer approval in the trusted UI.
            """
            return self.prepare_checkout(fulfillment_type, address_id, location_id)

        return [
            search_catalog,
            recommend_products,
            get_cart,
            add_to_cart,
            update_cart_item,
            remove_cart_item,
            list_fulfillment_destinations,
            prepare_checkout,
        ]

    def search_catalog(
        self, query: str = "", postal_code: str = "", max_price_minor: int = 0
    ) -> dict[str, Any]:
        return self._invoke(
            "search_catalog",
            {
                "query": query[:160],
                "postal_code": postal_code[:20],
                "max_price_minor": max_price_minor,
            },
            lambda: self._search_catalog(query, postal_code, max_price_minor),
        )

    def recommend_products(
        self, preferences: str, budget_minor: int = 0, postal_code: str = ""
    ) -> dict[str, Any]:
        return self._invoke(
            "recommend_products",
            {
                "preferences": preferences[:300],
                "budget_minor": budget_minor,
                "postal_code": postal_code[:20],
            },
            lambda: self._recommend_products(preferences, budget_minor, postal_code),
        )

    def get_cart(self) -> dict[str, Any]:
        return self._invoke("get_cart", {}, self._get_cart)

    def add_to_cart(
        self, variant_id: str, quantity: int, modifier_option_ids: list[str]
    ) -> dict[str, Any]:
        return self._invoke(
            "add_to_cart",
            {
                "variant_id": variant_id,
                "quantity": quantity,
                "modifier_option_ids": modifier_option_ids,
            },
            lambda: self._add_to_cart(variant_id, quantity, modifier_option_ids),
        )

    def update_cart_item(self, cart_item_id: str, quantity: int) -> dict[str, Any]:
        return self._invoke(
            "update_cart_item",
            {"cart_item_id": cart_item_id, "quantity": quantity},
            lambda: self._update_cart_item(cart_item_id, quantity),
        )

    def remove_cart_item(self, cart_item_id: str) -> dict[str, Any]:
        return self._invoke(
            "remove_cart_item",
            {"cart_item_id": cart_item_id},
            lambda: self._remove_cart_item(cart_item_id),
        )

    def list_fulfillment_destinations(self) -> dict[str, Any]:
        return self._invoke(
            "list_fulfillment_destinations", {}, self._list_fulfillment_destinations
        )

    def prepare_checkout(
        self, fulfillment_type: str, address_id: str = "", location_id: str = ""
    ) -> dict[str, Any]:
        return self._invoke(
            "prepare_checkout",
            {
                "fulfillment_type": fulfillment_type,
                "address_id": address_id,
                "location_id": location_id,
            },
            lambda: self._prepare_checkout(fulfillment_type, address_id, location_id),
        )

    def structured_content(self) -> dict[str, Any]:
        content: dict[str, Any] = {
            "activity": [
                {
                    "tool": outcome["tool"],
                    "status": outcome["status"],
                    "label": self._activity_label(outcome["tool"], outcome["status"]),
                }
                for outcome in self.outcomes
            ]
        }
        for outcome in self.outcomes:
            payload = outcome["payload"]
            if payload.get("products"):
                content["products"] = payload["products"]
            if "cart" in payload:
                content["cart"] = payload["cart"]
            if "checkout" in payload:
                content["checkout"] = payload["checkout"]
            if "destinations" in payload:
                content["destinations"] = payload["destinations"]
        return content

    def _search_catalog(self, query: str, postal_code: str, max_price_minor: int) -> dict[str, Any]:
        if max_price_minor < 0:
            raise DomainError("invalid_budget", "Budget cannot be negative.")
        catalog = CatalogService(self.db).search(
            self.merchant_slug,
            query=query.strip() or None,
            postal_code=postal_code.strip() or None,
        )
        products = self._compact_products(catalog.model_dump(mode="json")["products"])
        self.db.rollback()
        if max_price_minor:
            products = self._within_budget(products, max_price_minor)
        return {
            "status": "success",
            "currency": catalog.currency,
            "products": products[:8],
            "result_count": min(len(products), 8),
        }

    def _recommend_products(
        self, preferences: str, budget_minor: int, postal_code: str
    ) -> dict[str, Any]:
        if budget_minor < 0:
            raise DomainError("invalid_budget", "Budget cannot be negative.")
        catalog = CatalogService(self.db).search(
            self.merchant_slug, postal_code=postal_code.strip() or None
        )
        products = self._compact_products(catalog.model_dump(mode="json")["products"])
        self.db.rollback()
        if budget_minor:
            products = self._within_budget(products, budget_minor)
        tokens = set(_TOKEN_PATTERN.findall(preferences.lower()))
        ignored = {"a", "an", "and", "for", "i", "me", "my", "of", "the", "to", "want"}
        tokens -= ignored
        ranked: list[tuple[int, int, dict[str, Any]]] = []
        for product in products:
            haystack = json.dumps(product, sort_keys=True).lower()
            matched = sorted(token for token in tokens if token in haystack)
            product["recommendation_reason"] = (
                f"Matches your interest in {', '.join(matched[:3])}."
                if matched
                else product["description"]
            )
            minimum_price = min(variant["price_minor"] for variant in product["variants"])
            ranked.append((len(matched), -minimum_price, product))
        ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
        selected = [item[2] for item in ranked[:3]]
        return {
            "status": "success",
            "currency": catalog.currency,
            "products": selected,
            "preference_terms": sorted(tokens),
        }

    def _get_cart(self) -> dict[str, Any]:
        cart = CartService(self.db).get(self.customer, self.merchant_slug)
        return {"status": "success", "cart": cart.model_dump(mode="json")}

    def _add_to_cart(
        self, variant_id: str, quantity: int, modifier_option_ids: list[str]
    ) -> dict[str, Any]:
        payload = CartItemCreateRequest(
            variant_id=uuid.UUID(variant_id),
            quantity=quantity,
            modifier_option_ids=[uuid.UUID(option_id) for option_id in modifier_option_ids],
        )
        cart = CartService(self.db).add_item(self.customer, self.merchant_slug, payload)
        return {"status": "success", "cart": cart.model_dump(mode="json")}

    def _update_cart_item(self, cart_item_id: str, quantity: int) -> dict[str, Any]:
        cart = CartService(self.db).update_item(
            self.customer,
            self.merchant_slug,
            uuid.UUID(cart_item_id),
            CartItemUpdateRequest(quantity=quantity),
        )
        return {"status": "success", "cart": cart.model_dump(mode="json")}

    def _remove_cart_item(self, cart_item_id: str) -> dict[str, Any]:
        cart = CartService(self.db).remove_item(
            self.customer, self.merchant_slug, uuid.UUID(cart_item_id)
        )
        return {"status": "success", "cart": cart.model_dump(mode="json")}

    def _list_fulfillment_destinations(self) -> dict[str, Any]:
        addresses = list(
            self.db.scalars(
                select(CustomerAddress)
                .where(CustomerAddress.user_id == self.customer.id)
                .order_by(CustomerAddress.is_default.desc(), CustomerAddress.created_at)
            )
        )
        locations = list(
            self.db.scalars(
                select(Location)
                .where(Location.merchant_id == self.merchant_id, Location.active.is_(True))
                .order_by(Location.name)
            )
        )
        result = {
            "status": "success",
            "destinations": {
                "delivery_addresses": [
                    {
                        "id": str(address.id),
                        "label": address.label,
                        "city": address.city,
                        "region": address.region,
                        "postal_code": address.postal_code,
                        "is_default": address.is_default,
                    }
                    for address in addresses
                ],
                "pickup_locations": [
                    {
                        "id": str(location.id),
                        "name": location.name,
                        "postal_code": location.postal_code,
                        "preparation_minutes": location.preparation_minutes,
                    }
                    for location in locations
                ],
            },
        }
        self.db.rollback()
        return result

    def _prepare_checkout(
        self, fulfillment_type: str, address_id: str, location_id: str
    ) -> dict[str, Any]:
        try:
            fulfillment = FulfillmentType(fulfillment_type)
        except ValueError as error:
            raise DomainError(
                "invalid_fulfillment_type",
                "Fulfillment must be local_delivery, shipping, or pickup.",
            ) from error
        payload = CheckoutFromCartCreate(
            merchant_slug=self.merchant_slug,
            fulfillment_type=fulfillment,
            address_id=uuid.UUID(address_id) if address_id else None,
            location_id=uuid.UUID(location_id) if location_id else None,
        )
        checkout = CheckoutService(self.db).create_from_cart(
            payload, f"agent-{self.run_id}", self.customer
        )
        checkout_payload = checkout.model_dump(mode="json")
        checkout_payload.update(
            {
                "approval_required": True,
                "payment_started": False,
                "review_url": f"/checkout/review?checkout_id={checkout.id}",
            }
        )
        return {"status": "success", "checkout": checkout_payload}

    def _invoke(
        self,
        tool_name: str,
        input_payload: dict[str, Any],
        operation: Callable[[], dict[str, Any]],
    ) -> dict[str, Any]:
        call_id = self._start_call(tool_name, input_payload)
        try:
            output = operation()
        except DomainError as error:
            self.db.rollback()
            output = {
                "status": "error",
                "error": {"code": error.code, "message": error.message},
            }
            self._finish_call(call_id, tool_name, input_payload, output, error.code)
        except (ValueError, TypeError):
            self.db.rollback()
            output = {
                "status": "error",
                "error": {
                    "code": "invalid_tool_input",
                    "message": "The requested product or cart identifier is invalid.",
                },
            }
            self._finish_call(call_id, tool_name, input_payload, output, "invalid_tool_input")
        except Exception:
            self.db.rollback()
            output = {
                "status": "error",
                "error": {
                    "code": "tool_execution_failed",
                    "message": "The commerce action could not be completed safely.",
                },
            }
            self._finish_call(call_id, tool_name, input_payload, output, "tool_execution_failed")
        else:
            self.db.rollback()
            self._finish_call(call_id, tool_name, input_payload, output, None)
        self.outcomes.append({"tool": tool_name, "status": output["status"], "payload": output})
        return output

    def _start_call(self, tool_name: str, input_payload: dict[str, Any]) -> uuid.UUID:
        self.db.rollback()
        call = AgentToolCall(
            run_id=self.run_id,
            tool_name=tool_name,
            status="running",
            input_payload=input_payload,
        )
        with self.db.begin():
            self.db.add(call)
            self.db.flush()
        return call.id

    def _finish_call(
        self,
        call_id: uuid.UUID,
        tool_name: str,
        input_payload: dict[str, Any],
        output_payload: dict[str, Any],
        error_code: str | None,
    ) -> None:
        self.db.rollback()
        with self.db.begin():
            call = self.db.get(AgentToolCall, call_id)
            if call is None:
                return
            call.status = "failed" if error_code else "completed"
            call.output_payload = output_payload
            call.error_code = error_code
            call.completed_at = utc_now()
            self.db.add(
                AuditEvent(
                    merchant_id=self.merchant_id,
                    actor_type="agent",
                    actor_id=str(self.run_id),
                    event_type=f"agent.tool.{call.status}",
                    aggregate_type="agent_conversation",
                    aggregate_id=str(self.conversation_id),
                    payload={
                        "tool_call_id": str(call.id),
                        "tool_name": tool_name,
                        "input_sha256": self._digest(input_payload),
                        "output_sha256": self._digest(output_payload),
                        "error_code": error_code,
                    },
                )
            )

    @staticmethod
    def _compact_products(products: list[dict[str, Any]]) -> list[dict[str, Any]]:
        compact: list[dict[str, Any]] = []
        for product in products:
            compact.append(
                {
                    "id": product["id"],
                    "slug": product["slug"],
                    "name": product["name"],
                    "description": product["description"],
                    "product_type": product["product_type"],
                    "attributes": product["attributes"],
                    "image_url": product["image_urls"][0] if product["image_urls"] else None,
                    "variants": product["variants"],
                    "modifier_groups": product["modifier_groups"],
                }
            )
        return compact

    @staticmethod
    def _within_budget(products: list[dict[str, Any]], budget_minor: int) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for product in products:
            variants = [
                variant for variant in product["variants"] if variant["price_minor"] <= budget_minor
            ]
            if variants:
                result.append({**product, "variants": variants})
        return result

    @staticmethod
    def _digest(payload: dict[str, Any]) -> str:
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _activity_label(tool_name: str, status: str) -> str:
        labels = {
            "search_catalog": "Catalog checked",
            "recommend_products": "Recommendations grounded in catalog",
            "get_cart": "Cart read",
            "add_to_cart": "Cart updated",
            "update_cart_item": "Cart quantity updated",
            "remove_cart_item": "Cart item removed",
            "list_fulfillment_destinations": "Delivery choices checked",
            "prepare_checkout": "Exact checkout prepared",
        }
        label = labels.get(tool_name, "Commerce tool checked")
        return label if status == "success" else f"{label} — failed safely"
