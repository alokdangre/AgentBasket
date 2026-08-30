import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator


class AgentConversationCreate(BaseModel):
    merchant_slug: str = Field(default="ember-and-leaf", min_length=1, max_length=80)


class AgentMessageCreate(BaseModel):
    content: str = Field(min_length=1, max_length=2000)

    @field_validator("content")
    @classmethod
    def normalize_content(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("Message cannot be empty")
        return normalized


class AgentMessageOut(BaseModel):
    id: uuid.UUID
    role: str
    content: str
    structured_content: dict[str, Any]
    model: str | None
    created_at: datetime


class AgentConversationOut(BaseModel):
    id: uuid.UUID
    merchant_slug: str
    title: str
    status: str
    messages: list[AgentMessageOut]
    created_at: datetime
    last_activity_at: datetime


class AgentTurnOut(BaseModel):
    conversation_id: uuid.UUID
    run_id: uuid.UUID
    message: AgentMessageOut
