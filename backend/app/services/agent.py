from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.agents.guardrails import AgentOutputGuard
from app.agents.policy import (
    MUTATING_TOOLS,
    SCHEDULE_DRAFT_STATE_VERSION,
    AgentActionPolicy,
    AgentPolicyDecision,
    FulfillmentClarification,
)
from app.agents.runtime import AgentRuntimeResult, ShoppingAgentRuntime
from app.agents.state import GRAPH_VERSION
from app.agents.tools import AgentToolbox
from app.core.config import get_settings
from app.core.errors import ConflictError, DomainError, NotFoundError
from app.db.models import (
    AgentConversation,
    AgentMessage,
    AgentRun,
    AgentToolCall,
    AuditEvent,
    Merchant,
    UserAccount,
)
from app.schemas.agent import (
    AgentConversationOut,
    AgentMessageOut,
    AgentTurnOut,
)
from app.services.agent_memory import AgentMemoryService


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
        policy_decision: AgentPolicyDecision | None = None
        pending_fulfillment: FulfillmentClarification | None = None
        pending_schedule_draft = False
        history: list[tuple[str, str]] = []
        merchant_slug = ""
        merchant_id: uuid.UUID | None = None
        run_id: uuid.UUID | None = None
        source_message_id: uuid.UUID | None = None

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
                recovered = self._recover_verified_mutation(existing, conversation, merchant.id)
                if recovered is not None:
                    return recovered
                raise ConflictError(
                    "agent_run_in_progress" if existing.status == "running" else "agent_run_failed",
                    "This message is already being processed."
                    if existing.status == "running"
                    else "The previous attempt failed safely. Send again to retry.",
                )
            recent_turns = self.db.scalar(
                select(func.count())
                .select_from(AgentRun)
                .where(
                    AgentRun.customer_id == customer.id,
                    AgentRun.started_at >= utc_now() - timedelta(minutes=1),
                )
            )
            if int(recent_turns or 0) >= self.settings.agent_max_turns_per_minute:
                raise DomainError(
                    "agent_rate_limit_exceeded",
                    "Too many agent turns were started. Wait a minute and try again.",
                    429,
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
            if messages and messages[0].role == "assistant":
                pending_fulfillment = FulfillmentClarification.from_structured_content(
                    messages[0].structured_content
                )
                schedule_state = messages[0].structured_content.get("schedule_draft_pending")
                pending_schedule_draft = bool(
                    isinstance(schedule_state, dict)
                    and schedule_state.get("version") == SCHEDULE_DRAFT_STATE_VERSION
                )
            policy_decision = AgentActionPolicy.classify(
                content,
                max_tool_calls=self.settings.agent_max_tool_calls,
                max_mutations=self.settings.agent_max_mutations_per_turn,
                pending_fulfillment=pending_fulfillment,
                pending_schedule_draft=pending_schedule_draft,
            )
            run = AgentRun(
                conversation_id=conversation.id,
                customer_id=customer.id,
                idempotency_key=idempotency_key,
                request_sha256=request_sha256,
                model=self.runtime.model_name,
                graph_version=GRAPH_VERSION,
                policy_version=policy_decision.policy_version,
                intent=policy_decision.intent.value,
                risk_level=policy_decision.risk_level.value,
                allowed_tools=sorted(policy_decision.allowed_tools),
                checkpoint_state={
                    "version": "agent-checkpoint-1",
                    "phase": "policy_ready",
                    "updated_at": utc_now().isoformat(),
                },
                status="running",
            )
            self.db.add(run)
            self.db.flush()
            run_id = run.id
            source_message = AgentMessage(
                conversation_id=conversation.id,
                run_id=run.id,
                role="user",
                content=content,
                structured_content={},
            )
            self.db.add(source_message)
            self.db.flush()
            source_message_id = source_message.id
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
                        "graph_version": run.graph_version,
                        "policy_version": run.policy_version,
                        "intent": run.intent,
                        "risk_level": run.risk_level,
                    },
                )
            )
            self.db.add(
                AuditEvent(
                    merchant_id=merchant.id,
                    actor_type="policy",
                    actor_id=policy_decision.policy_version,
                    event_type="agent.policy.evaluated",
                    aggregate_type="agent_conversation",
                    aggregate_id=str(conversation.id),
                    payload={
                        "run_id": str(run.id),
                        **policy_decision.as_dict(),
                        "request_sha256": request_sha256,
                    },
                )
            )

        if (
            merchant_id is None
            or run_id is None
            or source_message_id is None
            or policy_decision is None
        ):
            raise DomainError("agent_run_not_started", "Agent run could not be started.", 500)
        try:
            memory_service = AgentMemoryService(self.db)
            memory_activity = memory_service.apply_explicit_message(
                customer,
                merchant_slug,
                content,
                source_message_id,
            )
            memories = memory_service.preferences_for_prompt(customer, merchant_slug)
        except DomainError as error:
            self._fail_run(conversation_id, run_id, merchant_id, error.code)
            raise
        except Exception as error:
            self._fail_run(conversation_id, run_id, merchant_id, "agent_memory_failed")
            raise DomainError(
                "agent_memory_failed",
                "Ask Ember could not load preferences safely. No commerce action was made.",
                503,
            ) from error
        toolbox = AgentToolbox(
            db=self.db,
            customer=customer,
            merchant_id=merchant_id,
            merchant_slug=merchant_slug,
            conversation_id=conversation_id,
            run_id=run_id,
            policy_decision=policy_decision,
            current_message=content,
            pending_fulfillment=pending_fulfillment,
        )
        toolbox.memory_activity = memory_activity
        if (
            policy_decision.fulfillment_selection is not None
            and policy_decision.fulfillment_selection.purpose == "checkout"
        ):
            result = self._resolve_contextual_fulfillment(toolbox, pending_fulfillment)
            return self._complete_run(conversation_id, run_id, merchant_id, toolbox, result)
        try:
            result = await self.runtime.run(
                toolbox=toolbox,
                history=history,
                current_message=content,
                conversation_id=conversation_id,
                user_id=customer.id,
                run_id=run_id,
                memories=memories,
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

    @staticmethod
    def _resolve_contextual_fulfillment(
        toolbox: AgentToolbox,
        clarification: FulfillmentClarification | None,
    ) -> AgentRuntimeResult:
        selection = toolbox.policy_decision.fulfillment_selection
        if selection is None:
            raise DomainError(
                "agent_fulfillment_context_missing",
                "The fulfillment selection could not be resolved safely.",
                409,
            )
        listed = toolbox.list_fulfillment_destinations()
        if listed.get("status") != "success":
            error = listed.get("error", {})
            return AgentRuntimeResult(
                text=(
                    f"{error.get('message', 'I could not refresh the pickup locations.')} "
                    "No checkout, approval, or payment was made."
                ),
                model="deterministic-policy",
            )
        destinations = listed.get("destinations", {})
        address_id = ""
        location_id = ""
        destination_label = selection.label
        destination_is_default = selection.resolution.startswith("defaulted_")
        if selection.fulfillment_type == "pickup":
            location_id = selection.destination_id
            selected_location = next(
                (
                    location
                    for location in destinations.get("pickup_locations", [])
                    if str(location.get("id")) == selection.destination_id
                ),
                None,
            )
            if selected_location is not None:
                destination_label = str(selected_location.get("name") or selection.label)
                destination_is_default = bool(selected_location.get("is_default"))
        else:
            address_id = selection.destination_id
            selected_address = next(
                (
                    address
                    for address in destinations.get("delivery_addresses", [])
                    if str(address.get("id")) == selection.destination_id
                ),
                None,
            )
            if selected_address is not None:
                address_parts = [
                    str(selected_address.get("label") or "saved"),
                    str(selected_address.get("city") or ""),
                    str(selected_address.get("postal_code") or ""),
                ]
                destination_label = ", ".join(part for part in address_parts if part)
                destination_is_default = bool(selected_address.get("is_default"))
        checkout = toolbox.prepare_checkout(
            selection.fulfillment_type,
            address_id,
            location_id,
        )
        if checkout.get("status") != "success":
            error = checkout.get("error", {})
            return AgentRuntimeResult(
                text=(
                    f"{error.get('message', 'I could not prepare checkout safely.')} "
                    "No approval or payment was made."
                ),
                model="deterministic-policy",
            )
        alternatives = (
            [
                option.label
                for option in clarification.options
                if option.destination_id != selection.destination_id
            ]
            if clarification is not None
            else []
        )
        if selection.fulfillment_type != "pickup":
            default_note = "default " if destination_is_default else ""
            text = (
                f"Got it — I prepared local delivery to your {default_note}{destination_label} "
                "address. Review the exact checkout quote below; no approval or payment has "
                "been made."
            )
        elif selection.resolution == "defaulted_pickup":
            alternative_note = (
                f" Tell me before approval if you prefer {', '.join(alternatives)} instead."
                if alternatives
                else ""
            )
            text = (
                f"Got it — I used {selection.label} as the default pickup location."
                f"{alternative_note} Review the exact checkout quote below; no approval or "
                "payment has been made."
            )
        else:
            text = (
                f"Got it — I prepared pickup at {selection.label}. Review the exact checkout "
                "quote below; no approval or payment has been made."
            )
        return AgentRuntimeResult(text=text, model="deterministic-policy")

    def _complete_run(
        self,
        conversation_id: uuid.UUID,
        run_id: uuid.UUID,
        merchant_id: uuid.UUID,
        toolbox: AgentToolbox,
        result: AgentRuntimeResult,
    ) -> AgentTurnOut:
        structured_content = toolbox.structured_content(result.text)
        response_text, ui_claim_corrected = AgentOutputGuard.reconcile_ui_claims(
            result.text,
            structured_content,
        )
        if ui_claim_corrected:
            structured_content["guardrail"] = {"ui_claim_corrected": True}
        response_sha256 = hashlib.sha256(response_text.encode()).hexdigest()
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
                content=response_text,
                structured_content=structured_content,
                model=result.model,
            )
            self.db.add(message)
            run.status = "completed"
            run.model = result.model
            run.response_sha256 = response_sha256
            run.tool_call_count = toolbox.tool_call_count
            run.mutation_count = toolbox.mutation_count
            run.checkpoint_state = {
                "version": "agent-checkpoint-1",
                "phase": "completed",
                "updated_at": utc_now().isoformat(),
            }
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
                        "mutation_count": toolbox.mutation_count,
                        "model": result.model,
                        "graph_version": run.graph_version,
                        "policy_version": run.policy_version,
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
                run.checkpoint_state = {
                    "version": "agent-checkpoint-1",
                    "phase": "failed",
                    "error_code": error_code,
                    "updated_at": utc_now().isoformat(),
                }
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
        return any(
            outcome["tool"] in MUTATING_TOOLS and outcome["status"] == "success"
            for outcome in toolbox.outcomes
        )

    def _recover_verified_mutation(
        self,
        run: AgentRun,
        conversation: AgentConversation,
        merchant_id: uuid.UUID,
    ) -> AgentTurnOut | None:
        if run.status != "running" or not self._run_is_stale(run):
            return None
        call = self.db.scalar(
            select(AgentToolCall)
            .where(
                AgentToolCall.run_id == run.id,
                AgentToolCall.tool_name.in_(MUTATING_TOOLS),
                AgentToolCall.status == "completed",
            )
            .order_by(AgentToolCall.completed_at.desc())
            .limit(1)
        )
        if call is None or not call.output_payload:
            return None
        verification = call.output_payload.get("verification", {})
        if (
            call.output_payload.get("status") != "success"
            or verification.get("status") != "verified"
        ):
            return None
        text = (
            "I recovered a verified commerce action from an interrupted response. "
            "Review the exact cart, checkout, or schedule below before doing anything again."
        )
        message = AgentMessage(
            conversation_id=conversation.id,
            run_id=run.id,
            role="assistant",
            content=text,
            structured_content=self._structured_content_from_recovery(call),
            model=run.model,
        )
        self.db.add(message)
        run.status = "completed"
        run.response_sha256 = hashlib.sha256(text.encode()).hexdigest()
        run.completed_at = utc_now()
        run.checkpoint_state = {
            "version": "agent-checkpoint-1",
            "phase": "recovered_after_verified_mutation",
            "last_tool_call_id": str(call.id),
            "updated_at": utc_now().isoformat(),
        }
        conversation.last_activity_at = utc_now()
        self.db.add(
            AuditEvent(
                merchant_id=merchant_id,
                actor_type="system",
                actor_id=None,
                event_type="agent.run.recovered",
                aggregate_type="agent_conversation",
                aggregate_id=str(conversation.id),
                payload={
                    "run_id": str(run.id),
                    "tool_call_id": str(call.id),
                    "tool_name": call.tool_name,
                    "recovery_reason": "verified_mutation_missing_response",
                },
            )
        )
        self.db.flush()
        return AgentTurnOut(
            conversation_id=conversation.id,
            run_id=run.id,
            message=self._message_out(message),
        )

    def _run_is_stale(self, run: AgentRun) -> bool:
        started_at = run.started_at
        if started_at.tzinfo is None:
            started_at = started_at.replace(tzinfo=UTC)
        stale_after = self.settings.agent_timeout_seconds + 5
        return (utc_now() - started_at).total_seconds() > stale_after

    @staticmethod
    def _structured_content_from_recovery(call: AgentToolCall) -> dict[str, object]:
        payload = call.output_payload or {}
        content: dict[str, object] = {
            "activity": [
                {
                    "tool": call.tool_name,
                    "status": "success",
                    "label": "Recovered and verified commerce action",
                }
            ],
            "recovered": True,
        }
        for key in ("cart", "checkout", "scheduled_purchase"):
            if key in payload:
                content[key] = payload[key]
        return content

    def _merchant(self, slug: str) -> Merchant:
        merchant = self.db.scalar(select(Merchant).where(Merchant.slug == slug))
        if merchant is None:
            raise NotFoundError("merchant_not_found", "Merchant was not found.")
        return merchant
