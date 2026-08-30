from __future__ import annotations

import uuid
from dataclasses import dataclass
from functools import lru_cache
from typing import Protocol

from google import genai
from google.adk import Agent
from google.adk.models.google_llm import Gemini
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from app.agents.prompt import SYSTEM_INSTRUCTION, conversation_prompt
from app.agents.tools import AgentToolbox
from app.core.config import get_settings
from app.core.errors import DomainError

APP_NAME = "agentbasket_in_app_shopping"


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


class GoogleAdkShoppingRuntime:
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
        self._client = genai.Client(api_key=api_key)

    @property
    def model_name(self) -> str:
        return self._model_name

    def build_agent(self, toolbox: AgentToolbox) -> Agent:
        return Agent(
            name="ember_shopping_agent",
            description="Catalog-grounded shopping assistant for Ember & Leaf.",
            model=Gemini(model=self._model_name, client=self._client),
            instruction=SYSTEM_INSTRUCTION,
            tools=toolbox.adk_tools(),
            mode="chat",
            timeout=self._timeout_seconds,
            generate_content_config=types.GenerateContentConfig(
                temperature=0.2,
                max_output_tokens=1200,
            ),
        )

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
        session_service = InMemorySessionService()
        session_id = str(conversation_id)
        await session_service.create_session(
            app_name=APP_NAME,
            user_id=str(user_id),
            session_id=session_id,
            state={
                "authenticated_user_id": str(user_id),
                "merchant_slug": toolbox.merchant_slug,
                "agent_run_id": str(run_id),
            },
        )
        runner = Runner(
            agent=self.build_agent(toolbox),
            app_name=APP_NAME,
            session_service=session_service,
        )
        message = types.Content(
            role="user",
            parts=[types.Part(text=conversation_prompt(history, current_message))],
        )
        final_text = ""
        try:
            async for event in runner.run_async(
                user_id=str(user_id),
                session_id=session_id,
                invocation_id=str(run_id),
                new_message=message,
            ):
                if not event.is_final_response() or not event.content:
                    continue
                parts = event.content.parts or []
                text_parts = [part.text for part in parts if getattr(part, "text", None)]
                if text_parts:
                    final_text = "".join(text_parts).strip()
        except Exception as error:
            raise DomainError(
                "agent_temporarily_unavailable",
                "Ask Ember is temporarily unavailable. No payment or approval was made.",
                503,
            ) from error
        if not final_text:
            raise DomainError(
                "agent_empty_response",
                "Ask Ember could not produce a response. No payment or approval was made.",
                503,
            )
        return AgentRuntimeResult(
            text=final_text[: self._max_output_characters],
            model=self._model_name,
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
    return GoogleAdkShoppingRuntime(
        api_key=settings.google_api_key.get_secret_value(),
        model_name=settings.agent_model,
        timeout_seconds=settings.agent_timeout_seconds,
        max_output_characters=settings.agent_max_output_characters,
    )
