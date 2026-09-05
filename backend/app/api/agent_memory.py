from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.dependencies import get_current_user
from app.core.database import get_db
from app.db.models import UserAccount
from app.schemas.agent import (
    AgentMemoryListOut,
    AgentMemoryMutationOut,
    AgentMemorySettingUpdate,
    AgentMemoryWrite,
)
from app.services.agent_memory import AgentMemoryService

router = APIRouter(prefix="/agent/memory", tags=["agent-memory"])


@router.get("/{merchant_slug}", response_model=AgentMemoryListOut)
def list_memories(
    merchant_slug: str,
    user: UserAccount = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> AgentMemoryListOut:
    return AgentMemoryService(db).list(user, merchant_slug)


@router.patch("/{merchant_slug}", response_model=AgentMemoryMutationOut)
def update_memory_setting(
    merchant_slug: str,
    payload: AgentMemorySettingUpdate,
    user: UserAccount = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> AgentMemoryMutationOut:
    return AgentMemoryService(db).set_enabled(user, merchant_slug, payload.enabled)


@router.put("/{merchant_slug}/{kind}", response_model=AgentMemoryMutationOut)
def save_memory(
    merchant_slug: str,
    kind: str,
    payload: AgentMemoryWrite,
    user: UserAccount = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> AgentMemoryMutationOut:
    return AgentMemoryService(db).upsert(
        user,
        merchant_slug,
        kind,
        payload.value,
        expires_in_days=payload.expires_in_days,
    )


@router.delete("/{merchant_slug}/{kind}", response_model=AgentMemoryMutationOut)
def delete_memory(
    merchant_slug: str,
    kind: str,
    user: UserAccount = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> AgentMemoryMutationOut:
    return AgentMemoryService(db).revoke(user, merchant_slug, kind)
