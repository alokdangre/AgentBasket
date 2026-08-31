import uuid
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from langchain_core.messages import AIMessage
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.agents.runtime import (
    AgentRuntimeResult,
    LangGraphShoppingRuntime,
    get_agent_runtime,
)
from app.agents.tools import AgentToolbox
from app.core.errors import DomainError
from app.db.models import (
    AgentRun,
    AgentToolCall,
    AuditEvent,
    Checkout,
    CheckoutApproval,
    Order,
)
from app.main import app


class FakeRuntime:
    model_name = "fake-shopping-model"

    def __init__(self, action: Callable[[AgentToolbox], None] | None = None) -> None:
        self.action = action
        self.calls = 0

    async def run(self, *, toolbox: AgentToolbox, **_: Any) -> AgentRuntimeResult:
        self.calls += 1
        if self.action:
            self.action(toolbox)
        return AgentRuntimeResult(
            text="Here is the catalog-grounded result.", model=self.model_name
        )


class FailingRuntime(FakeRuntime):
    async def run(self, *, toolbox: AgentToolbox, **_: Any) -> AgentRuntimeResult:
        self.calls += 1
        if self.action:
            self.action(toolbox)
        raise DomainError(
            "agent_temporarily_unavailable",
            "Ask Ember is temporarily unavailable. No payment or approval was made.",
            503,
        )


class CapturingGraph:
    def __init__(self, *, timeout: bool = False) -> None:
        self.timeout = timeout
        self.config: dict[str, Any] | None = None

    async def ainvoke(self, _: object, config: dict[str, Any]) -> dict[str, object]:
        self.config = config
        if self.timeout:
            raise TimeoutError
        return {"messages": [AIMessage(content="Catalog-grounded answer.")]}


def _headers(seeded: dict[str, object]) -> dict[str, str]:
    return {"Authorization": f"Bearer {seeded['customer_token']}"}


async def _conversation(
    client: httpx.AsyncClient,
    seeded: dict[str, object],
    runtime: FakeRuntime,
) -> dict[str, Any]:
    app.dependency_overrides[get_agent_runtime] = lambda: runtime
    response = await client.post(
        "/api/v1/agent/conversations/current",
        headers=_headers(seeded),
        json={"merchant_slug": "ember-and-leaf"},
    )
    assert response.status_code == 200
    return response.json()


@pytest.mark.anyio
async def test_conversation_requires_auth_and_current_is_stable(
    client: httpx.AsyncClient,
    seeded: dict[str, object],
) -> None:
    runtime = FakeRuntime()
    app.dependency_overrides[get_agent_runtime] = lambda: runtime
    unauthenticated = await client.post(
        "/api/v1/agent/conversations/current",
        json={"merchant_slug": "ember-and-leaf"},
    )
    assert unauthenticated.status_code == 401

    first = await _conversation(client, seeded, runtime)
    second = await _conversation(client, seeded, runtime)
    assert second["id"] == first["id"]
    assert first["messages"][0]["role"] == "assistant"
    assert first["messages"][0]["structured_content"]["suggestions"]


@pytest.mark.anyio
async def test_conversation_is_private(
    client: httpx.AsyncClient,
    seeded: dict[str, object],
) -> None:
    runtime = FakeRuntime()
    conversation = await _conversation(client, seeded, runtime)
    registration = await client.post(
        "/api/v1/auth/register",
        json={
            "email": "other-agent-user@example.com",
            "password": "correct-horse-battery-staple",
            "full_name": "Other Customer",
        },
    )
    other_headers = {"Authorization": f"Bearer {registration.json()['access_token']}"}
    response = await client.get(
        f"/api/v1/agent/conversations/{conversation['id']}", headers=other_headers
    )
    assert response.status_code == 404


@pytest.mark.anyio
async def test_recommendation_is_catalog_grounded_audited_and_idempotent(
    client: httpx.AsyncClient,
    seeded: dict[str, object],
    session_factory: sessionmaker[Session],
) -> None:
    runtime = FakeRuntime(
        lambda toolbox: toolbox.recommend_products("smooth refreshing cold brew", 30000, "560038")
    )
    conversation = await _conversation(client, seeded, runtime)
    request_headers = {
        **_headers(seeded),
        "Idempotency-Key": "agent-message-recommend-1",
    }
    first = await client.post(
        f"/api/v1/agent/conversations/{conversation['id']}/messages",
        headers=request_headers,
        json={"content": "Recommend something refreshing under ₹300."},
    )
    assert first.status_code == 200
    payload = first.json()
    assert payload["message"]["structured_content"]["products"][0]["name"] == "House Cold Brew"
    assert payload["message"]["structured_content"]["activity"][0]["status"] == "success"

    repeated = await client.post(
        f"/api/v1/agent/conversations/{conversation['id']}/messages",
        headers=request_headers,
        json={"content": "Recommend something refreshing under ₹300."},
    )
    assert repeated.status_code == 200
    assert repeated.json()["message"]["id"] == payload["message"]["id"]
    assert runtime.calls == 1

    conflict = await client.post(
        f"/api/v1/agent/conversations/{conversation['id']}/messages",
        headers=request_headers,
        json={"content": "Now recommend tea."},
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "idempotency_key_reused"

    with session_factory() as db:
        tool_call = db.scalar(select(AgentToolCall))
        assert tool_call is not None
        assert tool_call.tool_name == "recommend_products"
        assert tool_call.status == "completed"
        audit = db.scalar(select(AuditEvent).where(AuditEvent.event_type == "agent.tool.completed"))
        assert audit is not None
        assert audit.payload["tool_name"] == "recommend_products"


@pytest.mark.anyio
async def test_agent_can_customize_and_add_to_only_the_customers_cart(
    client: httpx.AsyncClient,
    seeded: dict[str, object],
) -> None:
    runtime = FakeRuntime(
        lambda toolbox: toolbox.add_to_cart(
            str(seeded["drink_variant_id"]),
            1,
            [str(seeded["unsweetened_id"]), str(seeded["oat_id"])],
        )
    )
    conversation = await _conversation(client, seeded, runtime)
    response = await client.post(
        f"/api/v1/agent/conversations/{conversation['id']}/messages",
        headers={**_headers(seeded), "Idempotency-Key": "agent-cart-add-item-1"},
        json={"content": "Add one regular cold brew, unsweetened, with oat milk."},
    )
    assert response.status_code == 200
    cart_artifact = response.json()["message"]["structured_content"]["cart"]
    assert cart_artifact["item_count"] == 1
    assert cart_artifact["subtotal_minor"] == 26000

    cart = await client.get("/api/v1/cart", headers=_headers(seeded))
    assert cart.json()["items"][0]["product_name"] == "House Cold Brew"


@pytest.mark.anyio
async def test_agent_prepares_checkout_but_cannot_approve_or_pay(
    client: httpx.AsyncClient,
    seeded: dict[str, object],
    session_factory: sessionmaker[Session],
) -> None:
    added = await client.post(
        "/api/v1/cart/items",
        headers=_headers(seeded),
        json={
            "variant_id": str(seeded["drink_variant_id"]),
            "quantity": 1,
            "modifier_option_ids": [str(seeded["unsweetened_id"])],
        },
    )
    assert added.status_code == 201

    def prepare(toolbox: AgentToolbox) -> None:
        toolbox.list_fulfillment_destinations()
        toolbox.prepare_checkout("local_delivery", str(seeded["customer_address_id"]), "")

    runtime = FakeRuntime(prepare)
    conversation = await _conversation(client, seeded, runtime)
    response = await client.post(
        f"/api/v1/agent/conversations/{conversation['id']}/messages",
        headers={**_headers(seeded), "Idempotency-Key": "agent-checkout-prepare-1"},
        json={"content": "Buy what is in my cart and deliver it to my default address."},
    )
    assert response.status_code == 200
    checkout = response.json()["message"]["structured_content"]["checkout"]
    assert checkout["status"] == "ready_for_approval"
    assert checkout["approval_required"] is True
    assert checkout["payment_started"] is False
    assert checkout["review_url"].startswith("/checkout/review?checkout_id=")

    with session_factory() as db:
        assert db.scalar(select(func.count()).select_from(Checkout)) == 1
        assert db.scalar(select(func.count()).select_from(CheckoutApproval)) == 0
        assert db.scalar(select(func.count()).select_from(Order)) == 0


@pytest.mark.anyio
async def test_invalid_agent_destination_fails_without_checkout(
    client: httpx.AsyncClient,
    seeded: dict[str, object],
    session_factory: sessionmaker[Session],
) -> None:
    runtime = FakeRuntime(
        lambda toolbox: toolbox.prepare_checkout("local_delivery", str(uuid.uuid4()), "")
    )
    conversation = await _conversation(client, seeded, runtime)
    response = await client.post(
        f"/api/v1/agent/conversations/{conversation['id']}/messages",
        headers={**_headers(seeded), "Idempotency-Key": "agent-bad-address-1"},
        json={"content": "Check out using that other address."},
    )
    assert response.status_code == 200
    activity = response.json()["message"]["structured_content"]["activity"]
    assert activity[0]["status"] == "error"
    with session_factory() as db:
        assert db.scalar(select(func.count()).select_from(Checkout)) == 0


@pytest.mark.anyio
async def test_runtime_failure_is_persisted_without_money_action(
    client: httpx.AsyncClient,
    seeded: dict[str, object],
    session_factory: sessionmaker[Session],
) -> None:
    runtime = FailingRuntime()
    conversation = await _conversation(client, seeded, runtime)
    response = await client.post(
        f"/api/v1/agent/conversations/{conversation['id']}/messages",
        headers={**_headers(seeded), "Idempotency-Key": "agent-runtime-failure-1"},
        json={"content": "What coffee do you have?"},
    )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "agent_temporarily_unavailable"
    with session_factory() as db:
        run = db.scalar(select(AgentRun))
        assert run is not None
        assert run.status == "failed"
        assert db.scalar(select(func.count()).select_from(CheckoutApproval)) == 0
        assert db.scalar(select(func.count()).select_from(Order)) == 0


@pytest.mark.anyio
async def test_interrupted_reply_after_cart_mutation_returns_safe_fallback(
    client: httpx.AsyncClient,
    seeded: dict[str, object],
) -> None:
    runtime = FailingRuntime(
        lambda toolbox: toolbox.add_to_cart(
            str(seeded["drink_variant_id"]),
            1,
            [str(seeded["unsweetened_id"])],
        )
    )
    conversation = await _conversation(client, seeded, runtime)
    response = await client.post(
        f"/api/v1/agent/conversations/{conversation['id']}/messages",
        headers={**_headers(seeded), "Idempotency-Key": "agent-fallback-cart-1"},
        json={"content": "Add one unsweetened cold brew."},
    )
    assert response.status_code == 200
    assert "completed the commerce action" in response.json()["message"]["content"]
    assert response.json()["message"]["structured_content"]["cart"]["item_count"] == 1


def test_langgraph_runtime_registers_only_bounded_commerce_tools() -> None:
    toolbox = AgentToolbox(
        db=None,  # type: ignore[arg-type]
        customer=None,  # type: ignore[arg-type]
        merchant_id=uuid.uuid4(),
        merchant_slug="ember-and-leaf",
        conversation_id=uuid.uuid4(),
        run_id=uuid.uuid4(),
    )
    runtime = LangGraphShoppingRuntime(
        api_key="test-only-key",
        model_name="gemini-3.5-flash-lite",
        timeout_seconds=30,
        max_output_characters=6000,
    )
    names = {tool.name for tool in runtime.build_tools(toolbox)}
    assert names == {
        "search_catalog",
        "recommend_products",
        "get_cart",
        "add_to_cart",
        "update_cart_item",
        "remove_cart_item",
        "list_fulfillment_destinations",
        "prepare_checkout",
    }
    assert "approve_checkout" not in names
    assert "create_payment" not in names


@pytest.mark.anyio
async def test_langgraph_runtime_attaches_safe_trace_correlation_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = LangGraphShoppingRuntime(
        api_key="test-only-key",
        model_name="gemini-3.5-flash-lite",
        timeout_seconds=30,
        max_output_characters=6000,
        tracing_environment="test",
    )
    graph = CapturingGraph()
    monkeypatch.setattr(runtime, "build_graph", lambda _: graph)
    conversation_id = uuid.uuid4()
    run_id = uuid.uuid4()

    result = await runtime.run(
        toolbox=None,  # type: ignore[arg-type]
        history=[],
        current_message="Recommend ginger tea.",
        conversation_id=conversation_id,
        user_id=uuid.uuid4(),
        run_id=run_id,
    )

    assert result.text == "Catalog-grounded answer."
    assert graph.config is not None
    assert graph.config["run_name"] == "ask-ember-turn"
    assert graph.config["metadata"] == {
        "agent_run_id": str(run_id),
        "conversation_id": str(conversation_id),
        "model": "gemini-3.5-flash-lite",
    }
    assert "user_id" not in graph.config["metadata"]
    assert "environment:test" in graph.config["tags"]


@pytest.mark.anyio
async def test_langgraph_runtime_reports_provider_timeout_without_money_action(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    runtime = LangGraphShoppingRuntime(
        api_key="test-only-key",
        model_name="gemini-3.5-flash-lite",
        timeout_seconds=30,
        max_output_characters=6000,
    )
    graph = CapturingGraph(timeout=True)
    monkeypatch.setattr(runtime, "build_graph", lambda _: graph)

    with pytest.raises(DomainError) as failure:
        await runtime.run(
            toolbox=None,  # type: ignore[arg-type]
            history=[],
            current_message="Recommend ginger tea.",
            conversation_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            run_id=uuid.uuid4(),
        )

    assert failure.value.code == "agent_provider_timeout"
    assert failure.value.status_code == 503
    assert "No payment or approval was made" in failure.value.message
    assert "Ask Ember provider timeout" in caplog.text
