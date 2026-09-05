from __future__ import annotations

from typing import Annotated, TypedDict

from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages

GRAPH_SCHEMA_VERSION = "shopping-graph-state-2"
GRAPH_VERSION = "ask-ember-graph-2"


class ShoppingGraphState(TypedDict, total=False):
    messages: Annotated[list[BaseMessage], add_messages]
    schema_version: str
    run_id: str
    conversation_id: str
    customer_id: str
    merchant_id: str
    channel: str
    intent: str
    risk_level: str
    policy_version: str
    policy_reason_codes: list[str]
    allowed_tools: list[str]
    tool_call_count: int
    mutation_count: int
    last_verified_tool: str | None
    last_verification_status: str | None
    final_text: str
