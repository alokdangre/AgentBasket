import uuid

from fastapi import APIRouter, Depends, Header
from sqlalchemy.orm import Session

from app.agents.runtime import ShoppingAgentRuntime, get_agent_runtime
from app.api.dependencies import get_current_user
from app.core.database import get_db
from app.db.models import UserAccount
from app.schemas.agent import (
    AgentConversationCreate,
    AgentConversationOut,
    AgentMessageCreate,
    AgentTurnOut,
)
from app.services.agent import ConversationalAgentService

router = APIRouter(prefix="/agent/conversations", tags=["conversational-agent"])


@router.post("/current", response_model=AgentConversationOut)
def current_conversation(
    payload: AgentConversationCreate,
    user: UserAccount = Depends(get_current_user),
    db: Session = Depends(get_db),
    runtime: ShoppingAgentRuntime = Depends(get_agent_runtime),
) -> AgentConversationOut:
    return ConversationalAgentService(db, runtime).current_conversation(user, payload.merchant_slug)


@router.post("", response_model=AgentConversationOut, status_code=201)
def create_conversation(
    payload: AgentConversationCreate,
    user: UserAccount = Depends(get_current_user),
    db: Session = Depends(get_db),
    runtime: ShoppingAgentRuntime = Depends(get_agent_runtime),
) -> AgentConversationOut:
    return ConversationalAgentService(db, runtime).create_conversation(user, payload.merchant_slug)


@router.get("/{conversation_id}", response_model=AgentConversationOut)
def get_conversation(
    conversation_id: uuid.UUID,
    user: UserAccount = Depends(get_current_user),
    db: Session = Depends(get_db),
    runtime: ShoppingAgentRuntime = Depends(get_agent_runtime),
) -> AgentConversationOut:
    return ConversationalAgentService(db, runtime).get_conversation(conversation_id, user)


@router.post("/{conversation_id}/messages", response_model=AgentTurnOut)
async def send_message(
    conversation_id: uuid.UUID,
    payload: AgentMessageCreate,
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=8, max_length=128),
    user: UserAccount = Depends(get_current_user),
    db: Session = Depends(get_db),
    runtime: ShoppingAgentRuntime = Depends(get_agent_runtime),
) -> AgentTurnOut:
    return await ConversationalAgentService(db, runtime).send_message(
        conversation_id,
        payload.content,
        idempotency_key,
        user,
    )
