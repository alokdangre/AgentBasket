from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.runtime import AgentRuntimeResult, ShoppingAgentRuntime
from app.agents.tools import AgentToolbox
from app.core.config import get_settings
from app.core.errors import ConflictError, DomainError, NotFoundError
from app.db.models import (
    AgentConversation,
    AgentMessage,
    AgentRun,
    AuditEvent,
    Merchant,
    UserAccount,
)
from app.schemas.agent import (
    AgentConversationOut,
    AgentMessageOut,
    AgentTurnOut,
)


def utc_now() -> datetime:
    return datetime.now(UTC)


class ConversationalAgentService:
    def __init__(self, db: Session, runtime: ShoppingAgentRuntime) -> None:
        self.db = db
        self.runtime = runtime
        self.settings = get_settings()

    def create_conversation(
        self, customer: UserAccount, merchant_slug: str
    ) -> AgentConversationOut:
        with self.db.begin():
            merchant = self._merchant(merchant_slug)
            conversation = self._new_conversation(customer, merchant)
            return self._conversation_out(conversation, merchant.slug)

    def current_conversation(
        self, customer: UserAccount, merchant_slug: str
    ) -> AgentConversationOut:
        with self.db.begin():
            merchant = self._merchant(merchant_slug)
            conversation = self.db.scalar(
                select(AgentConversation)
                .where(
                    AgentConversation.customer_id == customer.id,
                    AgentConversation.merchant_id == merchant.id,
                    AgentConversation.status == "active",
                )
                .order_by(AgentConversation.last_activity_at.desc())
                .limit(1)
            )
            if conversation is None:
                conversation = self._new_conversation(customer, merchant)
            return self._conversation_out(conversation, merchant.slug)

    def get_conversation(
        self, conversation_id: uuid.UUID, customer: UserAccount
    ) -> AgentConversationOut:
        conversation, merchant_slug = self._owned_conversation(conversation_id, customer.id)
        output = self._conversation_out(conversation, merchant_slug)
        self.db.rollback()
        return output

    async def send_message(
        self,
        conversation_id: uuid.UUID,
        content: str,
        idempotency_key: str,
        customer: UserAccount,
    ) -> AgentTurnOut:
        request_sha256 = hashlib.sha256(content.encode()).hexdigest()
        history: list[tuple[str, str]] = []
        merchant_slug = ""
        merchant_id: uuid.UUID | None = None
        run_id: uuid.UUID | None = None

        with self.db.begin():
            conversation = self.db.scalar(
                select(AgentConversation)
                .where(
                    AgentConversation.id == conversation_id,
                    AgentConversation.customer_id == customer.id,
                    AgentConversation.status == "active",
                )
                .with_for_update()
            )
            if conversation is None:
                raise NotFoundError("agent_conversation_not_found", "Conversation was not found.")
            merchant = self.db.get(Merchant, conversation.merchant_id)
            if merchant is None:
                raise NotFoundError("merchant_not_found", "Merchant was not found.")
            merchant_slug = merchant.slug
            merchant_id = merchant.id
            existing = self.db.scalar(
                select(AgentRun).where(
                    AgentRun.conversation_id == conversation.id,
                    AgentRun.idempotency_key == idempotency_key,
                )
            )
            if existing is not None:
                if existing.request_sha256 != request_sha256:
                    raise ConflictError(
                        "idempotency_key_reused",
                        "Idempotency-Key was already used for a different message.",
                    )
                if existing.status == "completed":
                    response = self.db.scalar(
                        select(AgentMessage).where(
                            AgentMessage.run_id == existing.id,
                            AgentMessage.role == "assistant",
                        )
                    )
                    if response is None:
                        raise ConflictError(
                            "agent_response_missing", "The completed response could not be found."
                        )
                    return AgentTurnOut(
                        conversation_id=conversation.id,
                        run_id=existing.id,
                        message=self._message_out(response),
                    )
                raise ConflictError(
                    "agent_run_in_progress" if existing.status == "running" else "agent_run_failed",
                    "This message is already being processed."
                    if existing.status == "running"
                    else "The previous attempt failed safely. Send again to retry.",
                )
            running = self.db.scalar(
                select(AgentRun.id).where(
                    AgentRun.conversation_id == conversation.id,
                    AgentRun.status == "running",
                )
            )
            if running is not None:
                raise ConflictError(
                    "agent_run_in_progress", "Wait for the current response before sending another."
                )
            messages = list(
                self.db.scalars(
                    select(AgentMessage)
                    .where(AgentMessage.conversation_id == conversation.id)
                    .order_by(AgentMessage.created_at.desc())
                    .limit(self.settings.agent_max_history_messages)
                )
            )
            history = [(message.role, message.content[:2000]) for message in reversed(messages)]
            run = AgentRun(
                conversation_id=conversation.id,
                customer_id=customer.id,
                idempotency_key=idempotency_key,
                request_sha256=request_sha256,
                model=self.runtime.model_name,
                status="running",
            )
            self.db.add(run)
            self.db.flush()
            run_id = run.id
            self.db.add(
                AgentMessage(
                    conversation_id=conversation.id,
                    run_id=run.id,
                    role="user",
                    content=content,
                    structured_content={},
                )
            )
            if len(messages) <= 1:
                conversation.title = content[:80]
            conversation.last_activity_at = utc_now()
            self.db.add(
                AuditEvent(
                    merchant_id=merchant.id,
                    actor_type="customer",
                    actor_id=str(customer.id),
                    event_type="agent.run.started",
                    aggregate_type="agent_conversation",
                    aggregate_id=str(conversation.id),
                    payload={
                        "run_id": str(run.id),
                        "request_sha256": request_sha256,
                        "model": run.model,
                    },
                )
            )

        if merchant_id is None or run_id is None:
            raise DomainError("agent_run_not_started", "Agent run could not be started.", 500)
        toolbox = AgentToolbox(
            db=self.db,
            customer=customer,
            merchant_id=merchant_id,
            merchant_slug=merchant_slug,
            conversation_id=conversation_id,
            run_id=run_id,
        )
        try:
            result = await self.runtime.run(
                toolbox=toolbox,
                history=history,
                current_message=content,
                conversation_id=conversation_id,
                user_id=customer.id,
                run_id=run_id,
            )
        except DomainError as error:
            if self._has_committed_mutation(toolbox):
                fallback = AgentRuntimeResult(
                    text=(
                        "I completed the commerce action, but my explanation was interrupted. "
                        "Review the cart or exact checkout below before doing anything again."
                    ),
                    model=self.runtime.model_name,
                )
                return self._complete_run(conversation_id, run_id, merchant_id, toolbox, fallback)
            self._fail_run(conversation_id, run_id, merchant_id, error.code)
            raise
        except Exception as error:
            if self._has_committed_mutation(toolbox):
                fallback = AgentRuntimeResult(
                    text=(
                        "I completed the commerce action, but my explanation was interrupted. "
                        "Review the cart or exact checkout below before doing anything again."
                    ),
                    model=self.runtime.model_name,
                )
                return self._complete_run(conversation_id, run_id, merchant_id, toolbox, fallback)
            self._fail_run(conversation_id, run_id, merchant_id, "agent_execution_failed")
            raise DomainError(
                "agent_execution_failed",
                "Ask Ember is temporarily unavailable. No payment or approval was made.",
                503,
            ) from error
        return self._complete_run(conversation_id, run_id, merchant_id, toolbox, result)

    def _complete_run(
        self,
        conversation_id: uuid.UUID,
        run_id: uuid.UUID,
        merchant_id: uuid.UUID,
        toolbox: AgentToolbox,
        result: AgentRuntimeResult,
    ) -> AgentTurnOut:
        structured_content = toolbox.structured_content()
        response_sha256 = hashlib.sha256(result.text.encode()).hexdigest()
        self.db.rollback()
        with self.db.begin():
            run = self.db.get(AgentRun, run_id)
            conversation = self.db.get(AgentConversation, conversation_id)
            if run is None or conversation is None:
                raise DomainError("agent_run_not_found", "Agent run could not be completed.", 500)
            message = AgentMessage(
                conversation_id=conversation_id,
                run_id=run_id,
                role="assistant",
                content=result.text,
                structured_content=structured_content,
                model=result.model,
            )
            self.db.add(message)
            run.status = "completed"
            run.model = result.model
            run.response_sha256 = response_sha256
            run.completed_at = utc_now()
            conversation.last_activity_at = utc_now()
            self.db.add(
                AuditEvent(
                    merchant_id=merchant_id,
                    actor_type="agent",
                    actor_id=str(run_id),
                    event_type="agent.run.completed",
                    aggregate_type="agent_conversation",
                    aggregate_id=str(conversation_id),
                    payload={
                        "run_id": str(run_id),
                        "response_sha256": response_sha256,
                        "tool_count": len(toolbox.outcomes),
                        "model": result.model,
                    },
                )
            )
            self.db.flush()
            return AgentTurnOut(
                conversation_id=conversation_id,
                run_id=run_id,
                message=self._message_out(message),
            )

    def _fail_run(
        self,
        conversation_id: uuid.UUID,
        run_id: uuid.UUID,
        merchant_id: uuid.UUID,
        error_code: str,
    ) -> None:
        self.db.rollback()
        with self.db.begin():
            run = self.db.get(AgentRun, run_id)
            if run is not None:
                run.status = "failed"
                run.error_code = error_code
                run.completed_at = utc_now()
            self.db.add(
                AuditEvent(
                    merchant_id=merchant_id,
                    actor_type="agent",
                    actor_id=str(run_id),
                    event_type="agent.run.failed",
                    aggregate_type="agent_conversation",
                    aggregate_id=str(conversation_id),
                    payload={"run_id": str(run_id), "error_code": error_code},
                )
            )

    def _new_conversation(self, customer: UserAccount, merchant: Merchant) -> AgentConversation:
        conversation = AgentConversation(
            merchant_id=merchant.id,
            customer_id=customer.id,
            title="Shopping with Ember",
            status="active",
        )
        self.db.add(conversation)
        self.db.flush()
        self.db.add(
            AgentMessage(
                conversation_id=conversation.id,
                role="assistant",
                content=(
                    "Hi, I’m Ember. Tell me what you’re in the mood for, your budget, "
                    "or how you brew—and I’ll use the live catalog to help."
                ),
                structured_content={
                    "suggestions": [
                        "Something refreshing under ₹300",
                        "Coffee for a V60",
                        "A floral tea",
                    ]
                },
                model=None,
            )
        )
        self.db.flush()
        return conversation

    def _owned_conversation(
        self, conversation_id: uuid.UUID, customer_id: uuid.UUID
    ) -> tuple[AgentConversation, str]:
        row = self.db.execute(
            select(AgentConversation, Merchant.slug)
            .join(Merchant, Merchant.id == AgentConversation.merchant_id)
            .where(
                AgentConversation.id == conversation_id,
                AgentConversation.customer_id == customer_id,
            )
        ).one_or_none()
        if row is None:
            raise NotFoundError("agent_conversation_not_found", "Conversation was not found.")
        return row[0], row[1]

    def _conversation_out(
        self, conversation: AgentConversation, merchant_slug: str
    ) -> AgentConversationOut:
        messages = list(
            self.db.scalars(
                select(AgentMessage)
                .where(AgentMessage.conversation_id == conversation.id)
                .order_by(AgentMessage.created_at)
            )
        )
        return AgentConversationOut(
            id=conversation.id,
            merchant_slug=merchant_slug,
            title=conversation.title,
            status=conversation.status,
            messages=[self._message_out(message) for message in messages],
            created_at=conversation.created_at,
            last_activity_at=conversation.last_activity_at,
        )

    @staticmethod
    def _message_out(message: AgentMessage) -> AgentMessageOut:
        return AgentMessageOut(
            id=message.id,
            role=message.role,
            content=message.content,
            structured_content=message.structured_content,
            model=message.model,
            created_at=message.created_at,
        )

    @staticmethod
    def _has_committed_mutation(toolbox: AgentToolbox) -> bool:
        mutation_tools = {
            "add_to_cart",
            "update_cart_item",
            "remove_cart_item",
            "prepare_checkout",
        }
        return any(
            outcome["tool"] in mutation_tools and outcome["status"] == "success"
            for outcome in toolbox.outcomes
        )

    def _merchant(self, slug: str) -> Merchant:
        merchant = self.db.scalar(select(Merchant).where(Merchant.slug == slug))
        if merchant is None:
            raise NotFoundError("merchant_not_found", "Merchant was not found.")
        return merchant
