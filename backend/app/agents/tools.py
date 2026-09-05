from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.policy import (
    CLARIFICATION_VERSION,
    MUTATING_TOOLS,
    SCHEDULE_DRAFT_STATE_VERSION,
    AgentActionPolicy,
    AgentIntent,
    AgentPolicyDecision,
    FulfillmentClarification,
)
from app.core.config import get_settings
from app.core.errors import DomainError
from app.db.models import (
    AgentRun,
    AgentToolCall,
    AuditEvent,
    Cart,
    CartItem,
    Checkout,
    CustomerAddress,
    Location,
    Product,
    ProductVariant,
    ScheduledPurchaseIntent,
    UserAccount,
)
from app.domain.enums import FulfillmentType, LocationKind
from app.schemas.cart import CartItemCreateRequest, CartItemUpdateRequest
from app.schemas.checkout import CheckoutFromCartCreate
from app.schemas.scheduled_purchase import ScheduledPurchaseDraftCreate
from app.services.cart import CartService
from app.services.catalog import CatalogService
from app.services.checkout import CheckoutService
from app.services.credentials_provider import CredentialsProviderService
from app.services.scheduled_purchase import ScheduledPurchaseService

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
        policy_decision: AgentPolicyDecision | None = None,
        current_message: str = "",
        pending_fulfillment: FulfillmentClarification | None = None,
    ) -> None:
        self.db = db
        self.customer = customer
        self.merchant_id = merchant_id
        self.merchant_slug = merchant_slug
        self.conversation_id = conversation_id
        self.run_id = run_id
        self.policy_decision = policy_decision or AgentActionPolicy.classify("")
        self.current_message = " ".join(current_message.strip().lower().split())[:2000]
        self.pending_fulfillment = pending_fulfillment
        self.tool_call_count = 0
        self.mutation_count = 0
        self.seen_products_by_id: dict[str, dict[str, Any]] = {}
        self.seen_variant_ids: set[str] = set()
        self.seen_cart_item_ids: set[str] = set()
        self.seen_destination_ids: set[str] = set()
        self.seen_destinations_by_id: dict[str, dict[str, Any]] = {}
        self.seen_payment_instrument_ids: set[str] = set()
        self.outcomes: list[dict[str, Any]] = []
        self.memory_activity: dict[str, Any] | None = None
        self._fulfillment_destinations_cache: dict[str, Any] | None = None

    @property
    def allowed_tool_names(self) -> frozenset[str]:
        return self.policy_decision.allowed_tools

    def commerce_tools(self) -> list[Callable[..., dict[str, Any]]]:
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

        def present_products(product_ids: list[str]) -> dict[str, Any]:
            """Select the exact catalog products shown as clickable recommendations.

            Call once after search_catalog or recommend_products with one to
            three product IDs returned in this run. Include only products you
            actually recommend and name in the final answer. This is display
            selection only and never changes commerce state.
            """
            return self.present_products(product_ids)

        def get_cart() -> dict[str, Any]:
            """Read the signed-in customer's current cart and authoritative subtotal."""
            return self.get_cart()

        def add_to_cart(
            variant_id: str, quantity: int, modifier_option_ids: list[str]
        ) -> dict[str, Any]:
            """Add an exact catalog configuration to the customer's cart.

            Call only after the customer's latest message explicitly asks to add,
            buy, or order it. Use variant and modifier IDs returned by catalog
            tools. Required modifier groups must be satisfied. If this fulfills a
            buy request for an empty cart, stop after adding; checkout preparation
            requires a later turn.
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

            Call only after the latest customer message explicitly asks to buy,
            check out, or use a fulfillment destination for the pending cart. For
            local_delivery or shipping use a saved address_id. For pickup use a
            location_id. The returned quote always requires separate customer
            approval in the trusted UI. Never call this after a cart mutation in
            the same turn.
            """
            return self.prepare_checkout(fulfillment_type, address_id, location_id)

        def list_scheduling_options() -> dict[str, Any]:
            """List saved destinations, pickup locations, and payment methods.

            Call before drafting a scheduled purchase. A listed payment method
            can be selected for the draft, but unattended execution remains
            blocked until Razorpay confirms a recurring authorization.
            """
            return self.list_scheduling_options()

        def draft_scheduled_purchase(
            items: list[dict[str, Any]],
            fulfillment_type: str,
            payment_instrument_id: str,
            first_run_at: str,
            expires_at: str,
            frequency: str,
            interval_count: int,
            max_occurrences: int,
            max_amount_minor: int,
            max_total_minor: int,
            timezone: str = "Asia/Kolkata",
            address_id: str = "",
            location_id: str = "",
        ) -> dict[str, Any]:
            """Draft a bounded scheduled purchase; never authorize or charge it.

            Call only when the latest customer message explicitly asks to
            schedule or repeat a purchase and supplies exact timing, occurrence,
            per-order, and total-budget limits. Each items entry must contain
            acceptable_variant_ids, quantity, and modifier_option_ids using IDs
            returned by catalog tools. Use IDs from list_scheduling_options.
            For pickup, pass location_id and leave address_id empty. For
            local_delivery or shipping, pass address_id and leave location_id empty.
            The trusted account UI separately displays and passkey-authorizes
            the draft.
            """
            return self.draft_scheduled_purchase(
                items=items,
                fulfillment_type=fulfillment_type,
                payment_instrument_id=payment_instrument_id,
                first_run_at=first_run_at,
                expires_at=expires_at,
                frequency=frequency,
                interval_count=interval_count,
                max_occurrences=max_occurrences,
                max_amount_minor=max_amount_minor,
                max_total_minor=max_total_minor,
                timezone=timezone,
                address_id=address_id,
                location_id=location_id,
            )

        tools = [
            search_catalog,
            recommend_products,
            present_products,
            get_cart,
            add_to_cart,
            update_cart_item,
            remove_cart_item,
            list_fulfillment_destinations,
            prepare_checkout,
            list_scheduling_options,
            draft_scheduled_purchase,
        ]
        return [tool for tool in tools if tool.__name__ in self.allowed_tool_names]

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

    def present_products(self, product_ids: list[str]) -> dict[str, Any]:
        return self._invoke(
            "present_products",
            {"product_ids": product_ids[:3]},
            lambda: self._present_products(product_ids),
        )

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
        if self._fulfillment_destinations_cache is not None:
            return self._fulfillment_destinations_cache
        result = self._invoke(
            "list_fulfillment_destinations", {}, self._list_fulfillment_destinations
        )
        if result.get("status") == "success":
            self._fulfillment_destinations_cache = result
        return result

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

    def list_scheduling_options(self) -> dict[str, Any]:
        return self._invoke("list_scheduling_options", {}, self._list_scheduling_options)

    def draft_scheduled_purchase(
        self,
        *,
        items: list[dict[str, Any]],
        fulfillment_type: str,
        payment_instrument_id: str,
        first_run_at: str,
        expires_at: str,
        frequency: str,
        interval_count: int,
        max_occurrences: int,
        max_amount_minor: int,
        max_total_minor: int,
        timezone: str = "Asia/Kolkata",
        address_id: str = "",
        location_id: str = "",
    ) -> dict[str, Any]:
        input_payload = {
            "items": items,
            "fulfillment_type": fulfillment_type,
            "payment_instrument_id": payment_instrument_id,
            "first_run_at": first_run_at,
            "expires_at": expires_at,
            "frequency": frequency,
            "interval_count": interval_count,
            "max_occurrences": max_occurrences,
            "max_amount_minor": max_amount_minor,
            "max_total_minor": max_total_minor,
            "timezone": timezone,
            "address_id": address_id,
            "location_id": location_id,
        }
        return self._invoke(
            "draft_scheduled_purchase",
            input_payload,
            lambda: self._draft_scheduled_purchase(input_payload),
        )

    def structured_content(self, assistant_text: str = "") -> dict[str, Any]:
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
        product_candidates: dict[str, dict[str, Any]] = {}
        catalog_candidates: dict[str, dict[str, Any]] = {}
        for outcome in self.outcomes:
            payload = outcome["payload"]
            if outcome["status"] == "success" and outcome["tool"] in {
                "search_catalog",
                "recommend_products",
                "present_products",
            }:
                for product in payload.get("products", []):
                    catalog_candidates[str(product["id"])] = product
            if outcome["tool"] == "present_products" and outcome["status"] == "success":
                for product in payload.get("products", []):
                    product_candidates[str(product["id"])] = product
            if "cart" in payload:
                content["cart"] = payload["cart"]
            if "checkout" in payload:
                content["checkout"] = payload["checkout"]
            if "scheduled_purchase" in payload:
                content["scheduled_purchase"] = payload["scheduled_purchase"]
            if "destinations" in payload:
                content["destinations"] = payload["destinations"]
            if "scheduling_options" in payload:
                content["scheduling_options"] = payload["scheduling_options"]
        referenced_products = self._products_referenced_in_text(
            list(product_candidates.values()), assistant_text
        )
        if referenced_products:
            content["products"] = referenced_products
        configuration = self._configuration_for_response(
            list(catalog_candidates.values()), assistant_text
        )
        if configuration is not None:
            content["product_configuration"] = configuration
        schedule_configuration = self._schedule_configuration_for_response()
        if schedule_configuration is not None:
            content["schedule_configuration"] = schedule_configuration
        clarification = self._fulfillment_clarification_for_response()
        if clarification is not None:
            content["clarification"] = clarification
            content["suggestions"] = [option["label"] for option in clarification["options"]]
        if self.memory_activity is not None:
            content["memory"] = self.memory_activity
        if (
            self.policy_decision.intent == AgentIntent.SCHEDULE_DRAFT
            and "scheduled_purchase" not in content
        ):
            content["schedule_draft_pending"] = {
                "version": SCHEDULE_DRAFT_STATE_VERSION,
            }
        return content

    def _fulfillment_clarification_for_response(self) -> dict[str, Any] | None:
        if self.policy_decision.intent not in {
            AgentIntent.CHECKOUT_PREPARE,
            AgentIntent.FULFILLMENT_SELECT,
            AgentIntent.SCHEDULE_DRAFT,
        }:
            return None
        if any(
            outcome["tool"] in {"prepare_checkout", "draft_scheduled_purchase"}
            and outcome["status"] == "success"
            for outcome in self.outcomes
        ) or any(
            outcome["tool"] == "draft_scheduled_purchase"
            for outcome in self.outcomes
        ):
            return None
        destinations = next(
            (
                outcome["payload"].get(
                    "destinations",
                    outcome["payload"].get("scheduling_options", {}),
                )
                for outcome in reversed(self.outcomes)
                if outcome["tool"] in {"list_fulfillment_destinations", "list_scheduling_options"}
                and outcome["status"] == "success"
            ),
            {},
        )
        addresses = destinations.get("delivery_addresses", [])
        locations = destinations.get("pickup_locations", [])
        if not addresses and not locations:
            return None
        delivery_unserviceable = any(
            outcome["tool"] == "prepare_checkout"
            and outcome["status"] == "error"
            and outcome["payload"].get("error", {}).get("code") == "address_not_serviceable"
            for outcome in self.outcomes
        )
        purpose = (
            "schedule_draft"
            if self.policy_decision.intent == AgentIntent.SCHEDULE_DRAFT
            else "checkout"
        )
        if delivery_unserviceable:
            return self._pickup_location_clarification(locations, purpose)
        if re.search(
            r"\b(?:local\s+delivery|delivery|deliver|address)\b",
            self.current_message,
        ):
            return self._delivery_address_clarification(addresses, purpose)
        if re.search(r"\b(?:pickup|pick(?:\s+it)?\s+up)\b", self.current_message):
            return self._pickup_location_clarification(locations, purpose)
        if (
            purpose == "schedule_draft"
            and self.policy_decision.fulfillment_selection is None
            and self.pending_fulfillment is not None
            and self.pending_fulfillment.purpose == "schedule_draft"
        ):
            if self.pending_fulfillment.kind == "delivery_address":
                return self._delivery_address_clarification(addresses, purpose)
            if self.pending_fulfillment.kind == "pickup_location":
                return self._pickup_location_clarification(locations, purpose)
        return self._fulfillment_method_clarification(addresses, locations, purpose)

    @staticmethod
    def _delivery_address_clarification(
        addresses: list[dict[str, Any]],
        purpose: str = "checkout",
    ) -> dict[str, Any] | None:
        options = [
            {
                "destination_id": str(address["id"]),
                "label": f"Use {address['label']} address",
                "fulfillment_type": "local_delivery",
            }
            for address in addresses
            if address.get("id") and address.get("label")
        ]
        if not options:
            return None
        default_address = next(
            (address for address in addresses if address.get("is_default")),
            addresses[0],
        )
        return {
            "version": CLARIFICATION_VERSION,
            "kind": "delivery_address",
            "purpose": purpose,
            "options": options,
            "default_destination_id": str(default_address["id"]),
        }

    @staticmethod
    def _pickup_location_clarification(
        locations: list[dict[str, Any]],
        purpose: str = "checkout",
    ) -> dict[str, Any] | None:
        options = [
            {
                "destination_id": str(location["id"]),
                "label": str(location["name"]),
                "fulfillment_type": "pickup",
            }
            for location in locations
            if location.get("id") and location.get("name")
        ]
        if not options:
            return None
        default_location = next(
            (location for location in locations if location.get("is_default")),
            locations[0],
        )
        return {
            "version": CLARIFICATION_VERSION,
            "kind": "pickup_location",
            "purpose": purpose,
            "options": options,
            "default_destination_id": str(default_location["id"]),
        }

    @staticmethod
    def _fulfillment_method_clarification(
        addresses: list[dict[str, Any]],
        locations: list[dict[str, Any]],
        purpose: str = "checkout",
    ) -> dict[str, Any] | None:
        options: list[dict[str, str]] = []
        if addresses:
            default_address = next(
                (address for address in addresses if address.get("is_default")),
                addresses[0],
            )
            options.append(
                {
                    "destination_id": str(default_address["id"]),
                    "label": "Local delivery",
                    "fulfillment_type": "local_delivery",
                }
            )
        if locations:
            default_location = next(
                (location for location in locations if location.get("is_default")),
                locations[0],
            )
            options.append(
                {
                    "destination_id": str(default_location["id"]),
                    "label": "Pickup",
                    "fulfillment_type": "pickup",
                }
            )
        if not options:
            return None
        return {
            "version": CLARIFICATION_VERSION,
            "kind": "fulfillment_method",
            "purpose": purpose,
            "options": options,
            "default_destination_id": None,
        }

    def _configuration_for_response(
        self, products: list[dict[str, Any]], assistant_text: str
    ) -> dict[str, Any] | None:
        if self.policy_decision.intent not in {
            AgentIntent.CART_ADD,
            AgentIntent.CHECKOUT_PREPARE,
            AgentIntent.SCHEDULE_DRAFT,
        }:
            return None
        if any(
            outcome["tool"] in MUTATING_TOOLS and outcome["status"] == "success"
            for outcome in self.outcomes
        ) or any(
            outcome["tool"] == "draft_scheduled_purchase"
            for outcome in self.outcomes
        ):
            return None
        referenced = self._products_referenced_in_text(products, self.current_message)
        if not referenced:
            referenced = self._products_referenced_in_text(products, assistant_text)
        configurable = [
            product
            for product in referenced
            if product.get("variants")
            and (len(product["variants"]) > 1 or product.get("modifier_groups"))
        ]
        if len(configurable) != 1:
            return None
        product = configurable[0]
        return {
            "purpose": (
                "schedule_draft"
                if self.policy_decision.intent == AgentIntent.SCHEDULE_DRAFT
                else "cart_add"
            ),
            "product_id": product["id"],
            "product_name": product["name"],
            "product_slug": product["slug"],
            "quantity": 1,
            "variants": product["variants"],
            "modifier_groups": product.get("modifier_groups", []),
        }

    def _schedule_configuration_for_response(self) -> dict[str, Any] | None:
        if self.policy_decision.intent != AgentIntent.SCHEDULE_DRAFT:
            return None
        if any(
            outcome["tool"] == "draft_scheduled_purchase"
            for outcome in self.outcomes
        ):
            return None
        schedule_bounds_present = all(
            re.search(pattern, self.current_message)
            for pattern in (
                r"\bfirst\s+run\b",
                r"\bexpires?\b",
                r"\bmaximum\s+\d+\s+(?:occurrences?|runs?)\b",
                r"\bper-order\s+cap\b",
                r"\btotal\s+cap\b",
            )
        )
        if schedule_bounds_present:
            return None
        scheduling_options = next(
            (
                outcome["payload"].get("scheduling_options", {})
                for outcome in reversed(self.outcomes)
                if outcome["tool"] == "list_scheduling_options" and outcome["status"] == "success"
            ),
            {},
        )
        if not scheduling_options:
            return None
        frequency_match = re.search(
            r"\b(?:once|daily|weekly|monthly)\b",
            self.current_message,
        )
        return {
            "version": "agent-schedule-configuration-1",
            "currency": "INR",
            "frequency": frequency_match.group(0) if frequency_match else None,
            "frequency_options": ["once", "daily", "weekly", "monthly"],
            "minimum_notification_lead_hours": scheduling_options.get(
                "minimum_notification_lead_hours"
            ),
            "earliest_first_run_at": scheduling_options.get("earliest_first_run_at"),
            "recurring_provider_enabled": bool(
                scheduling_options.get("recurring_provider_enabled")
            ),
            "draft_available": bool(scheduling_options.get("draft_available")),
            "availability_code": scheduling_options.get("availability_code"),
            "availability_message": scheduling_options.get("availability_message"),
        }

    @staticmethod
    def _products_referenced_in_text(
        products: list[dict[str, Any]], assistant_text: str
    ) -> list[dict[str, Any]]:
        normalized_text = f" {' '.join(_TOKEN_PATTERN.findall(assistant_text.casefold()))} "
        referenced: list[tuple[int, dict[str, Any]]] = []
        for product in products:
            normalized_name = " ".join(
                _TOKEN_PATTERN.findall(str(product.get("name", "")).casefold())
            )
            if not normalized_name:
                continue
            position = normalized_text.find(f" {normalized_name} ")
            if position >= 0:
                referenced.append((position, product))
        referenced.sort(key=lambda item: item[0])
        return [product for _, product in referenced]

    def record_denied_tool_call(
        self, tool_name: str, input_payload: dict[str, Any]
    ) -> dict[str, Any]:
        return self._invoke(
            tool_name,
            input_payload,
            lambda: {
                "status": "error",
                "error": {"code": "agent_tool_denied", "message": "Tool denied."},
            },
        )

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

    def _present_products(self, product_ids: list[str]) -> dict[str, Any]:
        if not product_ids or len(product_ids) > 3 or len(set(product_ids)) != len(product_ids):
            raise DomainError(
                "invalid_product_presentation",
                "Choose between one and three distinct products to present.",
                422,
            )
        missing = [
            product_id for product_id in product_ids if product_id not in self.seen_products_by_id
        ]
        if missing:
            raise DomainError(
                "product_presentation_not_verified",
                "Only products returned by a catalog tool in this run can be presented.",
                409,
            )
        return {
            "status": "success",
            "products": [self.seen_products_by_id[product_id] for product_id in product_ids],
        }

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
        default_pickup_id = next(
            (location.id for location in locations if location.kind == LocationKind.ROASTERY),
            locations[0].id if locations else None,
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
                        "is_default": location.id == default_pickup_id,
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
            payload,
            f"agent-{self.run_id}",
            self.customer,
            source="agent",
            agent_conversation_id=self.conversation_id,
            agent_run_id=self.run_id,
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

    def _list_scheduling_options(self) -> dict[str, Any]:
        destinations = self._list_fulfillment_destinations()["destinations"]
        instruments = CredentialsProviderService(self.db).list_instruments(self.customer)
        settings = get_settings()
        lead_hours = settings.razorpay_recurring_notification_lead_hours
        instrument_payloads = instruments.model_dump(mode="json")["payment_instruments"]
        recurring_instrument_available = any(
            instrument.get("instrument_type") == "com.razorpay.upi.autopay"
            for instrument in instrument_payloads
        )
        availability_code = None
        availability_message = None
        if not recurring_instrument_available:
            availability_code = (
                "recurring_instrument_unavailable"
                if settings.razorpay_recurring_enabled
                else "recurring_provider_disabled"
            )
            availability_message = (
                "No eligible recurring payment instrument is available."
                if settings.razorpay_recurring_enabled
                else (
                    "Recurring scheduling is disabled for this environment. "
                    "A merchant operator must enable it and restart the backend."
                )
            )
        return {
            "status": "success",
            "scheduling_options": {
                **destinations,
                "payment_instruments": instrument_payloads,
                "minimum_notification_lead_hours": lead_hours,
                "earliest_first_run_at": (
                    utc_now() + timedelta(hours=lead_hours, minutes=1)
                ).isoformat(),
                "recurring_provider_enabled": settings.razorpay_recurring_enabled,
                "draft_available": recurring_instrument_available,
                "availability_code": availability_code,
                "availability_message": availability_message,
                "authorization_note": (
                    "The draft still needs passkey approval. Unattended payment also needs "
                    "a confirmed Razorpay recurring authorization and advance pre-debit "
                    "notification."
                ),
            },
        }

    def _draft_scheduled_purchase(self, input_payload: dict[str, Any]) -> dict[str, Any]:
        fulfillment_type = str(input_payload.get("fulfillment_type", "")).strip().lower()
        raw_address_id = input_payload.get("address_id")
        raw_location_id = input_payload.get("location_id")
        address_id = raw_address_id if raw_address_id else None
        location_id = raw_location_id if raw_location_id else None
        if fulfillment_type == "pickup":
            address_id = None
        elif fulfillment_type in {"local_delivery", "shipping"}:
            location_id = None

        payload = ScheduledPurchaseDraftCreate.model_validate(
            {
                "merchant_slug": self.merchant_slug,
                "currency": "INR",
                **input_payload,
                "address_id": address_id,
                "location_id": location_id,
            }
        )
        schedule = ScheduledPurchaseService(self.db).create_draft(
            payload,
            self.customer,
            idempotency_key=f"agent-schedule-{self.run_id}",
        )
        schedule_payload = schedule.model_dump(mode="json")
        schedule_payload.update(
            {
                "authorization_required": True,
                "payment_started": False,
                "review_url": "/account#scheduled-purchases",
            }
        )
        return {"status": "success", "scheduled_purchase": schedule_payload}

    def _invoke(
        self,
        tool_name: str,
        input_payload: dict[str, Any],
        operation: Callable[[], dict[str, Any]],
    ) -> dict[str, Any]:
        authorization = AgentActionPolicy.authorize_tool(
            self.policy_decision,
            tool_name,
            tool_call_count=self.tool_call_count,
            mutation_count=self.mutation_count,
        )
        self.tool_call_count += 1
        call_id = self._start_call(tool_name, input_payload)
        if not authorization.allowed:
            output = {
                "status": "error",
                "error": {
                    "code": "agent_tool_denied",
                    "message": "That commerce action is not allowed for this request.",
                    "reason": authorization.reason_code,
                },
            }
            self._finish_call(
                call_id,
                tool_name,
                input_payload,
                output,
                "agent_tool_denied",
                policy_reason=authorization.reason_code,
            )
            self.outcomes.append({"tool": tool_name, "status": "error", "payload": output})
            return output
        try:
            self._validate_action_scope(tool_name, input_payload)
        except DomainError as error:
            self.db.rollback()
            output = {
                "status": "error",
                "error": {
                    "code": error.code,
                    "message": error.message,
                    "reason": error.code,
                },
            }
            self._finish_call(
                call_id,
                tool_name,
                input_payload,
                output,
                error.code,
                policy_reason=error.code,
            )
            self.outcomes.append({"tool": tool_name, "status": "error", "payload": output})
            return output
        self.db.rollback()
        try:
            output = operation()
            output = self._verify_result(tool_name, output)
        except DomainError as error:
            self.db.rollback()
            output = {
                "status": "error",
                "error": {"code": error.code, "message": error.message},
            }
            self._finish_call(call_id, tool_name, input_payload, output, error.code)
        except (ValueError, TypeError) as error:
            self.db.rollback()
            message = str(error).strip() or "The requested product or cart identifier is invalid."
            output = {
                "status": "error",
                "error": {
                    "code": "invalid_tool_input",
                    "message": message,
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
            if tool_name in MUTATING_TOOLS:
                self.mutation_count += 1
            self._record_seen_resources(output)
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
        policy_reason: str = "policy_allowed",
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
            run = self.db.get(AgentRun, self.run_id)
            if run is not None:
                run.tool_call_count = self.tool_call_count
                run.mutation_count = self.mutation_count
                verification = output_payload.get("verification", {})
                run.checkpoint_state = {
                    "version": "agent-checkpoint-1",
                    "phase": (
                        "mutation_verified"
                        if verification.get("status") == "verified"
                        else "tool_denied"
                        if error_code == "agent_tool_denied"
                        else "tool_completed"
                        if error_code is None
                        else "tool_failed"
                    ),
                    "last_tool_call_id": str(call.id),
                    "last_tool": tool_name,
                    "verification_status": verification.get("status"),
                    "updated_at": utc_now().isoformat(),
                }
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
                        "policy_version": self.policy_decision.policy_version,
                        "policy_reason": policy_reason,
                        "intent": self.policy_decision.intent.value,
                        "risk_level": self.policy_decision.risk_level.value,
                    },
                )
            )

    def _verify_result(self, tool_name: str, output: dict[str, Any]) -> dict[str, Any]:
        if output.get("status") != "success" or tool_name not in MUTATING_TOOLS:
            return output

        if tool_name in {"add_to_cart", "update_cart_item", "remove_cart_item"}:
            authoritative = self._get_cart()
            return {
                **authoritative,
                "verification": {
                    "status": "verified",
                    "source": "commerce_database",
                    "tool": tool_name,
                },
            }

        if tool_name == "prepare_checkout":
            checkout_payload = output.get("checkout", {})
            checkout_id = uuid.UUID(str(checkout_payload.get("id")))
            checkout = self.db.scalar(
                select(Checkout).where(
                    Checkout.id == checkout_id,
                    Checkout.customer_id == self.customer.id,
                    Checkout.merchant_id == self.merchant_id,
                    Checkout.agent_run_id == self.run_id,
                )
            )
            if checkout is None or (
                checkout.total_minor != checkout_payload.get("total_minor")
                or checkout.currency != checkout_payload.get("currency")
                or checkout.quote_version != checkout_payload.get("quote_version")
            ):
                raise DomainError(
                    "agent_verification_failed",
                    "The prepared checkout could not be verified against current commerce state.",
                    409,
                )

        if tool_name == "draft_scheduled_purchase":
            schedule_payload = output.get("scheduled_purchase", {})
            schedule_id = uuid.UUID(str(schedule_payload.get("id")))
            schedule = self.db.scalar(
                select(ScheduledPurchaseIntent).where(
                    ScheduledPurchaseIntent.id == schedule_id,
                    ScheduledPurchaseIntent.customer_id == self.customer.id,
                    ScheduledPurchaseIntent.merchant_id == self.merchant_id,
                )
            )
            if schedule is None or schedule.status.value != "draft":
                raise DomainError(
                    "agent_verification_failed",
                    "The scheduled purchase draft could not be verified.",
                    409,
                )

        return {
            **output,
            "verification": {
                "status": "verified",
                "source": "commerce_database",
                "tool": tool_name,
            },
        }

    def _validate_action_scope(self, tool_name: str, payload: dict[str, Any]) -> None:
        if tool_name == "add_to_cart":
            variant_id = str(payload.get("variant_id", ""))
            variant = self.db.scalar(
                select(ProductVariant)
                .join(Product, ProductVariant.product_id == Product.id)
                .where(
                    ProductVariant.id == self._uuid(variant_id),
                    ProductVariant.merchant_id == self.merchant_id,
                    Product.merchant_id == self.merchant_id,
                )
            )
            if variant is None:
                raise DomainError("variant_not_found", "Product variant is unavailable.", 404)
            if not self._message_names_resource(variant.product.name, variant.name) and not (
                self._has_deictic_reference() and self.seen_variant_ids == {variant_id}
            ):
                raise DomainError(
                    "agent_action_ambiguous",
                    "Name the exact product and size before I add it to the cart.",
                    409,
                )

        if tool_name in {"update_cart_item", "remove_cart_item"}:
            item_id = str(payload.get("cart_item_id", ""))
            row = self.db.execute(
                select(CartItem, Product, ProductVariant)
                .join(Cart, CartItem.cart_id == Cart.id)
                .join(ProductVariant, CartItem.variant_id == ProductVariant.id)
                .join(Product, ProductVariant.product_id == Product.id)
                .where(
                    CartItem.id == self._uuid(item_id),
                    Cart.user_id == self.customer.id,
                    Cart.merchant_id == self.merchant_id,
                )
            ).one_or_none()
            if row is None:
                raise DomainError("cart_item_not_found", "Cart item was not found.", 404)
            if not self._message_names_resource(row.Product.name, row.ProductVariant.name) and not (
                self._has_deictic_reference() and item_id in self.seen_cart_item_ids
            ):
                raise DomainError(
                    "agent_action_ambiguous",
                    "Name the exact cart item before changing it.",
                    409,
                )

        if tool_name == "prepare_checkout":
            destination_id = str(payload.get("address_id") or payload.get("location_id") or "")
            if not destination_id or destination_id not in self.seen_destination_ids:
                raise DomainError(
                    "agent_destination_not_verified",
                    "I must read your current fulfillment choices before preparing checkout.",
                    409,
                )
            selection = self.policy_decision.fulfillment_selection
            destination = self.seen_destinations_by_id.get(destination_id, {})
            if selection is None and not self._message_authorizes_destination(
                payload,
                destination,
            ):
                raise DomainError(
                    "agent_fulfillment_selection_required",
                    "Choose a delivery address or pickup location before I prepare checkout.",
                    409,
                )
            if selection is not None:
                selected_field = (
                    str(payload.get("location_id") or "")
                    if selection.fulfillment_type == "pickup"
                    else str(payload.get("address_id") or "")
                )
                if (
                    payload.get("fulfillment_type") != selection.fulfillment_type
                    or selected_field != selection.destination_id
                ):
                    raise DomainError(
                        "agent_context_destination_mismatch",
                        "The checkout destination did not match your selected option.",
                        409,
                    )

        if tool_name == "draft_scheduled_purchase":
            destination_id = str(payload.get("address_id") or payload.get("location_id") or "")
            payment_id = str(payload.get("payment_instrument_id") or "")
            if (
                not destination_id
                or destination_id not in self.seen_destination_ids
                or payment_id not in self.seen_payment_instrument_ids
            ):
                raise DomainError(
                    "agent_schedule_options_not_verified",
                    (
                        "I must read current destinations and payment options before "
                        "drafting a schedule."
                    ),
                    409,
                )
            selection = self.policy_decision.fulfillment_selection
            if selection is not None and selection.purpose == "schedule_draft":
                selected_field = (
                    str(payload.get("location_id") or "")
                    if selection.fulfillment_type == "pickup"
                    else str(payload.get("address_id") or "")
                )
                if (
                    payload.get("fulfillment_type") != selection.fulfillment_type
                    or selected_field != selection.destination_id
                ):
                    raise DomainError(
                        "agent_schedule_destination_mismatch",
                        "The schedule destination did not match your selected option.",
                        409,
                    )

    def _record_seen_resources(self, output: dict[str, Any]) -> None:
        for product in output.get("products", []):
            if product.get("id"):
                self.seen_products_by_id[str(product["id"])] = product
            for variant in product.get("variants", []):
                if variant.get("id"):
                    self.seen_variant_ids.add(str(variant["id"]))
        for item in output.get("cart", {}).get("items", []):
            if item.get("id"):
                self.seen_cart_item_ids.add(str(item["id"]))
        for container in (
            output.get("destinations", {}),
            output.get("scheduling_options", {}),
        ):
            for key in ("delivery_addresses", "pickup_locations"):
                for item in container.get(key, []):
                    if item.get("id"):
                        destination_id = str(item["id"])
                        self.seen_destination_ids.add(destination_id)
                        self.seen_destinations_by_id[destination_id] = {
                            **item,
                            "resource_kind": (
                                "delivery_address"
                                if key == "delivery_addresses"
                                else "pickup_location"
                            ),
                        }
            for item in container.get("payment_instruments", []):
                if item.get("id"):
                    self.seen_payment_instrument_ids.add(str(item["id"]))

    def _message_names_resource(self, *names: str) -> bool:
        ignored = {"one", "the", "and", "with", "regular", "small", "medium", "large"}
        resource_tokens = {
            token
            for name in names
            for token in _TOKEN_PATTERN.findall(name.lower())
            if len(token) >= 3 and token not in ignored
        }
        message_tokens = set(_TOKEN_PATTERN.findall(self.current_message))
        return bool(resource_tokens & message_tokens)

    def _has_deictic_reference(self) -> bool:
        return bool(re.search(r"\b(?:this|that|it|one|those|these)\b", self.current_message))

    def _message_authorizes_destination(
        self,
        payload: dict[str, Any],
        destination: dict[str, Any],
    ) -> bool:
        fulfillment_type = str(payload.get("fulfillment_type", ""))
        if fulfillment_type == "pickup":
            if destination.get("resource_kind") != "pickup_location":
                return False
            if self._message_names_resource(str(destination.get("name", ""))):
                return True
            requests_pickup = bool(
                re.search(r"\b(?:pickup|pick(?:\s+it)?\s+up)\b", self.current_message)
            )
            return requests_pickup and bool(destination.get("is_default"))

        if destination.get("resource_kind") != "delivery_address":
            return False
        if self._message_names_resource(str(destination.get("label", ""))):
            return True
        requests_delivery = bool(
            re.search(
                r"\b(?:local\s+delivery|deliver|ship|default\s+address|"
                r"registered\s+address|saved\s+address)\b",
                self.current_message,
            )
        )
        return requests_delivery and bool(destination.get("is_default"))

    @staticmethod
    def _uuid(value: str) -> uuid.UUID:
        try:
            return uuid.UUID(value)
        except (TypeError, ValueError) as error:
            raise DomainError(
                "invalid_tool_input", "The requested resource identifier is invalid.", 422
            ) from error

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
            "present_products": "Recommendation cards selected",
            "get_cart": "Cart read",
            "add_to_cart": "Cart updated",
            "update_cart_item": "Cart quantity updated",
            "remove_cart_item": "Cart item removed",
            "list_fulfillment_destinations": "Delivery choices checked",
            "prepare_checkout": "Exact checkout prepared",
            "list_scheduling_options": "Schedule boundaries checked",
            "draft_scheduled_purchase": "Bounded schedule drafted",
        }
        label = labels.get(tool_name, "Commerce tool checked")
        return label if status == "success" else f"{label} — failed safely"
