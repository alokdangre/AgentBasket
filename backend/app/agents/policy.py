from __future__ import annotations

import re
import unicodedata
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Any


class AgentIntent(StrEnum):
    DISCOVER = "discover"
    CART_READ = "cart_read"
    FULFILLMENT_READ = "fulfillment_read"
    FULFILLMENT_SELECT = "fulfillment_select"
    CART_ADD = "cart_add"
    CART_UPDATE = "cart_update"
    CART_REMOVE = "cart_remove"
    CHECKOUT_PREPARE = "checkout_prepare"
    SCHEDULE_DRAFT = "schedule_draft"
    MEMORY_MANAGE = "memory_manage"
    ANSWER = "answer"


class AgentRiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


POLICY_VERSION = "agent-action-policy-5"
CLARIFICATION_VERSION = "agent-clarification-1"
SCHEDULE_DRAFT_STATE_VERSION = "agent-schedule-draft-1"
_CLARIFICATION_KINDS = frozenset({"fulfillment_method", "delivery_address", "pickup_location"})
_CLARIFICATION_PURPOSES = frozenset({"checkout", "schedule_draft"})

READ_TOOLS = frozenset(
    {
        "search_catalog",
        "recommend_products",
        "present_products",
        "get_cart",
        "list_fulfillment_destinations",
    }
)
CART_WRITE_TOOLS = frozenset({"add_to_cart", "update_cart_item", "remove_cart_item"})
CHECKOUT_TOOLS = frozenset({"prepare_checkout"})
SCHEDULE_TOOLS = frozenset({"list_scheduling_options", "draft_scheduled_purchase"})
MUTATING_TOOLS = CART_WRITE_TOOLS | {"prepare_checkout", "draft_scheduled_purchase"}
ALL_AGENT_TOOLS = READ_TOOLS | CART_WRITE_TOOLS | CHECKOUT_TOOLS | SCHEDULE_TOOLS

_DENIED_FINANCIAL_PATTERNS = (
    re.compile(r"\b(?:approve|authorize)\b.{0,40}\b(?:checkout|order|payment|purchase)\b", re.I),
    re.compile(r"\b(?:pay|charge|capture|refund|transfer)\b", re.I),
    re.compile(r"\b(?:open|launch|start)\b.{0,30}\brazorpay\b", re.I),
)
_ACKNOWLEDGEMENT_PREFIX = (
    r"(?:(?:yes|yeah|yep|okay|ok|sure|alright)\b|go\s+ahead\b)"
    r"\s*[,!.]?\s*(?:and\s+)?"
)
_ACTION_PREFIX = (
    rf"^(?:{_ACKNOWLEDGEMENT_PREFIX})*"
    r"(?:(?:please)\s+|(?:(?:can|could|would|will)\s+you\s+(?:please\s+)?)|"
    r"(?:i\s+(?:want|need)\s+(?:you\s+)?to\s+)|"
    r"(?:i(?:'d| would)\s+like\s+(?:you\s+)?to\s+))?"
)
_SCHEDULE_PATTERN = re.compile(
    _ACTION_PREFIX + r"(?:"
    r"(?:schedule|draft)\b|"
    r"(?:set\s+up|create)\b.{0,120}\b"
    r"(?:schedule|scheduled|recurring|reorder|daily|weekly|monthly|fortnightly)\b|"
    r"(?:reorder|repeat)\b.{0,120}\b(?:daily|weekly|monthly|fortnightly)\b"
    r")",
    re.I,
)
_CHECKOUT_PATTERN = re.compile(
    _ACTION_PREFIX + r"(?:"
    r"(?:prepare|create|start)\b.{0,40}\b(?:checkout|order)\b|"
    r"(?:checkout|check\s+out|buy|order)\b|"
    r"place\b.{0,20}\border\b"
    r")",
    re.I,
)
_ADD_PATTERN = re.compile(
    _ACTION_PREFIX + r"(?:add(?!\s+up\b)|put|include)\b",
    re.I,
)
_UPDATE_PATTERN = re.compile(
    _ACTION_PREFIX + r"(?:update|change|set|increase|decrease)\b.{0,80}\b"
    r"(?:cart|basket|quantity|qty|item|line|to|one|two|three|\d+)\b",
    re.I,
)
_REMOVE_PATTERN = re.compile(
    _ACTION_PREFIX + r"(?:remove|delete|take\s+out|drop)\b",
    re.I,
)
_CART_READ_PATTERN = re.compile(
    r"\b(?:show|view|what(?:'s| is)|check)\b.{0,40}\b(?:cart|basket)\b", re.I
)
_FULFILLMENT_SELECT_PATTERN = re.compile(
    _ACTION_PREFIX + r"(?:"
    r"(?:use|select|choose)\b.{0,80}\b(?:address(?:es)?|destination(?:s)?)\b|"
    r"(?:deliver|ship)\b.{0,80}\b(?:address|destination|there)\b"
    r")",
    re.I,
)
_FULFILLMENT_READ_PATTERN = re.compile(
    r"(?:\b(?:use|check|show|list|find|get)\b.{0,80}\b"
    r"(?:address(?:es)?|destination(?:s)?)\b|"
    r"\b(?:saved|registered|default)\s+"
    r"(?:(?:delivery|shipping)\s+)?address(?:es)?\b)",
    re.I,
)
_DISCOVERY_PATTERN = re.compile(
    r"\b(?:recommend|suggest|find|search|show|have|available|menu|catalog|coffee|tea|drink|"
    r"beverage|snack|gift|beans|budget|under|floral|refreshing)\b",
    re.I,
)
_MEMORY_PATTERN = re.compile(r"\b(?:remember|forget|memory|preference)\b", re.I)
_PICKUP_PATTERN = re.compile(r"\b(?:pickup|pick(?:\s+it)?\s+up)\b", re.I)
_NEGATED_PICKUP_PATTERN = re.compile(
    r"\b(?:no|not|never|don(?:'|’)t|do\s+not)\b.{0,30}"
    r"\b(?:pickup|pick(?:\s+it)?\s+up)\b",
    re.I,
)
_AFFIRMATIVE_ONLY_PATTERN = re.compile(
    r"^(?:yes|yeah|yep|okay|ok|sure|alright|go\s+ahead)(?:\s+please)?[,.!]?$",
    re.I,
)
_LOCAL_DELIVERY_PATTERN = re.compile(r"\b(?:local\s+delivery|deliver(?:y|ed)?)\b", re.I)
_NEGATED_LOCAL_DELIVERY_PATTERN = re.compile(
    r"\b(?:no|not|never|don(?:'|’)t|do\s+not)\b.{0,30}"
    r"\b(?:local\s+delivery|deliver(?:y|ed)?)\b",
    re.I,
)
_CHECKOUT_CONTINUATION_PATTERN = re.compile(
    r"^(?:(?:yes|yeah|yep|okay|ok|sure|alright)\s*[,!.]?\s*)?"
    r"(?:(?:i(?:'|’)m|i\s+am)\s+)?"
    r"(?:ready|proceed|confirm|go\s+ahead)"
    r"(?:\s+(?:please|with\s+(?:it|checkout)))?[,.!]?$",
    re.I,
)
_SCHEDULE_CANCEL_PATTERN = re.compile(
    r"\b(?:cancel|stop|abandon|never\s+mind|forget\s+it)\b.{0,40}\b"
    r"(?:schedule|scheduled|recurring|reorder|purchase)?\b",
    re.I,
)
_SCHEDULE_CONTINUATION_PATTERN = re.compile(
    r"(?:"
    r"^\s*(?:yes|yeah|yep|okay|ok|sure|alright)(?:\s+please)?[,.!]?\s*$|"
    r"\d|₹|"
    r"\b(?:once|daily|weekly|monthly|occurrences?|runs?|budget|caps?|rupees?|inr|"
    r"start|first\s+run|expires?|end\s+date|small|regular|medium|large|dairy|oat|"
    r"sweet(?:ness)?|whole\s+bean|ground)\b"
    r")",
    re.I,
)


@dataclass(frozen=True)
class FulfillmentOption:
    destination_id: str
    label: str
    fulfillment_type: str

    def as_dict(self) -> dict[str, str]:
        return {
            "destination_id": self.destination_id,
            "label": self.label,
            "fulfillment_type": self.fulfillment_type,
        }


@dataclass(frozen=True)
class FulfillmentClarification:
    options: tuple[FulfillmentOption, ...]
    default_destination_id: str | None
    version: str = CLARIFICATION_VERSION
    kind: str = "pickup_location"
    purpose: str = "checkout"

    @classmethod
    def from_structured_content(
        cls, structured_content: Mapping[str, Any] | None
    ) -> FulfillmentClarification | None:
        if not structured_content:
            return None
        value = structured_content.get("clarification")
        if not isinstance(value, Mapping):
            return None
        kind = str(value.get("kind", ""))
        if value.get("version") != CLARIFICATION_VERSION or kind not in _CLARIFICATION_KINDS:
            return None
        purpose = str(value.get("purpose", "checkout"))
        if purpose not in _CLARIFICATION_PURPOSES:
            return None
        raw_options = value.get("options")
        if not isinstance(raw_options, list) or not 1 <= len(raw_options) <= 10:
            return None
        options: list[FulfillmentOption] = []
        seen_ids: set[str] = set()
        for raw_option in raw_options:
            if not isinstance(raw_option, Mapping):
                return None
            destination_id = str(raw_option.get("destination_id", ""))
            label = str(raw_option.get("label", "")).strip()
            fulfillment_type = str(raw_option.get("fulfillment_type", ""))
            try:
                uuid.UUID(destination_id)
            except ValueError:
                return None
            allowed_types = (
                {"pickup"}
                if kind == "pickup_location"
                else {"local_delivery", "shipping"}
                if kind == "delivery_address"
                else {"local_delivery", "pickup"}
            )
            if fulfillment_type not in allowed_types or not label or len(label) > 160:
                return None
            if destination_id in seen_ids:
                return None
            seen_ids.add(destination_id)
            options.append(FulfillmentOption(destination_id, label, fulfillment_type))
        raw_default = value.get("default_destination_id")
        default_destination_id = str(raw_default) if raw_default else None
        if kind == "fulfillment_method" and default_destination_id is not None:
            return None
        if kind != "fulfillment_method" and default_destination_id not in seen_ids:
            return None
        return cls(
            options=tuple(options),
            default_destination_id=default_destination_id,
            kind=kind,
            purpose=purpose,
        )


@dataclass(frozen=True)
class FulfillmentSelection:
    destination_id: str
    label: str
    fulfillment_type: str
    resolution: str
    purpose: str = "checkout"

    def as_dict(self) -> dict[str, str]:
        return {
            "destination_id": self.destination_id,
            "label": self.label,
            "fulfillment_type": self.fulfillment_type,
            "resolution": self.resolution,
            "purpose": self.purpose,
        }


@dataclass(frozen=True)
class AgentPolicyDecision:
    intent: AgentIntent
    risk_level: AgentRiskLevel
    allowed_tools: frozenset[str]
    reason_codes: tuple[str, ...]
    policy_version: str = POLICY_VERSION
    max_tool_calls: int = 8
    max_mutations: int = 1
    financial_action_requested: bool = False
    fulfillment_selection: FulfillmentSelection | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "intent": self.intent.value,
            "risk_level": self.risk_level.value,
            "allowed_tools": sorted(self.allowed_tools),
            "reason_codes": list(self.reason_codes),
            "policy_version": self.policy_version,
            "max_tool_calls": self.max_tool_calls,
            "max_mutations": self.max_mutations,
            "financial_action_requested": self.financial_action_requested,
            "fulfillment_selection": (
                self.fulfillment_selection.as_dict() if self.fulfillment_selection else None
            ),
        }


@dataclass(frozen=True)
class ToolAuthorization:
    allowed: bool
    reason_code: str


class AgentActionPolicy:
    """Deterministic capability policy for one authenticated agent turn.

    The current message is authoritative for consent. The only accepted prior
    state is a server-generated, immediately preceding clarification whose exact
    destination is carried into object-level authorization.
    """

    _intent_tools = MappingProxyType(
        {
            AgentIntent.DISCOVER: READ_TOOLS,
            AgentIntent.CART_READ: READ_TOOLS,
            AgentIntent.FULFILLMENT_READ: READ_TOOLS,
            AgentIntent.FULFILLMENT_SELECT: READ_TOOLS | CHECKOUT_TOOLS,
            AgentIntent.CART_ADD: READ_TOOLS | {"add_to_cart"},
            AgentIntent.CART_UPDATE: READ_TOOLS | {"update_cart_item"},
            AgentIntent.CART_REMOVE: READ_TOOLS | {"remove_cart_item"},
            AgentIntent.CHECKOUT_PREPARE: READ_TOOLS | CHECKOUT_TOOLS | {"add_to_cart"},
            AgentIntent.SCHEDULE_DRAFT: READ_TOOLS | SCHEDULE_TOOLS,
            AgentIntent.MEMORY_MANAGE: READ_TOOLS,
            AgentIntent.ANSWER: READ_TOOLS,
        }
    )

    @classmethod
    def classify(
        cls,
        current_message: str,
        *,
        max_tool_calls: int = 8,
        max_mutations: int = 1,
        pending_fulfillment: FulfillmentClarification | None = None,
        pending_schedule_draft: bool = False,
    ) -> AgentPolicyDecision:
        message = cls._normalize(current_message)
        financial_requested = any(pattern.search(message) for pattern in _DENIED_FINANCIAL_PATTERNS)
        fulfillment_selection = (
            cls._resolve_fulfillment_reply(message, pending_fulfillment)
            if not financial_requested
            else None
        )

        if _SCHEDULE_PATTERN.search(message) and not _SCHEDULE_CANCEL_PATTERN.search(message):
            intent = AgentIntent.SCHEDULE_DRAFT
            risk = AgentRiskLevel.HIGH
            reason = "explicit_schedule_request"
        elif fulfillment_selection is not None:
            intent = (
                AgentIntent.SCHEDULE_DRAFT
                if fulfillment_selection.purpose == "schedule_draft"
                else AgentIntent.FULFILLMENT_SELECT
            )
            risk = AgentRiskLevel.HIGH
            reason = (
                "contextual_schedule_fulfillment"
                if fulfillment_selection.purpose == "schedule_draft"
                else "contextual_fulfillment_default"
                if fulfillment_selection.resolution.startswith("defaulted_")
                else "contextual_fulfillment_option"
            )
        elif _CHECKOUT_PATTERN.search(message) and not financial_requested:
            intent = AgentIntent.CHECKOUT_PREPARE
            risk = AgentRiskLevel.HIGH
            reason = "explicit_checkout_request"
        elif _FULFILLMENT_SELECT_PATTERN.search(message):
            intent = AgentIntent.FULFILLMENT_SELECT
            risk = AgentRiskLevel.HIGH
            reason = "explicit_fulfillment_selection"
        elif _REMOVE_PATTERN.search(message):
            intent = AgentIntent.CART_REMOVE
            risk = AgentRiskLevel.MEDIUM
            reason = "explicit_cart_remove"
        elif _UPDATE_PATTERN.search(message):
            intent = AgentIntent.CART_UPDATE
            risk = AgentRiskLevel.MEDIUM
            reason = "explicit_cart_update"
        elif _ADD_PATTERN.search(message):
            intent = AgentIntent.CART_ADD
            risk = AgentRiskLevel.MEDIUM
            reason = "explicit_cart_add"
        elif _CART_READ_PATTERN.search(message):
            intent = AgentIntent.CART_READ
            risk = AgentRiskLevel.LOW
            reason = "cart_read_request"
        elif _FULFILLMENT_READ_PATTERN.search(message):
            intent = AgentIntent.FULFILLMENT_READ
            risk = AgentRiskLevel.LOW
            reason = "fulfillment_read_request"
        elif _MEMORY_PATTERN.search(message):
            intent = AgentIntent.MEMORY_MANAGE
            risk = AgentRiskLevel.LOW
            reason = "memory_management_request"
        elif (
            pending_schedule_draft
            and _SCHEDULE_CONTINUATION_PATTERN.search(message)
            and not _SCHEDULE_CANCEL_PATTERN.search(message)
        ):
            intent = AgentIntent.SCHEDULE_DRAFT
            risk = AgentRiskLevel.HIGH
            reason = "contextual_schedule_continuation"
        elif _DISCOVERY_PATTERN.search(message):
            intent = AgentIntent.DISCOVER
            risk = AgentRiskLevel.LOW
            reason = "catalog_discovery_request"
        else:
            intent = AgentIntent.ANSWER
            risk = AgentRiskLevel.LOW
            reason = "no_mutation_intent"

        allowed = cls._intent_tools[intent]
        reasons = [reason]
        if financial_requested:
            allowed = READ_TOOLS
            risk = AgentRiskLevel.CRITICAL
            reasons.extend(("financial_action_not_agent_capability", "trusted_surface_required"))

        return AgentPolicyDecision(
            intent=intent,
            risk_level=risk,
            allowed_tools=frozenset(allowed),
            reason_codes=tuple(reasons),
            max_tool_calls=max_tool_calls,
            max_mutations=max_mutations,
            financial_action_requested=financial_requested,
            fulfillment_selection=fulfillment_selection,
        )

    @classmethod
    def _resolve_fulfillment_reply(
        cls,
        current_message: str,
        clarification: FulfillmentClarification | None,
    ) -> FulfillmentSelection | None:
        if clarification is None:
            return None
        message = cls._normalize_for_match(current_message)
        matched = [
            option
            for option in clarification.options
            if cls._message_selects_option(message, option.label)
        ]
        if len(matched) == 1:
            option = matched[0]
            return FulfillmentSelection(
                option.destination_id,
                option.label,
                option.fulfillment_type,
                "matched_option",
                clarification.purpose,
            )

        ordinal_match = re.fullmatch(
            r"(?:the\s+)?(?:(first|second|third)|option\s+([123]))(?:\s+one)?[,.!]?",
            message,
        )
        if ordinal_match:
            value = ordinal_match.group(1) or ordinal_match.group(2)
            index = {"first": 0, "second": 1, "third": 2, "1": 0, "2": 1, "3": 2}[value]
            if index < len(clarification.options):
                option = clarification.options[index]
                return FulfillmentSelection(
                    option.destination_id,
                    option.label,
                    option.fulfillment_type,
                    "matched_option",
                    clarification.purpose,
                )

        if clarification.kind == "fulfillment_method":
            method_selection = cls._resolve_fulfillment_method(current_message, clarification)
            if method_selection is not None:
                return method_selection

        should_default = (
            bool(_AFFIRMATIVE_ONLY_PATTERN.fullmatch(current_message.strip()))
            or bool(_CHECKOUT_CONTINUATION_PATTERN.fullmatch(current_message.strip()))
            or (
                clarification.kind == "pickup_location"
                and bool(_PICKUP_PATTERN.search(current_message))
                and not _NEGATED_PICKUP_PATTERN.search(current_message)
            )
            or (
                clarification.kind == "delivery_address"
                and bool(_LOCAL_DELIVERY_PATTERN.search(current_message))
                and not _NEGATED_LOCAL_DELIVERY_PATTERN.search(current_message)
            )
        )
        if not should_default:
            return None
        option = next(
            (
                candidate
                for candidate in clarification.options
                if candidate.destination_id == clarification.default_destination_id
            ),
            None,
        )
        if option is None:
            return None
        return FulfillmentSelection(
            option.destination_id,
            option.label,
            option.fulfillment_type,
            ("defaulted_pickup" if option.fulfillment_type == "pickup" else "defaulted_delivery"),
            clarification.purpose,
        )

    @staticmethod
    def _resolve_fulfillment_method(
        current_message: str,
        clarification: FulfillmentClarification,
    ) -> FulfillmentSelection | None:
        requested_type: str | None = None
        if _PICKUP_PATTERN.search(current_message) and not _NEGATED_PICKUP_PATTERN.search(
            current_message
        ):
            requested_type = "pickup"
        elif _LOCAL_DELIVERY_PATTERN.search(
            current_message
        ) and not _NEGATED_LOCAL_DELIVERY_PATTERN.search(current_message):
            requested_type = "local_delivery"
        if requested_type is None:
            return None
        matching = [
            option for option in clarification.options if option.fulfillment_type == requested_type
        ]
        if len(matching) != 1:
            return None
        option = matching[0]
        return FulfillmentSelection(
            option.destination_id,
            option.label,
            option.fulfillment_type,
            "defaulted_pickup" if requested_type == "pickup" else "defaulted_delivery",
            clarification.purpose,
        )

    @classmethod
    def _message_selects_option(cls, normalized_message: str, label: str) -> bool:
        normalized_label = re.escape(cls._normalize_for_match(label))
        patterns = (
            rf"(?:the\s+)?{normalized_label}(?:\s+please)?",
            rf"(?:i\s+)?(?:will\s+)?(?:use|choose|select|prefer|want|go\s+with)\s+"
            rf"(?:the\s+)?{normalized_label}(?:\s+please)?",
            rf"(?:pickup|pick(?:\s+it)?\s+up)\s+(?:at|from)\s+"
            rf"(?:the\s+)?{normalized_label}(?:\s+please)?",
        )
        return any(re.fullmatch(pattern, normalized_message) for pattern in patterns)

    @staticmethod
    def authorize_tool(
        decision: AgentPolicyDecision,
        tool_name: str,
        *,
        tool_call_count: int,
        mutation_count: int,
    ) -> ToolAuthorization:
        if tool_name not in ALL_AGENT_TOOLS:
            return ToolAuthorization(False, "unknown_tool")
        if tool_name not in decision.allowed_tools:
            return ToolAuthorization(False, "tool_not_allowed_for_intent")
        if tool_call_count >= decision.max_tool_calls:
            return ToolAuthorization(False, "tool_call_budget_exceeded")
        if tool_name in MUTATING_TOOLS and mutation_count >= decision.max_mutations:
            return ToolAuthorization(False, "mutation_budget_exceeded")
        return ToolAuthorization(True, "policy_allowed")

    @staticmethod
    def _normalize(message: str) -> str:
        return " ".join(message.strip().split())[:2000]

    @staticmethod
    def _normalize_for_match(message: str) -> str:
        decomposed = unicodedata.normalize("NFKD", message.casefold())
        ascii_message = "".join(
            character for character in decomposed if not unicodedata.combining(character)
        )
        return " ".join(re.findall(r"[a-z0-9]+", ascii_message))[:2000]
