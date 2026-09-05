import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.agents.guardrails import AgentOutputGuard
from app.agents.policy import (
    AgentActionPolicy,
    AgentIntent,
    AgentRiskLevel,
    FulfillmentClarification,
    FulfillmentOption,
)
from app.agents.runtime import AgentRuntimeResult, get_agent_runtime
from app.agents.tools import AgentToolbox
from app.core.config import get_settings
from app.core.errors import DomainError
from app.db.models import (
    AgentConversation,
    AgentMessage,
    AgentRun,
    AgentToolCall,
    AuditEvent,
    Checkout,
    CustomerAddress,
    Location,
    Merchant,
    PaymentInstrument,
    ScheduledPurchaseIntent,
)
from app.domain.enums import LocationKind
from app.main import app


class ActionRuntime:
    model_name = "security-test-model"

    def __init__(
        self,
        action: Callable[[AgentToolbox], None] | None = None,
        text: str = "Safe test response.",
    ) -> None:
        self.action = action
        self.text = text
        self.calls = 0

    async def run(self, *, toolbox: AgentToolbox, **_: Any) -> AgentRuntimeResult:
        self.calls += 1
        if self.action:
            self.action(toolbox)
        return AgentRuntimeResult(text=self.text, model=self.model_name)


def headers(seeded: dict[str, object], key: str | None = None) -> dict[str, str]:
    result = {"Authorization": f"Bearer {seeded['customer_token']}"}
    if key:
        result["Idempotency-Key"] = key
    return result


async def conversation(
    client: httpx.AsyncClient, seeded: dict[str, object], runtime: ActionRuntime
) -> dict[str, Any]:
    app.dependency_overrides[get_agent_runtime] = lambda: runtime
    response = await client.post(
        "/api/v1/agent/conversations/current",
        headers=headers(seeded),
        json={"merchant_slug": "ember-and-leaf"},
    )
    assert response.status_code == 200
    return response.json()


@pytest.mark.parametrize(
    ("message", "intent", "risk", "included", "excluded"),
    [
        (
            "Recommend a floral tea.",
            AgentIntent.DISCOVER,
            AgentRiskLevel.LOW,
            "recommend_products",
            "add_to_cart",
        ),
        (
            "Add one of those to my cart.",
            AgentIntent.CART_ADD,
            AgentRiskLevel.MEDIUM,
            "add_to_cart",
            "remove_cart_item",
        ),
        (
            "Remove this item from my cart.",
            AgentIntent.CART_REMOVE,
            AgentRiskLevel.MEDIUM,
            "remove_cart_item",
            "prepare_checkout",
        ),
        (
            "Buy what is in my cart and check out.",
            AgentIntent.CHECKOUT_PREPARE,
            AgentRiskLevel.HIGH,
            "prepare_checkout",
            "remove_cart_item",
        ),
        (
            "Schedule this weekly for three weeks.",
            AgentIntent.SCHEDULE_DRAFT,
            AgentRiskLevel.HIGH,
            "draft_scheduled_purchase",
            "update_cart_item",
        ),
    ],
)
def test_policy_assigns_only_phase_tools(
    message: str,
    intent: AgentIntent,
    risk: AgentRiskLevel,
    included: str,
    excluded: str,
) -> None:
    decision = AgentActionPolicy.classify(message)
    assert decision.intent == intent
    assert decision.risk_level == risk
    assert included in decision.allowed_tools
    assert excluded not in decision.allowed_tools


def test_financial_request_has_no_write_or_payment_capability() -> None:
    decision = AgentActionPolicy.classify("Approve and pay for my cart now with Razorpay.")
    assert decision.risk_level == AgentRiskLevel.CRITICAL
    assert decision.financial_action_requested is True
    assert decision.allowed_tools == {
        "search_catalog",
        "recommend_products",
        "present_products",
        "get_cart",
        "list_fulfillment_destinations",
    }


@pytest.mark.parametrize(
    ("message", "forbidden_tool"),
    [
        ("Should I buy the cold brew?", "prepare_checkout"),
        ("Okay, should I buy the cold brew?", "prepare_checkout"),
        ("Do you offer weekly subscriptions?", "draft_scheduled_purchase"),
        ("How do I remove an item?", "remove_cart_item"),
        ("Can I add a cold brew?", "add_to_cart"),
        ("Sure, can I add a cold brew?", "add_to_cart"),
        ("Compare and add up the cold brew prices.", "add_to_cart"),
    ],
)
def test_advice_questions_do_not_grant_mutation_capabilities(
    message: str, forbidden_tool: str
) -> None:
    decision = AgentActionPolicy.classify(message)
    assert forbidden_tool not in decision.allowed_tools
    assert decision.risk_level == AgentRiskLevel.LOW


@pytest.mark.parametrize(
    "message",
    [
        "Add the regular cold brew.",
        "Please add the regular cold brew.",
        "Can you add the regular cold brew?",
        "I would like you to add the regular cold brew.",
    ],
)
def test_explicit_add_requests_grant_only_the_add_capability(message: str) -> None:
    decision = AgentActionPolicy.classify(message)
    assert decision.intent == AgentIntent.CART_ADD
    assert "add_to_cart" in decision.allowed_tools
    assert "prepare_checkout" not in decision.allowed_tools


@pytest.mark.parametrize(
    ("message", "intent"),
    [
        ("Yes, buy it.", AgentIntent.CHECKOUT_PREPARE),
        ("Yes buy it, do all things you want for it.", AgentIntent.CHECKOUT_PREPARE),
        ("Okay buy the House Cold Brew.", AgentIntent.CHECKOUT_PREPARE),
        ("Sure, add it to my cart.", AgentIntent.CART_ADD),
        ("Yeah, remove it from my cart.", AgentIntent.CART_REMOVE),
        ("Go ahead and check out.", AgentIntent.CHECKOUT_PREPARE),
    ],
)
def test_conversational_affirmations_preserve_explicit_action_intent(
    message: str, intent: AgentIntent
) -> None:
    decision = AgentActionPolicy.classify(message)
    assert decision.intent == intent


@pytest.mark.parametrize(
    ("message", "intent", "can_prepare_checkout"),
    [
        ("Use my registered address.", AgentIntent.FULFILLMENT_SELECT, True),
        ("Check my default address.", AgentIntent.FULFILLMENT_READ, False),
        ("Show my saved addresses.", AgentIntent.FULFILLMENT_READ, False),
    ],
)
def test_saved_address_requests_get_scoped_fulfillment_capabilities(
    message: str, intent: AgentIntent, can_prepare_checkout: bool
) -> None:
    decision = AgentActionPolicy.classify(message)
    assert decision.intent == intent
    assert "list_fulfillment_destinations" in decision.allowed_tools
    assert "add_to_cart" not in decision.allowed_tools
    assert ("prepare_checkout" in decision.allowed_tools) is can_prepare_checkout


def test_fulfillment_reply_only_inherits_checkout_capability_from_pending_state() -> None:
    central_id = str(uuid.uuid4())
    indiranagar_id = str(uuid.uuid4())
    clarification = FulfillmentClarification(
        options=(
            FulfillmentOption(central_id, "Central Roastery", "pickup"),
            FulfillmentOption(indiranagar_id, "Indiranagar Café", "pickup"),
        ),
        default_destination_id=central_id,
    )

    without_context = AgentActionPolicy.classify("Central Roastery")
    affirmative_without_context = AgentActionPolicy.classify("yes")
    selected = AgentActionPolicy.classify("Indiranagar Cafe", pending_fulfillment=clarification)
    defaulted = AgentActionPolicy.classify("yes", pending_fulfillment=clarification)
    conversational_default = AgentActionPolicy.classify(
        "yes i will pick it up", pending_fulfillment=clarification
    )
    rejected_default = AgentActionPolicy.classify(
        "no, do not pick it up", pending_fulfillment=clarification
    )
    rejected_location = AgentActionPolicy.classify(
        "not Central Roastery", pending_fulfillment=clarification
    )

    assert without_context.intent == AgentIntent.ANSWER
    assert "prepare_checkout" not in without_context.allowed_tools
    assert "prepare_checkout" not in affirmative_without_context.allowed_tools
    assert selected.intent == AgentIntent.FULFILLMENT_SELECT
    assert selected.fulfillment_selection is not None
    assert selected.fulfillment_selection.destination_id == indiranagar_id
    assert selected.fulfillment_selection.resolution == "matched_option"
    assert defaulted.fulfillment_selection is not None
    assert defaulted.fulfillment_selection.destination_id == central_id
    assert defaulted.fulfillment_selection.resolution == "defaulted_pickup"
    assert conversational_default.fulfillment_selection is not None
    assert conversational_default.fulfillment_selection.destination_id == central_id
    assert rejected_default.fulfillment_selection is None
    assert rejected_location.fulfillment_selection is None


def test_schedule_fulfillment_reply_continues_schedule_without_checkout_intent() -> None:
    central_id = str(uuid.uuid4())
    clarification = FulfillmentClarification(
        options=(FulfillmentOption(central_id, "Central Roastery", "pickup"),),
        default_destination_id=central_id,
        purpose="schedule_draft",
    )

    decision = AgentActionPolicy.classify(
        "Central Roastery",
        pending_fulfillment=clarification,
    )

    assert decision.intent == AgentIntent.SCHEDULE_DRAFT
    assert decision.fulfillment_selection is not None
    assert decision.fulfillment_selection.purpose == "schedule_draft"
    assert "draft_scheduled_purchase" in decision.allowed_tools
    assert "prepare_checkout" not in decision.allowed_tools


@pytest.mark.parametrize(
    "reply",
    [
        "7, 100000 rupee",
        "small, dairy, normal sweet",
        "yes",
        "start next Monday and end after 30 days",
    ],
)
def test_schedule_details_continue_only_with_server_pending_state(reply: str) -> None:
    without_context = AgentActionPolicy.classify(reply)
    with_context = AgentActionPolicy.classify(reply, pending_schedule_draft=True)

    assert "draft_scheduled_purchase" not in without_context.allowed_tools
    assert with_context.intent == AgentIntent.SCHEDULE_DRAFT
    assert "list_scheduling_options" in with_context.allowed_tools
    assert "draft_scheduled_purchase" in with_context.allowed_tools


def test_schedule_pending_state_can_be_cancelled() -> None:
    decision = AgentActionPolicy.classify(
        "never mind, cancel the schedule",
        pending_schedule_draft=True,
    )

    assert decision.intent == AgentIntent.ANSWER
    assert "draft_scheduled_purchase" not in decision.allowed_tools


def test_financial_request_cannot_use_pending_fulfillment_state_to_pay() -> None:
    central_id = str(uuid.uuid4())
    clarification = FulfillmentClarification(
        options=(FulfillmentOption(central_id, "Central Roastery", "pickup"),),
        default_destination_id=central_id,
    )

    decision = AgentActionPolicy.classify(
        "Central Roastery and pay now", pending_fulfillment=clarification
    )

    assert decision.risk_level == AgentRiskLevel.CRITICAL
    assert decision.fulfillment_selection is None
    assert "prepare_checkout" not in decision.allowed_tools


@pytest.mark.parametrize("reply", ["ready", "yes ready", "proceed", "confirm", "go ahead"])
def test_checkout_continuation_phrases_require_pending_delivery_context(reply: str) -> None:
    address_id = str(uuid.uuid4())
    clarification = FulfillmentClarification(
        options=(FulfillmentOption(address_id, "Use Home address", "local_delivery"),),
        default_destination_id=address_id,
        kind="delivery_address",
    )

    standalone = AgentActionPolicy.classify(reply)
    contextual = AgentActionPolicy.classify(reply, pending_fulfillment=clarification)

    assert standalone.intent == AgentIntent.ANSWER
    assert "prepare_checkout" not in standalone.allowed_tools
    assert contextual.intent == AgentIntent.FULFILLMENT_SELECT
    assert contextual.fulfillment_selection is not None
    assert contextual.fulfillment_selection.destination_id == address_id
    assert contextual.fulfillment_selection.fulfillment_type == "local_delivery"


@pytest.mark.anyio
async def test_checkout_flow_reconciles_phantom_controls_and_continues_with_local_delivery(
    client: httpx.AsyncClient,
    seeded: dict[str, object],
    session_factory: sessionmaker[Session],
) -> None:
    added = await client.post(
        "/api/v1/cart/items",
        headers=headers(seeded),
        json={
            "variant_id": str(seeded["drink_variant_id"]),
            "quantity": 1,
            "modifier_option_ids": [str(seeded["unsweetened_id"])],
        },
    )
    assert added.status_code == 201

    def repeat_reads(toolbox: AgentToolbox) -> None:
        toolbox.get_cart()
        for _ in range(3):
            toolbox.list_fulfillment_destinations()

    runtime = ActionRuntime(
        repeat_reads,
        text="Please use the checkout controls below to complete your order.",
    )
    current = await conversation(client, seeded, runtime)
    first = await client.post(
        f"/api/v1/agent/conversations/{current['id']}/messages",
        headers=headers(seeded, "checkout-continuation-start-1"),
        json={"content": "Checkout my cart."},
    )
    assert first.status_code == 200
    first_message = first.json()["message"]
    assert "checkout controls below" not in first_message["content"].lower()
    assert "checkout" not in first_message["structured_content"]
    assert first_message["structured_content"]["suggestions"] == ["Local delivery", "Pickup"]
    assert first_message["structured_content"]["clarification"]["kind"] == ("fulfillment_method")
    assert first_message["structured_content"]["guardrail"]["ui_claim_corrected"] is True
    assert [item["tool"] for item in first_message["structured_content"]["activity"]].count(
        "list_fulfillment_destinations"
    ) == 1

    second = await client.post(
        f"/api/v1/agent/conversations/{current['id']}/messages",
        headers=headers(seeded, "checkout-continuation-delivery-1"),
        json={"content": "local delivery"},
    )
    assert second.status_code == 200
    second_message = second.json()["message"]
    assert second_message["structured_content"]["checkout"]["status"] == ("ready_for_approval")
    assert second_message["structured_content"]["checkout"]["payment_started"] is False
    assert "Home" in second_message["content"]
    assert runtime.calls == 1

    with session_factory() as db:
        checkout = db.scalar(select(Checkout))
        assert checkout is not None
        assert checkout.fulfillment_type.value == "local_delivery"
        assert checkout.delivery_address is not None
        assert checkout.delivery_address["postal_code"] == "560038"


@pytest.mark.anyio
async def test_yes_ready_confirms_server_generated_delivery_address(
    client: httpx.AsyncClient,
    seeded: dict[str, object],
    session_factory: sessionmaker[Session],
) -> None:
    added = await client.post(
        "/api/v1/cart/items",
        headers=headers(seeded),
        json={
            "variant_id": str(seeded["drink_variant_id"]),
            "quantity": 1,
            "modifier_option_ids": [str(seeded["unsweetened_id"])],
        },
    )
    assert added.status_code == 201

    runtime = ActionRuntime(
        lambda toolbox: toolbox.list_fulfillment_destinations(),
        text="Use your saved Home address for local delivery?",
    )
    current = await conversation(client, seeded, runtime)
    clarification_response = await client.post(
        f"/api/v1/agent/conversations/{current['id']}/messages",
        headers=headers(seeded, "checkout-address-question-1"),
        json={"content": "Checkout my cart with local delivery."},
    )
    assert clarification_response.status_code == 200
    clarification = clarification_response.json()["message"]["structured_content"]
    assert clarification["clarification"]["kind"] == "delivery_address"
    assert clarification["suggestions"] == ["Use Home address"]

    response = await client.post(
        f"/api/v1/agent/conversations/{current['id']}/messages",
        headers=headers(seeded, "checkout-yes-ready-1"),
        json={"content": "yes ready"},
    )
    assert response.status_code == 200
    message = response.json()["message"]
    assert message["structured_content"]["checkout"]["status"] == "ready_for_approval"
    assert message["structured_content"]["checkout"]["payment_started"] is False
    assert runtime.calls == 1


@pytest.mark.anyio
async def test_model_cannot_guess_fulfillment_or_claim_checkout_controls(
    client: httpx.AsyncClient,
    seeded: dict[str, object],
    session_factory: sessionmaker[Session],
) -> None:
    added = await client.post(
        "/api/v1/cart/items",
        headers=headers(seeded),
        json={
            "variant_id": str(seeded["drink_variant_id"]),
            "quantity": 1,
            "modifier_option_ids": [str(seeded["unsweetened_id"])],
        },
    )
    assert added.status_code == 201

    def guess_pickup(toolbox: AgentToolbox) -> None:
        toolbox.list_fulfillment_destinations()
        toolbox.prepare_checkout("pickup", "", str(seeded["location_id"]))

    runtime = ActionRuntime(
        guess_pickup,
        text="Use the checkout controls below to finish.",
    )
    current = await conversation(client, seeded, runtime)
    response = await client.post(
        f"/api/v1/agent/conversations/{current['id']}/messages",
        headers=headers(seeded, "checkout-guessed-fulfillment-1"),
        json={"content": "Checkout my cart."},
    )
    assert response.status_code == 200
    message = response.json()["message"]
    assert "checkout" not in message["structured_content"]
    assert message["structured_content"]["suggestions"] == ["Local delivery", "Pickup"]
    assert message["structured_content"]["activity"][-1]["status"] == "error"
    assert "checkout controls below" not in message["content"].lower()
    with session_factory() as db:
        assert db.scalar(select(func.count()).select_from(Checkout)) == 0


@pytest.mark.anyio
async def test_unserviceable_delivery_falls_back_to_pickup_location_suggestions(
    client: httpx.AsyncClient,
    seeded: dict[str, object],
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as db, db.begin():
        address = db.get(CustomerAddress, seeded["customer_address_id"])
        merchant = db.scalar(select(Merchant).where(Merchant.slug == "ember-and-leaf"))
        assert address is not None
        assert merchant is not None
        address.postal_code = "999999"
        central = Location(
            merchant_id=merchant.id,
            slug="central-roastery",
            name="Central Roastery",
            kind=LocationKind.ROASTERY,
            postal_code="560027",
            preparation_minutes=25,
        )
        db.add(central)
        db.flush()
        central_id = central.id

    added = await client.post(
        "/api/v1/cart/items",
        headers=headers(seeded),
        json={
            "variant_id": str(seeded["drink_variant_id"]),
            "quantity": 1,
            "modifier_option_ids": [str(seeded["unsweetened_id"])],
        },
    )
    assert added.status_code == 201

    def attempt_delivery(toolbox: AgentToolbox) -> None:
        toolbox.list_fulfillment_destinations()
        toolbox.prepare_checkout(
            "local_delivery",
            str(seeded["customer_address_id"]),
            "",
        )

    runtime = ActionRuntime(
        attempt_delivery,
        text="That address is outside our delivery area.",
    )
    current = await conversation(client, seeded, runtime)
    response = await client.post(
        f"/api/v1/agent/conversations/{current['id']}/messages",
        headers=headers(seeded, "checkout-unserviceable-delivery-1"),
        json={"content": "Checkout my cart with local delivery to my default address."},
    )

    assert response.status_code == 200
    structured = response.json()["message"]["structured_content"]
    assert "checkout" not in structured
    assert structured["clarification"]["kind"] == "pickup_location"
    assert structured["suggestions"] == ["Central Roastery", "Indiranagar Café"]
    assert structured["activity"][-1]["tool"] == "prepare_checkout"
    assert structured["activity"][-1]["status"] == "error"

    with session_factory() as db:
        call = db.scalar(select(AgentToolCall).where(AgentToolCall.tool_name == "prepare_checkout"))
        assert call is not None
        assert call.error_code == "address_not_serviceable"
        assert db.scalar(select(func.count()).select_from(Checkout)) == 0

    pickup = await client.post(
        f"/api/v1/agent/conversations/{current['id']}/messages",
        headers=headers(seeded, "checkout-unserviceable-pickup-1"),
        json={"content": "Central Roastery"},
    )
    assert pickup.status_code == 200
    pickup_message = pickup.json()["message"]
    assert pickup_message["structured_content"]["checkout"]["fulfillment_type"] == "pickup"
    assert pickup_message["structured_content"]["checkout"]["payment_started"] is False
    assert runtime.calls == 1

    with session_factory() as db:
        checkout = db.scalar(select(Checkout))
        assert checkout is not None
        assert checkout.location_id == central_id


@pytest.mark.anyio
async def test_incomplete_schedule_shows_controls_and_does_not_claim_trusted_ui(
    client: httpx.AsyncClient,
    seeded: dict[str, object],
    session_factory: sessionmaker[Session],
) -> None:
    added = await client.post(
        "/api/v1/cart/items",
        headers=headers(seeded),
        json={
            "variant_id": str(seeded["drink_variant_id"]),
            "quantity": 1,
            "modifier_option_ids": [str(seeded["unsweetened_id"])],
        },
    )
    assert added.status_code == 201

    runtime = ActionRuntime(
        lambda toolbox: toolbox.list_scheduling_options(),
        text=(
            "I have all the details. Please use the trusted account UI to review and "
            "authorize the scheduled bounds."
        ),
    )
    current = await conversation(client, seeded, runtime)
    response = await client.post(
        f"/api/v1/agent/conversations/{current['id']}/messages",
        headers=headers(seeded, "schedule-needs-details-1"),
        json={"content": "Schedule House Cold Brew daily for pickup."},
    )

    assert response.status_code == 200
    message = response.json()["message"]
    assert "trusted account ui" not in message["content"].lower()
    assert "first run" in message["content"].lower()
    assert message["structured_content"]["clarification"]["purpose"] == "schedule_draft"
    assert message["structured_content"]["clarification"]["kind"] == "pickup_location"
    assert message["structured_content"]["suggestions"] == ["Indiranagar Café"]
    assert message["structured_content"]["schedule_draft_pending"] == {
        "version": "agent-schedule-draft-1"
    }
    assert message["structured_content"]["schedule_configuration"]["frequency"] == "daily"
    assert message["structured_content"]["schedule_configuration"]["draft_available"] is False
    assert message["structured_content"]["schedule_configuration"]["availability_code"] == (
        "recurring_provider_disabled"
    )
    assert "scheduled_purchase" not in message["structured_content"]

    details = await client.post(
        f"/api/v1/agent/conversations/{current['id']}/messages",
        headers=headers(seeded, "schedule-bounds-continuation-1"),
        json={"content": "7, 100000 rupee"},
    )
    assert details.status_code == 200
    detail_message = details.json()["message"]
    assert detail_message["structured_content"]["clarification"]["kind"] == "pickup_location"
    assert detail_message["structured_content"]["schedule_draft_pending"] == {
        "version": "agent-schedule-draft-1"
    }

    selection = await client.post(
        f"/api/v1/agent/conversations/{current['id']}/messages",
        headers=headers(seeded, "schedule-pickup-selection-1"),
        json={"content": "Indiranagar Café"},
    )
    assert selection.status_code == 200
    assert "checkout" not in selection.json()["message"]["structured_content"]
    assert runtime.calls == 3

    with session_factory() as db:
        latest_run = db.scalar(select(AgentRun).order_by(AgentRun.started_at.desc()))
        assert latest_run is not None
        assert latest_run.intent == AgentIntent.SCHEDULE_DRAFT.value
        assert db.scalar(select(func.count()).select_from(Checkout)) == 0


@pytest.mark.anyio
async def test_schedule_bounds_then_address_confirmation_creates_reviewable_draft(
    client: httpx.AsyncClient,
    seeded: dict[str, object],
    session_factory: sessionmaker[Session],
) -> None:
    first_run = datetime.now(UTC) + timedelta(days=3)
    expires_at = first_run + timedelta(days=8)
    with session_factory() as db, db.begin():
        autopay = PaymentInstrument(
            user_id=seeded["customer_id"],
            provider="razorpay_test",
            instrument_type="com.razorpay.upi.autopay",
            alias="Razorpay UPI Autopay",
            status="active",
            is_default=False,
            instrument_metadata={"mode": "test", "token_status": "not_started"},
        )
        db.add(autopay)
        db.flush()
        autopay_id = autopay.id

    def schedule_action(toolbox: AgentToolbox) -> None:
        toolbox.list_scheduling_options()
        if toolbox.current_message == "yes":
            toolbox.draft_scheduled_purchase(
                items=[
                    {
                        "acceptable_variant_ids": [str(seeded["drink_variant_id"])],
                        "quantity": 1,
                        "modifier_option_ids": [str(seeded["unsweetened_id"])],
                    }
                ],
                fulfillment_type="local_delivery",
                payment_instrument_id=str(autopay_id),
                address_id=str(seeded["customer_address_id"]),
                first_run_at=first_run.isoformat(),
                expires_at=expires_at.isoformat(),
                frequency="daily",
                interval_count=1,
                max_occurrences=7,
                max_amount_minor=30_000,
                max_total_minor=210_000,
            )

    runtime = ActionRuntime(
        schedule_action,
        text="Use the trusted account UI to review this schedule draft.",
    )
    current = await conversation(client, seeded, runtime)
    start = await client.post(
        f"/api/v1/agent/conversations/{current['id']}/messages",
        headers=headers(seeded, "schedule-full-start-1"),
        json={"content": "Schedule House Cold Brew daily with local delivery."},
    )
    assert start.status_code == 200
    assert start.json()["message"]["structured_content"]["suggestions"] == ["Use Home address"]

    bounds = await client.post(
        f"/api/v1/agent/conversations/{current['id']}/messages",
        headers=headers(seeded, "schedule-full-bounds-1"),
        json={
            "content": (
                f"maximum 7 occurrences, first run {first_run.isoformat()}, expires "
                f"{expires_at.isoformat()}, per-order cap INR 300 and total cap INR 2100"
            )
        },
    )
    assert bounds.status_code == 200
    bounds_message = bounds.json()["message"]
    assert bounds_message["structured_content"]["suggestions"] == ["Use Home address"]
    assert "schedule_configuration" not in bounds_message["structured_content"]
    assert bounds_message["structured_content"]["schedule_draft_pending"] == {
        "version": "agent-schedule-draft-1"
    }

    confirmed = await client.post(
        f"/api/v1/agent/conversations/{current['id']}/messages",
        headers=headers(seeded, "schedule-full-confirm-1"),
        json={"content": "yes"},
    )
    assert confirmed.status_code == 200
    confirmed_message = confirmed.json()["message"]
    artifact = confirmed_message["structured_content"]["scheduled_purchase"]
    assert artifact["status"] == "draft"
    assert artifact["review_url"] == "/account#scheduled-purchases"
    assert artifact["authorization_required"] is True
    assert artifact["payment_started"] is False
    assert "schedule_configuration" not in confirmed_message["structured_content"]
    assert "schedule_draft_pending" not in confirmed_message["structured_content"]
    assert runtime.calls == 3

    with session_factory() as db:
        schedule = db.scalar(select(ScheduledPurchaseIntent))
        assert schedule is not None
        assert schedule.status.value == "draft"
        assert db.scalar(select(func.count()).select_from(Checkout)) == 0


def test_contextual_checkout_is_bound_to_the_selected_destination() -> None:
    central_id = str(uuid.uuid4())
    indiranagar_id = str(uuid.uuid4())
    clarification = FulfillmentClarification(
        options=(
            FulfillmentOption(central_id, "Central Roastery", "pickup"),
            FulfillmentOption(indiranagar_id, "Indiranagar Café", "pickup"),
        ),
        default_destination_id=central_id,
    )
    decision = AgentActionPolicy.classify("Central Roastery", pending_fulfillment=clarification)
    toolbox = AgentToolbox(
        db=None,  # type: ignore[arg-type]
        customer=None,  # type: ignore[arg-type]
        merchant_id=uuid.uuid4(),
        merchant_slug="ember-and-leaf",
        conversation_id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        policy_decision=decision,
        current_message="Central Roastery",
    )
    toolbox.seen_destination_ids = {central_id, indiranagar_id}

    with pytest.raises(DomainError) as mismatch:
        toolbox._validate_action_scope(
            "prepare_checkout",
            {
                "fulfillment_type": "pickup",
                "address_id": "",
                "location_id": indiranagar_id,
            },
        )

    assert mismatch.value.code == "agent_context_destination_mismatch"


def test_contextual_schedule_is_bound_to_the_selected_destination() -> None:
    central_id = str(uuid.uuid4())
    indiranagar_id = str(uuid.uuid4())
    payment_id = str(uuid.uuid4())
    clarification = FulfillmentClarification(
        options=(
            FulfillmentOption(central_id, "Central Roastery", "pickup"),
            FulfillmentOption(indiranagar_id, "Indiranagar Café", "pickup"),
        ),
        default_destination_id=central_id,
        purpose="schedule_draft",
    )
    decision = AgentActionPolicy.classify(
        "Central Roastery",
        pending_fulfillment=clarification,
    )
    toolbox = AgentToolbox(
        db=None,  # type: ignore[arg-type]
        customer=None,  # type: ignore[arg-type]
        merchant_id=uuid.uuid4(),
        merchant_slug="ember-and-leaf",
        conversation_id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        policy_decision=decision,
        current_message="Central Roastery",
    )
    toolbox.seen_destination_ids = {central_id, indiranagar_id}
    toolbox.seen_payment_instrument_ids = {payment_id}

    with pytest.raises(DomainError) as mismatch:
        toolbox._validate_action_scope(
            "draft_scheduled_purchase",
            {
                "fulfillment_type": "pickup",
                "address_id": "",
                "location_id": indiranagar_id,
                "payment_instrument_id": payment_id,
            },
        )

    assert mismatch.value.code == "agent_schedule_destination_mismatch"


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("reply", "expected_name", "expected_reason"),
    [
        ("yes", "Central Roastery", "contextual_fulfillment_default"),
        ("Indiranagar Cafe", "Indiranagar Café", "contextual_fulfillment_option"),
    ],
)
async def test_pickup_clarification_emits_chips_and_resolves_next_turn_deterministically(
    client: httpx.AsyncClient,
    seeded: dict[str, object],
    session_factory: sessionmaker[Session],
    reply: str,
    expected_name: str,
    expected_reason: str,
) -> None:
    with session_factory() as db, db.begin():
        merchant = db.scalar(select(Merchant).where(Merchant.slug == "ember-and-leaf"))
        assert merchant is not None
        central = Location(
            merchant_id=merchant.id,
            slug="central-roastery",
            name="Central Roastery",
            kind=LocationKind.ROASTERY,
            postal_code="560027",
            preparation_minutes=25,
        )
        db.add(central)
        db.flush()
        central_id = central.id

    added = await client.post(
        "/api/v1/cart/items",
        headers=headers(seeded),
        json={
            "variant_id": str(seeded["drink_variant_id"]),
            "quantity": 1,
            "modifier_option_ids": [str(seeded["unsweetened_id"])],
        },
    )
    assert added.status_code == 201

    runtime = ActionRuntime(lambda toolbox: toolbox.list_fulfillment_destinations())
    current = await conversation(client, seeded, runtime)
    key_suffix = "central" if expected_name == "Central Roastery" else "indiranagar"
    clarification_response = await client.post(
        f"/api/v1/agent/conversations/{current['id']}/messages",
        headers=headers(seeded, f"pickup-choice-{key_suffix}-1"),
        json={"content": "Buy my cart; I will pick it up."},
    )
    assert clarification_response.status_code == 200
    structured = clarification_response.json()["message"]["structured_content"]
    assert structured["suggestions"] == ["Central Roastery", "Indiranagar Café"]
    assert structured["clarification"]["kind"] == "pickup_location"
    assert structured["clarification"]["default_destination_id"] == str(central_id)
    assert structured["destinations"]["pickup_locations"][0]["is_default"] is True

    checkout_response = await client.post(
        f"/api/v1/agent/conversations/{current['id']}/messages",
        headers=headers(seeded, f"pickup-resolve-{key_suffix}-1"),
        json={"content": reply},
    )
    assert checkout_response.status_code == 200
    payload = checkout_response.json()["message"]
    assert payload["structured_content"]["checkout"]["status"] == "ready_for_approval"
    assert payload["structured_content"]["checkout"]["payment_started"] is False
    assert expected_name in payload["content"]
    assert runtime.calls == 1

    with session_factory() as db:
        checkout = db.scalar(select(Checkout))
        assert checkout is not None
        expected_id = central_id if expected_name == "Central Roastery" else seeded["location_id"]
        assert checkout.location_id == expected_id
        audit = db.scalar(
            select(AuditEvent)
            .where(AuditEvent.event_type == "agent.policy.evaluated")
            .order_by(AuditEvent.occurred_at.desc())
        )
        assert audit is not None
        assert expected_reason in audit.payload["reason_codes"]
        run = db.get(AgentRun, uuid.UUID(audit.payload["run_id"]))
        assert run is not None
        assert run.intent == AgentIntent.FULFILLMENT_SELECT.value


@pytest.mark.anyio
async def test_runtime_replacement_cannot_bypass_current_turn_consent(
    client: httpx.AsyncClient,
    seeded: dict[str, object],
    session_factory: sessionmaker[Session],
) -> None:
    runtime = ActionRuntime(
        lambda toolbox: toolbox.add_to_cart(
            str(seeded["drink_variant_id"]),
            1,
            [str(seeded["unsweetened_id"])],
        )
    )
    current = await conversation(client, seeded, runtime)
    response = await client.post(
        f"/api/v1/agent/conversations/{current['id']}/messages",
        headers=headers(seeded, "security-no-consent-1"),
        json={"content": "Recommend a refreshing beverage."},
    )
    assert response.status_code == 200
    assert response.json()["message"]["structured_content"]["activity"][0]["status"] == "error"

    cart = await client.get("/api/v1/cart", headers=headers(seeded))
    assert cart.json()["item_count"] == 0
    with session_factory() as db:
        call = db.scalar(select(AgentToolCall).where(AgentToolCall.tool_name == "add_to_cart"))
        assert call is not None
        assert call.error_code == "agent_tool_denied"
        denial = db.scalar(
            select(AuditEvent).where(
                AuditEvent.event_type == "agent.tool.failed",
                AuditEvent.payload["policy_reason"].as_string() == "tool_not_allowed_for_intent",
            )
        )
        assert denial is not None


@pytest.mark.anyio
async def test_one_mutation_budget_blocks_duplicate_cart_write(
    client: httpx.AsyncClient,
    seeded: dict[str, object],
) -> None:
    def add_twice(toolbox: AgentToolbox) -> None:
        for _ in range(2):
            toolbox.add_to_cart(
                str(seeded["drink_variant_id"]),
                1,
                [str(seeded["unsweetened_id"])],
            )

    runtime = ActionRuntime(add_twice)
    current = await conversation(client, seeded, runtime)
    response = await client.post(
        f"/api/v1/agent/conversations/{current['id']}/messages",
        headers=headers(seeded, "security-one-mutation-1"),
        json={"content": "Add one unsweetened cold brew to my cart."},
    )
    assert response.status_code == 200
    activity = response.json()["message"]["structured_content"]["activity"]
    assert [item["status"] for item in activity] == ["success", "error"]
    cart = await client.get("/api/v1/cart", headers=headers(seeded))
    assert cart.json()["item_count"] == 1


@pytest.mark.anyio
async def test_affirmative_buy_can_add_a_uniquely_selected_product(
    client: httpx.AsyncClient,
    seeded: dict[str, object],
) -> None:
    def add_selected_product(toolbox: AgentToolbox) -> None:
        toolbox.search_catalog("House Cold Brew")
        toolbox.add_to_cart(
            str(seeded["drink_variant_id"]),
            1,
            [str(seeded["unsweetened_id"])],
        )

    runtime = ActionRuntime(add_selected_product)
    current = await conversation(client, seeded, runtime)
    response = await client.post(
        f"/api/v1/agent/conversations/{current['id']}/messages",
        headers=headers(seeded, "security-affirmative-buy-1"),
        json={"content": "Yes, buy it."},
    )
    assert response.status_code == 200
    assert [
        item["status"] for item in response.json()["message"]["structured_content"]["activity"]
    ] == ["success", "success"]
    cart = await client.get("/api/v1/cart", headers=headers(seeded))
    assert cart.json()["item_count"] == 1


@pytest.mark.anyio
async def test_saved_address_request_reads_the_authenticated_customers_default(
    client: httpx.AsyncClient,
    seeded: dict[str, object],
) -> None:
    runtime = ActionRuntime(lambda toolbox: toolbox.list_fulfillment_destinations())
    current = await conversation(client, seeded, runtime)
    response = await client.post(
        f"/api/v1/agent/conversations/{current['id']}/messages",
        headers=headers(seeded, "security-default-address-read-1"),
        json={"content": "Check my default address."},
    )
    assert response.status_code == 200
    structured = response.json()["message"]["structured_content"]
    assert structured["activity"][0]["status"] == "success"
    assert structured["destinations"]["delivery_addresses"] == [
        {
            "id": str(seeded["customer_address_id"]),
            "label": "Home",
            "city": "Bengaluru",
            "region": "Karnataka",
            "postal_code": "560038",
            "is_default": True,
        }
    ]


@pytest.mark.anyio
async def test_saved_address_selection_can_prepare_but_not_approve_checkout(
    client: httpx.AsyncClient,
    seeded: dict[str, object],
    session_factory: sessionmaker[Session],
) -> None:
    added = await client.post(
        "/api/v1/cart/items",
        headers=headers(seeded),
        json={
            "variant_id": str(seeded["drink_variant_id"]),
            "quantity": 1,
            "modifier_option_ids": [str(seeded["unsweetened_id"])],
        },
    )
    assert added.status_code == 201

    def prepare_with_registered_address(toolbox: AgentToolbox) -> None:
        toolbox.list_fulfillment_destinations()
        toolbox.prepare_checkout("local_delivery", str(seeded["customer_address_id"]), "")

    runtime = ActionRuntime(prepare_with_registered_address)
    current = await conversation(client, seeded, runtime)
    response = await client.post(
        f"/api/v1/agent/conversations/{current['id']}/messages",
        headers=headers(seeded, "security-registered-address-select-1"),
        json={"content": "Use my registered address."},
    )
    assert response.status_code == 200
    structured = response.json()["message"]["structured_content"]
    assert [item["status"] for item in structured["activity"]] == ["success", "success"]
    assert structured["checkout"]["status"] == "ready_for_approval"
    assert structured["checkout"]["approval_required"] is True
    assert structured["checkout"]["payment_started"] is False
    with session_factory() as db:
        assert db.scalar(select(func.count()).select_from(Checkout)) == 1


@pytest.mark.anyio
async def test_buy_turn_can_add_but_cannot_also_prepare_checkout(
    client: httpx.AsyncClient,
    seeded: dict[str, object],
    session_factory: sessionmaker[Session],
) -> None:
    def add_then_prepare(toolbox: AgentToolbox) -> None:
        toolbox.search_catalog("House Cold Brew")
        toolbox.add_to_cart(
            str(seeded["drink_variant_id"]),
            1,
            [str(seeded["unsweetened_id"])],
        )
        toolbox.list_fulfillment_destinations()
        toolbox.prepare_checkout("local_delivery", str(seeded["customer_address_id"]), "")

    runtime = ActionRuntime(add_then_prepare)
    current = await conversation(client, seeded, runtime)
    response = await client.post(
        f"/api/v1/agent/conversations/{current['id']}/messages",
        headers=headers(seeded, "security-buy-one-mutation-1"),
        json={"content": ("Yes, buy the House Cold Brew and check out using my default address.")},
    )
    assert response.status_code == 200
    activity = response.json()["message"]["structured_content"]["activity"]
    assert [item["status"] for item in activity] == [
        "success",
        "success",
        "success",
        "error",
    ]
    cart = await client.get("/api/v1/cart", headers=headers(seeded))
    assert cart.json()["item_count"] == 1
    with session_factory() as db:
        assert db.scalar(select(func.count()).select_from(Checkout)) == 0
        prepare_call = db.scalar(
            select(AgentToolCall).where(AgentToolCall.tool_name == "prepare_checkout")
        )
        assert prepare_call is not None
        assert prepare_call.output_payload["error"]["reason"] == "mutation_budget_exceeded"


@pytest.mark.anyio
async def test_financial_prompt_cannot_prepare_checkout(
    client: httpx.AsyncClient,
    seeded: dict[str, object],
    session_factory: sessionmaker[Session],
) -> None:
    runtime = ActionRuntime(
        lambda toolbox: toolbox.prepare_checkout(
            "local_delivery", str(seeded["customer_address_id"]), ""
        )
    )
    current = await conversation(client, seeded, runtime)
    response = await client.post(
        f"/api/v1/agent/conversations/{current['id']}/messages",
        headers=headers(seeded, "security-financial-deny-1"),
        json={"content": "Approve and pay for my basket right now."},
    )
    assert response.status_code == 200
    assert response.json()["message"]["structured_content"]["activity"][0]["status"] == "error"
    with session_factory() as db:
        assert db.scalar(select(func.count()).select_from(Checkout)) == 0


@pytest.mark.anyio
async def test_cross_merchant_variant_is_rejected_by_commerce_module(
    client: httpx.AsyncClient,
    seeded: dict[str, object],
) -> None:
    runtime = ActionRuntime(
        lambda toolbox: toolbox.add_to_cart(str(seeded["other_variant_id"]), 1, [])
    )
    current = await conversation(client, seeded, runtime)
    response = await client.post(
        f"/api/v1/agent/conversations/{current['id']}/messages",
        headers=headers(seeded, "security-cross-merchant-1"),
        json={"content": "Add one of that item to my cart."},
    )
    assert response.status_code == 200
    activity = response.json()["message"]["structured_content"]["activity"]
    assert activity[0]["status"] == "error"
    cart = await client.get("/api/v1/cart", headers=headers(seeded))
    assert cart.json()["item_count"] == 0


def test_output_guard_redacts_restricted_material() -> None:
    value = "Use Bearer abcdefghijklmnop and card 4111 1111 1111 1111"
    protected = AgentOutputGuard.sanitize(value, 6000)
    assert "abcdefghijklmnop" not in protected
    assert "4111" not in protected
    assert protected.count("[REDACTED]") == 2


def test_output_guard_rejects_configuration_controls_that_have_no_artifact() -> None:
    text, corrected = AgentOutputGuard.reconcile_ui_claims(
        "Please use the configuration controls below.",
        {"activity": []},
    )

    assert corrected is True
    assert "controls below" not in text.lower()
    assert "do not have verified product-configuration controls" in text.lower()


def test_output_guard_keeps_checkout_controls_claim_when_checkout_exists() -> None:
    original = "Please use the checkout controls below."
    text, corrected = AgentOutputGuard.reconcile_ui_claims(
        original,
        {"checkout": {"status": "ready_for_approval"}},
    )

    assert corrected is False
    assert text == original


def test_output_guard_rejects_trusted_schedule_ui_claim_without_draft() -> None:
    text, corrected = AgentOutputGuard.reconcile_ui_claims(
        "Please use the trusted account UI to review and authorize the scheduled bounds.",
        {"activity": []},
    )

    assert corrected is True
    assert "trusted account ui" not in text.lower()
    assert "first run" in text.lower()
    assert "per-order" in text.lower()


def test_output_guard_keeps_trusted_schedule_ui_claim_with_draft() -> None:
    original = "Use the trusted account UI to review this schedule draft."
    text, corrected = AgentOutputGuard.reconcile_ui_claims(
        original,
        {"scheduled_purchase": {"status": "draft"}},
    )

    assert corrected is False
    assert text == original


def test_output_guard_rejects_false_schedule_tool_unavailable_claim() -> None:
    text, corrected = AgentOutputGuard.reconcile_ui_claims(
        "Automated scheduling tools are currently unavailable.",
        {
            "activity": [
                {
                    "tool": "list_scheduling_options",
                    "status": "success",
                    "label": "Schedule boundaries checked",
                }
            ],
            "schedule_draft_pending": {"version": "agent-schedule-draft-1"},
            "schedule_configuration": {
                "version": "agent-schedule-configuration-1",
                "draft_available": True,
            },
        },
    )

    assert corrected is True
    assert "unavailable" not in text.lower()
    assert "schedule controls" in text.lower()


def test_output_guard_explains_disabled_recurring_provider() -> None:
    text, corrected = AgentOutputGuard.reconcile_ui_claims(
        "Automated scheduling tools are currently unavailable.",
        {
            "activity": [],
            "schedule_configuration": {
                "version": "agent-schedule-configuration-1",
                "draft_available": False,
                "availability_message": (
                    "Recurring scheduling is disabled for this environment. "
                    "A merchant operator must enable it and restart the backend."
                ),
            },
        },
    )

    assert corrected is True
    assert "merchant operator" in text.lower()
    assert "restart the backend" in text.lower()


@pytest.mark.anyio
async def test_stale_verified_mutation_checkpoint_recovers_without_model_call(
    client: httpx.AsyncClient,
    seeded: dict[str, object],
    session_factory: sessionmaker[Session],
) -> None:
    runtime = ActionRuntime()
    current = await conversation(client, seeded, runtime)
    request_content = "Add one unsweetened cold brew to my cart."
    request_key = "security-checkpoint-recover-1"
    with session_factory() as db, db.begin():
        conversation_row = db.get(AgentConversation, uuid.UUID(current["id"]))
        assert conversation_row is not None
        run = AgentRun(
            conversation_id=conversation_row.id,
            customer_id=seeded["customer_id"],
            idempotency_key=request_key,
            request_sha256=__import__("hashlib").sha256(request_content.encode()).hexdigest(),
            model=runtime.model_name,
            status="running",
            started_at=datetime.now(UTC) - timedelta(minutes=2),
            checkpoint_state={"version": "agent-checkpoint-1", "phase": "mutation_verified"},
        )
        db.add(run)
        db.flush()
        db.add(
            AgentMessage(
                conversation_id=conversation_row.id,
                run_id=run.id,
                role="user",
                content=request_content,
                structured_content={},
            )
        )
        db.add(
            AgentToolCall(
                run_id=run.id,
                tool_name="add_to_cart",
                status="completed",
                input_payload={"variant_id": str(seeded["drink_variant_id"]), "quantity": 1},
                output_payload={
                    "status": "success",
                    "cart": {"item_count": 1, "subtotal_minor": 22000},
                    "verification": {"status": "verified", "source": "commerce_database"},
                },
                completed_at=datetime.now(UTC) - timedelta(minutes=1),
            )
        )

    response = await client.post(
        f"/api/v1/agent/conversations/{current['id']}/messages",
        headers=headers(seeded, request_key),
        json={"content": request_content},
    )
    assert response.status_code == 200
    assert response.json()["message"]["structured_content"]["recovered"] is True
    assert runtime.calls == 0


@pytest.mark.anyio
async def test_customer_agent_turn_rate_limit_is_enforced(
    client: httpx.AsyncClient,
    seeded: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(get_settings(), "agent_max_turns_per_minute", 1)
    runtime = ActionRuntime()
    current = await conversation(client, seeded, runtime)
    first = await client.post(
        f"/api/v1/agent/conversations/{current['id']}/messages",
        headers=headers(seeded, "security-rate-first-1"),
        json={"content": "Show me tea."},
    )
    assert first.status_code == 200
    limited = await client.post(
        f"/api/v1/agent/conversations/{current['id']}/messages",
        headers=headers(seeded, "security-rate-second-1"),
        json={"content": "Show me coffee."},
    )
    assert limited.status_code == 429
    assert limited.json()["error"]["code"] == "agent_rate_limit_exceeded"
