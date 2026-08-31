from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass
from functools import lru_cache
from typing import Annotated, Protocol, TypedDict

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.tools import BaseTool, StructuredTool
from langchain_google_genai import ChatGoogleGenerativeAI
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages

from app.agents.prompt import SYSTEM_INSTRUCTION
from app.agents.tools import AgentToolbox
from app.core.config import get_settings
from app.core.errors import DomainError


class ShoppingGraphState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]


@dataclass(frozen=True)
class AgentRuntimeResult:
    text: str
    model: str


class ShoppingAgentRuntime(Protocol):
    @property
    def model_name(self) -> str: ...

    async def run(
        self,
        *,
        toolbox: AgentToolbox,
        history: list[tuple[str, str]],
        current_message: str,
        conversation_id: uuid.UUID,
        user_id: uuid.UUID,
        run_id: uuid.UUID,
    ) -> AgentRuntimeResult: ...


class LangGraphShoppingRuntime:
    """Bounded LangGraph tool loop; the commerce database is the durable authority."""

    def __init__(
        self,
        api_key: str,
        model_name: str,
        timeout_seconds: float,
        max_output_characters: int,
    ) -> None:
        self._model_name = model_name
        self._timeout_seconds = timeout_seconds
        self._max_output_characters = max_output_characters
        self._model = ChatGoogleGenerativeAI(
            model=model_name,
            google_api_key=api_key,
            temperature=0.2,
            max_output_tokens=1200,
        )

    @property
    def model_name(self) -> str:
        return self._model_name

    @staticmethod
    def build_tools(toolbox: AgentToolbox) -> list[BaseTool]:
        return [
            StructuredTool.from_function(
                func=tool,
                name=tool.__name__,
                description=(tool.__doc__ or "Bounded commerce operation.").strip(),
            )
            for tool in toolbox.commerce_tools()
        ]

    def build_graph(self, toolbox: AgentToolbox):
        tools = self.build_tools(toolbox)
        tools_by_name = {tool.name: tool for tool in tools}
        model = self._model.bind_tools(tools)

        async def call_model(state: ShoppingGraphState) -> dict[str, list[BaseMessage]]:
            response = await model.ainvoke(state["messages"])
            return {"messages": [response]}

        async def call_tools(state: ShoppingGraphState) -> dict[str, list[BaseMessage]]:
            message = state["messages"][-1]
            if not isinstance(message, AIMessage):
                return {"messages": []}
            results: list[BaseMessage] = []
            for call in message.tool_calls:
                tool = tools_by_name.get(call["name"])
                if tool is None:
                    result: object = {
                        "status": "error",
                        "error": {"code": "unknown_tool", "message": "Tool is not allowed."},
                    }
                else:
                    result = tool.invoke(call.get("args", {}))
                results.append(
                    ToolMessage(
                        content=json.dumps(result, sort_keys=True, default=str),
                        tool_call_id=call["id"],
                        name=call["name"],
                    )
                )
            return {"messages": results}

        def route(state: ShoppingGraphState) -> str:
            message = state["messages"][-1]
            return "tools" if isinstance(message, AIMessage) and message.tool_calls else END

        graph = StateGraph(ShoppingGraphState)
        graph.add_node("assistant", call_model)
        graph.add_node("tools", call_tools)
        graph.add_edge(START, "assistant")
        graph.add_conditional_edges("assistant", route, {"tools": "tools", END: END})
        graph.add_edge("tools", "assistant")
        return graph.compile()

    async def run(
        self,
        *,
        toolbox: AgentToolbox,
        history: list[tuple[str, str]],
        current_message: str,
        conversation_id: uuid.UUID,
        user_id: uuid.UUID,
        run_id: uuid.UUID,
    ) -> AgentRuntimeResult:
        del conversation_id, user_id, run_id
        messages: list[BaseMessage] = [SystemMessage(content=SYSTEM_INSTRUCTION)]
        for role, content in history:
            messages.append(
                HumanMessage(content=content) if role == "user" else AIMessage(content=content)
            )
        messages.append(HumanMessage(content=current_message))
        try:
            async with asyncio.timeout(self._timeout_seconds):
                state = await self.build_graph(toolbox).ainvoke(
                    {"messages": messages},
                    config={"recursion_limit": 16},
                )
        except Exception as error:
            raise DomainError(
                "agent_temporarily_unavailable",
                "Ask Ember is temporarily unavailable. No payment or approval was made.",
                503,
            ) from error
        final = state["messages"][-1]
        text = self._message_text(final).strip()
        if not text:
            raise DomainError(
                "agent_empty_response",
                "Ask Ember could not produce a response. No payment or approval was made.",
                503,
            )
        return AgentRuntimeResult(
            text=text[: self._max_output_characters],
            model=self._model_name,
        )

    @staticmethod
    def _message_text(message: BaseMessage) -> str:
        if isinstance(message.content, str):
            return message.content
        return "".join(
            str(block.get("text", ""))
            for block in message.content
            if isinstance(block, dict) and block.get("type") == "text"
        )


class UnavailableShoppingRuntime:
    model_name = "not-configured"

    async def run(self, **_: object) -> AgentRuntimeResult:
        raise DomainError(
            "agent_not_configured",
            "Ask Ember needs GOOGLE_API_KEY before it can answer.",
            503,
        )


@lru_cache
def get_agent_runtime() -> ShoppingAgentRuntime:
    settings = get_settings()
    if settings.google_api_key is None:
        return UnavailableShoppingRuntime()
    return LangGraphShoppingRuntime(
        api_key=settings.google_api_key.get_secret_value(),
        model_name=settings.agent_model,
        timeout_seconds=settings.agent_timeout_seconds,
        max_output_characters=settings.agent_max_output_characters,
    )
