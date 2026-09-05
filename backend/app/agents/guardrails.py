from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

_RESTRICTED_OUTPUT_PATTERNS = (
    re.compile(r"(?i)\bbearer\s+[a-z0-9._~+/-]{12,}"),
    re.compile(r"(?i)\b(?:api[_ -]?key|password|secret)\s*[:=]\s*\S+"),
    re.compile(r"\beyJ[a-zA-Z0-9_-]{8,}\.[a-zA-Z0-9_-]{8,}\.[a-zA-Z0-9_-]{8,}\b"),
    re.compile(r"\b(?:\d[ -]*?){13,19}\b"),
)
_BELOW_CONTROL_CLAIM = re.compile(
    r"\b(?:controls?|buttons?|panel)\s+(?:shown\s+)?below\b",
    re.I,
)
_TRUSTED_SCHEDULE_UI_CLAIM = re.compile(
    r"\btrusted\s+(?:account\s+)?(?:ui|surface)\b.{0,120}"
    r"\b(?:schedule|scheduled|bounds?|authoriz(?:e|ation))\b",
    re.I,
)
_SCHEDULE_UNAVAILABLE_CLAIM = re.compile(
    r"\b(?:automated\s+)?schedul(?:e|ing)\s+(?:tools?|features?|controls?)\b"
    r".{0,40}\b(?:unavailable|not\s+available|disabled)\b",
    re.I,
)


class AgentOutputGuard:
    @classmethod
    def sanitize(cls, text: str, max_characters: int) -> str:
        protected = text
        for pattern in _RESTRICTED_OUTPUT_PATTERNS:
            protected = pattern.sub("[REDACTED]", protected)
        return protected.strip()[:max_characters]

    @classmethod
    def reconcile_ui_claims(
        cls,
        text: str,
        structured_content: Mapping[str, Any],
    ) -> tuple[str, bool]:
        if _SCHEDULE_UNAVAILABLE_CLAIM.search(text):
            activity = structured_content.get("activity")
            schedule_tool_failed = isinstance(activity, list) and any(
                isinstance(item, Mapping)
                and item.get("tool") in {"list_scheduling_options", "draft_scheduled_purchase"}
                and item.get("status") == "error"
                for item in activity
            )
            if not schedule_tool_failed:
                schedule_configuration = structured_content.get("schedule_configuration")
                if isinstance(schedule_configuration, Mapping) and not schedule_configuration.get(
                    "draft_available", True
                ):
                    availability_message = schedule_configuration.get("availability_message")
                    if isinstance(availability_message, str) and availability_message.strip():
                        return availability_message.strip(), True
                if schedule_configuration:
                    return (
                        "Complete the schedule controls below, then choose any remaining "
                        "product or fulfillment option. Nothing has been authorized.",
                        True,
                    )
                return (
                    "The schedule draft is still incomplete. Please provide the missing "
                    "timing, occurrence, and spending bounds. Nothing has been authorized.",
                    True,
                )
        if _TRUSTED_SCHEDULE_UI_CLAIM.search(text) and not structured_content.get(
            "scheduled_purchase"
        ):
            return (
                "I have not created a schedule draft yet. Please provide the missing "
                "schedule details: first run date and time, end date or maximum runs, "
                "per-order cap, and total cap. Nothing has been authorized.",
                True,
            )
        if not _BELOW_CONTROL_CLAIM.search(text):
            return text, False
        lowered = text.casefold()
        claims_configuration = "configuration" in lowered or "customization" in lowered
        claims_checkout = "checkout" in lowered or "payment" in lowered
        if claims_configuration and not structured_content.get("product_configuration"):
            return (
                "I do not have verified product-configuration controls to show yet. "
                "Please choose a product so I can load its current options.",
                True,
            )
        if claims_checkout and not structured_content.get("checkout"):
            if structured_content.get("suggestions"):
                return (
                    "Choose one of the fulfillment options below. I will display checkout "
                    "controls only after the exact checkout quote is prepared successfully.",
                    True,
                )
            return (
                "I have not prepared a checkout yet, so there are no checkout controls to "
                "show. No approval or payment has been made.",
                True,
            )
        if not structured_content.get("checkout") and not structured_content.get(
            "product_configuration"
        ):
            return (
                "There are no verified interactive controls to show for this response.",
                True,
            )
        return text, False
