from __future__ import annotations

import base64
import calendar
import hashlib
import hmac
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import jwt
from ap2.sdk.checkout_mandate_chain import CheckoutMandateChain
from ap2.sdk.constraints import MandateContext
from ap2.sdk.generated.checkout_mandate import CheckoutMandate
from ap2.sdk.generated.open_checkout_mandate import (
    AllowedMerchants,
    LineItemRequirements,
    LineItems,
    OpenCheckoutMandate,
)
from ap2.sdk.generated.open_checkout_mandate import Item as MandateItem
from ap2.sdk.generated.open_payment_mandate import (
    AgentRecurrence,
    AllowedPayees,
    AllowedPaymentInstruments,
    AllowedPisps,
    AmountRange,
    Budget,
    ExecutionDate,
    Frequency,
    OpenPaymentMandate,
    PaymentReference,
)
from ap2.sdk.generated.payment_mandate import PaymentMandate
from ap2.sdk.generated.types.amount import Amount
from ap2.sdk.generated.types.merchant import Merchant
from ap2.sdk.generated.types.payment_instrument import PaymentInstrument
from ap2.sdk.generated.types.pisp import PISP
from ap2.sdk.mandate import MandateClient
from ap2.sdk.payment_mandate_chain import PaymentMandateChain
from ap2.sdk.sdjwt.common import compute_sd_hash, parse_token
from cryptography.hazmat.primitives.asymmetric import ec
from jwcrypto.jwk import JWK

from app.protocols.ap2.crypto import AP2KeySet, digest_b64url

_P256_ORDER = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
_FREQUENCIES = {
    "once": Frequency.ON_DEMAND,
    "daily": Frequency.DAILY,
    "weekly": Frequency.WEEKLY,
    "monthly": Frequency.MONTHLY,
}
_SCHEDULED_POLICY_DOMAIN = "agentbasket.ap2.scheduled-purchase-policy.v1"
_SCHEDULED_POLICY_RISK_KEY = "agentbasket_scheduled_policy"


class _JsonSerializableOpenPaymentMandate(OpenPaymentMandate):
    """Adapter for AP2 v0.2's Python-mode enum serialization bug.

    The pinned SDK calls ``model_dump()`` without JSON mode before signing.
    Nested ``Frequency`` values would otherwise reach the JSON encoder as Enum
    objects. The signed claims remain the ordinary OpenPaymentMandate schema and
    are verified back into the upstream model below.
    """

    def model_dump(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        kwargs["mode"] = "json"
        return super().model_dump(*args, **kwargs)


@dataclass(frozen=True)
class OpenMandateBundle:
    checkout_token: str
    payment_token: str
    checkout_hash: str
    agent_key_id: str
    agent_public_jwk: dict[str, Any]
    policy_sha256: str


@dataclass(frozen=True)
class ClosedMandateBundle:
    checkout_token: str
    payment_token: str
    checkout_hash: str


def derive_agent_key(master_key_b64url: str, intent_id: uuid.UUID) -> JWK:
    """Derive one stable P-256 key without persisting private key material."""
    try:
        encoded = master_key_b64url.strip()
        raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
    except (ValueError, TypeError) as error:
        raise ValueError("AP2 autonomous master key is not valid base64url") from error
    if len(raw) != 32:
        raise ValueError("AP2 autonomous master key must decode to exactly 32 bytes")
    digest = hmac.new(
        raw,
        b"agentbasket:ap2:autonomous-agent:v1:" + intent_id.bytes,
        hashlib.sha256,
    ).digest()
    scalar = int.from_bytes(digest, "big") % (_P256_ORDER - 1) + 1
    private_key = ec.derive_private_key(scalar, ec.SECP256R1())
    key = JWK.from_pyca(private_key)
    key.update(kid=f"agentbasket-schedule-{intent_id.hex}")
    return key


def public_jwk(key: JWK) -> dict[str, Any]:
    value = key.export_public(as_dict=True)
    return {name: value[name] for name in ("kty", "crv", "x", "y", "kid") if name in value}


def create_open_mandates(
    *,
    keys: AP2KeySet,
    agent_key: JWK,
    merchant_id: str,
    merchant_name: str,
    item_requirements: list[dict[str, Any]],
    payment_instrument_id: str,
    payment_instrument_type: str,
    currency: str,
    max_amount_minor: int,
    max_total_minor: int,
    frequency: str,
    max_occurrences: int,
    first_run_at: datetime,
    expires_at: datetime,
    strict_constraints: dict[str, Any],
) -> OpenMandateBundle:
    if frequency not in _FREQUENCIES:
        raise ValueError("Unsupported AP2 recurrence frequency")
    if max_amount_minor <= 0 or max_total_minor < max_amount_minor:
        raise ValueError("Invalid AP2 payment bounds")
    _validate_open_policy(
        constraints=strict_constraints,
        merchant_id=merchant_id,
        merchant_name=merchant_name,
        item_requirements=item_requirements,
        currency=currency,
        max_amount_minor=max_amount_minor,
        max_total_minor=max_total_minor,
        frequency=frequency,
        max_occurrences=max_occurrences,
        first_run_at=first_run_at,
        expires_at=expires_at,
    )
    policy_sha256 = scheduled_policy_sha256(strict_constraints)
    cnf = {"jwk": public_jwk(agent_key)}
    merchant = Merchant(id=merchant_id, name=merchant_name)
    open_checkout = OpenCheckoutMandate(
        constraints=[
            AllowedMerchants(allowed=[merchant]),
            LineItems(
                items=[
                    LineItemRequirements(
                        id=str(requirement["id"]),
                        acceptable_items=[
                            MandateItem(id=str(item["id"]), title=str(item["title"]))
                            for item in requirement["acceptable_items"]
                        ],
                        quantity=int(requirement["quantity"]),
                    )
                    for requirement in item_requirements
                ]
            ),
        ],
        cnf=cnf,
        iat=int(datetime.now(first_run_at.tzinfo).timestamp()),
        exp=int(expires_at.timestamp()),
    )
    client = MandateClient()
    checkout_token = client.create(
        payloads=[open_checkout], issuer_key=keys.trusted_surface.private_jwk
    )
    client.verify(
        token=checkout_token,
        key_or_provider=keys.trusted_surface.public_jwk,
        payload_type=OpenCheckoutMandate,
    )
    checkout_hash = compute_sd_hash(parse_token(checkout_token))
    instrument = PaymentInstrument(
        id=payment_instrument_id,
        type=payment_instrument_type,
    )
    pisp = PISP(
        legal_name="Razorpay Software Private Limited",
        brand_name="Razorpay",
        domain_name="razorpay.com",
    )
    open_payment = _JsonSerializableOpenPaymentMandate(
        constraints=[
            AgentRecurrence(
                frequency=_FREQUENCIES[frequency],
                max_occurrences=max_occurrences,
            ),
            AllowedPayees(allowed=[merchant]),
            AllowedPaymentInstruments(allowed=[instrument]),
            AllowedPisps(allowed=[pisp]),
            AmountRange(currency=currency, min=100, max=max_amount_minor),
            # AP2 v0.2 Budget.max is in major units. AgentBasket repeats this
            # check using integer minor units before every execution.
            Budget(max=max_total_minor / 100, currency=currency),
            ExecutionDate(
                not_before=first_run_at.isoformat(),
                not_after=expires_at.isoformat(),
            ),
            PaymentReference(conditional_transaction_id=checkout_hash),
        ],
        cnf=cnf,
        risk_data={
            "channel": "merchant_in_app",
            "human_present": False,
            _SCHEDULED_POLICY_RISK_KEY: {
                "domain": _SCHEDULED_POLICY_DOMAIN,
                "sha256": policy_sha256,
            },
        },
        iat=int(datetime.now(first_run_at.tzinfo).timestamp()),
        exp=int(expires_at.timestamp()),
    )
    payment_token = client.create(
        payloads=[open_payment], issuer_key=keys.trusted_surface.private_jwk
    )
    client.verify(
        token=payment_token,
        key_or_provider=keys.trusted_surface.public_jwk,
        payload_type=OpenPaymentMandate,
    )
    return OpenMandateBundle(
        checkout_token=checkout_token,
        payment_token=payment_token,
        checkout_hash=checkout_hash,
        agent_key_id=str(agent_key.get("kid")),
        agent_public_jwk=public_jwk(agent_key),
        policy_sha256=policy_sha256,
    )


def close_and_verify_mandates(
    *,
    keys: AP2KeySet,
    agent_key: JWK,
    open_checkout_token: str,
    open_payment_token: str,
    open_checkout_hash: str,
    merchant_checkout_jwt: str,
    amount_minor: int,
    currency: str,
    merchant_id: str,
    merchant_name: str,
    payment_instrument_id: str,
    payment_instrument_type: str,
    audience: str,
    nonce: str,
    successful_occurrences: int,
    spent_minor: int,
    last_used_at: datetime | None,
    execution_at: datetime,
    strict_constraints: dict[str, Any],
) -> ClosedMandateBundle:
    policy_sha256 = scheduled_policy_sha256(strict_constraints)
    checkout_hash = digest_b64url(merchant_checkout_jwt)
    now = int(datetime.now().timestamp())
    checkout_payload = CheckoutMandate(
        checkout_jwt=merchant_checkout_jwt,
        checkout_hash=checkout_hash,
        iat=now,
    )
    pisp = PISP(
        legal_name="Razorpay Software Private Limited",
        brand_name="Razorpay",
        domain_name="razorpay.com",
    )
    payment_payload = PaymentMandate(
        transaction_id=checkout_hash,
        payee=Merchant(id=merchant_id, name=merchant_name),
        pisp=pisp,
        payment_amount=Amount(amount=amount_minor, currency=currency),
        payment_instrument=PaymentInstrument(
            id=payment_instrument_id,
            type=payment_instrument_type,
        ),
        execution_date=execution_at.isoformat(),
        risk_data={
            "channel": "merchant_in_app",
            "human_present": False,
            _SCHEDULED_POLICY_RISK_KEY: {
                "domain": _SCHEDULED_POLICY_DOMAIN,
                "sha256": policy_sha256,
            },
        },
        iat=now,
    )
    client = MandateClient()
    checkout_token = client.present(
        holder_key=agent_key,
        mandate_token=open_checkout_token,
        payloads=[checkout_payload],
        aud=audience,
        nonce=nonce,
    )
    payment_token = client.present(
        holder_key=agent_key,
        mandate_token=open_payment_token,
        payloads=[payment_payload],
        aud=audience,
        nonce=nonce,
    )

    def root_key_provider(_token: Any) -> JWK:
        return keys.trusted_surface.public_jwk

    checkout_payloads = client.verify(
        token=checkout_token,
        key_or_provider=root_key_provider,
        expected_aud=audience,
        expected_nonce=nonce,
    )
    payment_payloads = client.verify(
        token=payment_token,
        key_or_provider=root_key_provider,
        expected_aud=audience,
        expected_nonce=nonce,
    )
    if not isinstance(checkout_payloads, list) or not isinstance(payment_payloads, list):
        raise ValueError("AP2 closed mandate chain verification returned an invalid result")
    checkout_chain = CheckoutMandateChain.parse(checkout_payloads)
    payment_chain = PaymentMandateChain.parse(payment_payloads)
    violations = checkout_chain.verify(
        expected_checkout_hash=checkout_hash,
        checkout_jwt=merchant_checkout_jwt,
    )
    violations.extend(
        payment_chain.verify(
            expected_transaction_id=checkout_hash,
            expected_open_checkout_hash=open_checkout_hash,
            mandate_context=MandateContext(
                total_uses=successful_occurrences,
                total_amount=spent_minor,
                last_used_date=last_used_at.timestamp() if last_used_at else None,
            ),
        )
    )
    violations.extend(
        strict_violations(
            merchant_checkout_jwt=merchant_checkout_jwt,
            checkout_hash=checkout_hash,
            payment=payment_payload,
            merchant_id=merchant_id,
            payment_instrument_id=payment_instrument_id,
            successful_occurrences=successful_occurrences,
            spent_minor=spent_minor,
            constraints=strict_constraints,
        )
    )
    violations.extend(
        _scheduled_policy_digest_violations(
            open_payment=payment_chain.open_mandate,
            closed_payment=payment_chain.closed_mandate,
            constraints=strict_constraints,
        )
    )
    if violations:
        raise ValueError("; ".join(violations))
    # The SDK validates constraints but not the merchant Checkout JWT signature.
    keys.merchant.verify(merchant_checkout_jwt, keys.audience)
    return ClosedMandateBundle(
        checkout_token=checkout_token,
        payment_token=payment_token,
        checkout_hash=checkout_hash,
    )


def strict_violations(
    *,
    merchant_checkout_jwt: str,
    checkout_hash: str,
    payment: PaymentMandate,
    merchant_id: str,
    payment_instrument_id: str,
    successful_occurrences: int,
    spent_minor: int,
    constraints: dict[str, Any],
) -> list[str]:
    """Close SDK gaps with exact, integer-only AgentBasket policy checks."""
    violations: list[str] = []
    claims = jwt.decode(merchant_checkout_jwt, options={"verify_signature": False})
    if claims.get("merchant", {}).get("id") != merchant_id:
        violations.append("Strict merchant constraint failed")
    lines = claims.get("line_items")
    if not isinstance(lines, list) or len(lines) != len(constraints.get("items", [])):
        violations.append("Strict line-item count constraint failed")
        lines = []
    remaining = list(lines)
    for requirement in constraints.get("items", []):
        acceptable = {str(value) for value in requirement["acceptable_variant_ids"]}
        quantity = int(requirement["quantity"])
        matching = [
            line
            for line in remaining
            if isinstance(line, dict) and str((line.get("item") or {}).get("id")) in acceptable
        ]
        if len(matching) != 1 or int(matching[0].get("quantity", -1)) != quantity:
            violations.append(f"Strict line-item requirement {requirement['id']} failed")
            continue
        expected_modifiers = sorted(
            str(value) for value in requirement.get("modifier_option_ids", [])
        )
        actual_modifiers = sorted(
            str(value)
            for value in (matching[0].get("agentbasket") or {}).get("modifier_option_ids", [])
        )
        if actual_modifiers != expected_modifiers:
            violations.append(f"Strict line-item modifiers {requirement['id']} failed")
        remaining.remove(matching[0])
    if remaining:
        violations.append("Strict checkout contains unapproved line items")

    total_minor = next(
        (
            int(total["amount"])
            for total in claims.get("totals", [])
            if isinstance(total, dict) and total.get("type") == "total"
        ),
        -1,
    )
    max_amount = int(constraints["max_amount_minor"])
    max_total = int(constraints["max_total_minor"])
    max_occurrences = int(constraints["max_occurrences"])
    if total_minor < 0 or total_minor > max_amount:
        violations.append("Strict per-order amount constraint failed")
    if spent_minor + max(total_minor, 0) > max_total:
        violations.append("Strict total budget constraint failed")
    if successful_occurrences >= max_occurrences:
        violations.append("Strict occurrence constraint failed")
    if payment.payment_amount.amount != total_minor:
        violations.append("Strict payment-to-checkout amount binding failed")
    if payment.payment_amount.currency.upper() != str(claims.get("currency", "")).upper():
        violations.append("Strict currency binding failed")
    if payment.payment_amount.currency.upper() != str(constraints.get("currency", "")).upper():
        violations.append("Strict authorized currency constraint failed")
    if payment.payment_instrument.id != payment_instrument_id:
        violations.append("Strict payment instrument constraint failed")
    if payment.transaction_id != checkout_hash:
        violations.append("Strict transaction binding failed")

    agentbasket = claims.get("agentbasket")
    if not isinstance(agentbasket, dict):
        violations.append("Strict merchant checkout metadata missing")
        agentbasket = {}
    if agentbasket.get("fulfillment_type") != constraints.get("fulfillment_type"):
        violations.append("Strict fulfillment constraint failed")
    if str(agentbasket.get("location_id") or "") != str(constraints.get("location_id") or ""):
        violations.append("Strict location constraint failed")
    if agentbasket.get("address_sha256") != constraints.get("address_sha256"):
        violations.append("Strict delivery address constraint failed")
    try:
        execution_at = _parse_datetime(payment.execution_date)
        signed_execution_at = _parse_datetime(agentbasket.get("scheduled_for"))
        if execution_at != signed_execution_at:
            violations.append("Strict payment execution-time binding failed")
        if not _is_authorized_execution_at(constraints, execution_at):
            violations.append("Strict recurrence cadence constraint failed")
    except (KeyError, TypeError, ValueError, ZoneInfoNotFoundError):
        violations.append("Strict recurrence cadence constraint failed")
    return violations


def _parse_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        raise TypeError("timestamp is missing")
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include timezone")
    return parsed.astimezone(UTC)


def _is_authorized_execution_at(constraints: dict[str, Any], execution_at: datetime) -> bool:
    """Check recurrence-grid membership independently from successful debits.

    Failed provider attempts still advance the schedule, so the number of
    successful uses cannot identify the next authorized calendar slot.
    """
    first = _parse_datetime(constraints["first_run_at"])
    frequency = str(constraints["frequency"])
    interval = int(constraints["interval_count"])
    if interval <= 0:
        raise ValueError("invalid recurrence state")
    if frequency == "once":
        return execution_at == first
    zone = ZoneInfo(str(constraints["timezone"]))
    first_local = first.astimezone(zone)
    execution_local = execution_at.astimezone(zone)
    if execution_at < first:
        return False
    if frequency == "daily":
        elapsed_days = (execution_local.date() - first_local.date()).days
        if elapsed_days % interval:
            return False
        candidate = first_local + timedelta(days=elapsed_days)
        return candidate.astimezone(UTC) == execution_at
    if frequency == "weekly":
        period_days = interval * 7
        elapsed_days = (execution_local.date() - first_local.date()).days
        if elapsed_days % period_days:
            return False
        candidate = first_local + timedelta(days=elapsed_days)
        return candidate.astimezone(UTC) == execution_at
    if frequency != "monthly":
        raise ValueError("unsupported recurrence frequency")
    elapsed_months = (
        (execution_local.year - first_local.year) * 12 + execution_local.month - first_local.month
    )
    if elapsed_months < 0 or elapsed_months % interval:
        return False
    month_index = first_local.year * 12 + first_local.month - 1 + elapsed_months
    year, month_zero = divmod(month_index, 12)
    month = month_zero + 1
    day = min(first_local.day, calendar.monthrange(year, month)[1])
    candidate = first_local.replace(year=year, month=month, day=day)
    return candidate.astimezone(UTC) == execution_at


def verify_existing_closed_mandates(
    *,
    keys: AP2KeySet,
    checkout_token: str,
    payment_token: str,
    open_checkout_hash: str,
    merchant_checkout_jwt: str,
    audience: str,
    nonce: str,
    merchant_id: str,
    payment_instrument_id: str,
    successful_occurrences: int,
    spent_minor: int,
    last_used_at: datetime | None,
    strict_constraints: dict[str, Any],
) -> None:
    """Independently verify a stored open-to-closed pair at a role boundary."""
    client = MandateClient()

    def root_key_provider(_token: Any) -> JWK:
        return keys.trusted_surface.public_jwk

    checkout_payloads = client.verify(
        token=checkout_token,
        key_or_provider=root_key_provider,
        expected_aud=audience,
        expected_nonce=nonce,
    )
    payment_payloads = client.verify(
        token=payment_token,
        key_or_provider=root_key_provider,
        expected_aud=audience,
        expected_nonce=nonce,
    )
    if not isinstance(checkout_payloads, list) or not isinstance(payment_payloads, list):
        raise ValueError("AP2 closed mandate chain verification returned an invalid result")
    checkout_chain = CheckoutMandateChain.parse(checkout_payloads)
    payment_chain = PaymentMandateChain.parse(payment_payloads)
    checkout_hash = digest_b64url(merchant_checkout_jwt)
    violations = checkout_chain.verify(
        expected_checkout_hash=checkout_hash,
        checkout_jwt=merchant_checkout_jwt,
    )
    violations.extend(
        payment_chain.verify(
            expected_transaction_id=checkout_hash,
            expected_open_checkout_hash=open_checkout_hash,
            mandate_context=MandateContext(
                total_uses=successful_occurrences,
                total_amount=spent_minor,
                last_used_date=last_used_at.timestamp() if last_used_at else None,
            ),
        )
    )
    violations.extend(
        strict_violations(
            merchant_checkout_jwt=merchant_checkout_jwt,
            checkout_hash=checkout_hash,
            payment=payment_chain.closed_mandate,
            merchant_id=merchant_id,
            payment_instrument_id=payment_instrument_id,
            successful_occurrences=successful_occurrences,
            spent_minor=spent_minor,
            constraints=strict_constraints,
        )
    )
    violations.extend(
        _scheduled_policy_digest_violations(
            open_payment=payment_chain.open_mandate,
            closed_payment=payment_chain.closed_mandate,
            constraints=strict_constraints,
        )
    )
    if violations:
        raise ValueError("; ".join(violations))
    keys.merchant.verify(merchant_checkout_jwt, keys.audience)


def canonical_sha256(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def scheduled_policy_sha256(constraints: dict[str, Any]) -> str:
    """Domain-separated digest of the complete user-approved schedule policy."""
    _validate_scheduled_policy_shape(constraints)
    return canonical_sha256({"domain": _SCHEDULED_POLICY_DOMAIN, "constraints": constraints})


def _validate_scheduled_policy_shape(constraints: dict[str, Any]) -> None:
    required = {
        "version",
        "merchant_id",
        "merchant_name",
        "items",
        "fulfillment_type",
        "address_sha256",
        "location_id",
        "frequency",
        "interval_count",
        "timezone",
        "first_run_at",
        "expires_at",
        "max_occurrences",
        "max_amount_minor",
        "max_total_minor",
        "currency",
    }
    missing = sorted(required.difference(constraints))
    if missing:
        raise ValueError(f"AP2 scheduled policy is missing fields: {', '.join(missing)}")
    items = constraints["items"]
    if not isinstance(items, list) or not items:
        raise ValueError("AP2 scheduled policy requires at least one item")
    item_fields = {
        "id",
        "acceptable_variant_ids",
        "acceptable_items",
        "quantity",
        "modifier_option_ids",
    }
    for item in items:
        if not isinstance(item, dict) or item_fields.difference(item):
            raise ValueError("AP2 scheduled policy contains an invalid item constraint")
        acceptable_variant_ids = item["acceptable_variant_ids"]
        acceptable_items = item["acceptable_items"]
        modifier_option_ids = item["modifier_option_ids"]
        if (
            not isinstance(acceptable_variant_ids, list)
            or not acceptable_variant_ids
            or not isinstance(acceptable_items, list)
            or not isinstance(modifier_option_ids, list)
            or int(item["quantity"]) <= 0
        ):
            raise ValueError("AP2 scheduled policy contains an invalid item constraint")
        described_variant_ids = [
            str(value.get("id")) for value in acceptable_items if isinstance(value, dict)
        ]
        if sorted(str(value) for value in acceptable_variant_ids) != sorted(described_variant_ids):
            raise ValueError("AP2 scheduled item descriptions do not match allowed variants")
    if str(constraints["frequency"]) not in _FREQUENCIES:
        raise ValueError("AP2 scheduled policy has an unsupported frequency")
    if int(constraints["interval_count"]) <= 0:
        raise ValueError("AP2 scheduled policy has an invalid recurrence interval")
    ZoneInfo(str(constraints["timezone"]))
    first_run_at = _parse_datetime(constraints["first_run_at"])
    expires_at = _parse_datetime(constraints["expires_at"])
    if expires_at <= first_run_at:
        raise ValueError("AP2 scheduled policy expires before its first execution")
    if int(constraints["max_occurrences"]) <= 0:
        raise ValueError("AP2 scheduled policy has an invalid occurrence cap")
    max_amount = int(constraints["max_amount_minor"])
    max_total = int(constraints["max_total_minor"])
    if max_amount <= 0 or max_total < max_amount:
        raise ValueError("AP2 scheduled policy has invalid payment bounds")
    if not str(constraints["merchant_id"]) or not str(constraints["location_id"]):
        raise ValueError("AP2 scheduled policy is missing merchant or location")
    if not str(constraints["fulfillment_type"]) or not str(constraints["currency"]):
        raise ValueError("AP2 scheduled policy is missing fulfillment or currency")


def _scheduled_policy_digest_violations(
    *,
    open_payment: OpenPaymentMandate,
    closed_payment: PaymentMandate,
    constraints: dict[str, Any],
) -> list[str]:
    expected = scheduled_policy_sha256(constraints)
    open_policy = (open_payment.risk_data or {}).get(_SCHEDULED_POLICY_RISK_KEY)
    closed_policy = (closed_payment.risk_data or {}).get(_SCHEDULED_POLICY_RISK_KEY)
    expected_claim = {"domain": _SCHEDULED_POLICY_DOMAIN, "sha256": expected}
    violations: list[str] = []
    if open_policy != expected_claim:
        violations.append("AP2 user-authorized scheduled policy digest mismatch")
    if closed_policy != expected_claim:
        violations.append("AP2 delegated scheduled policy digest mismatch")
    return violations


def _validate_open_policy(
    *,
    constraints: dict[str, Any],
    merchant_id: str,
    merchant_name: str,
    item_requirements: list[dict[str, Any]],
    currency: str,
    max_amount_minor: int,
    max_total_minor: int,
    frequency: str,
    max_occurrences: int,
    first_run_at: datetime,
    expires_at: datetime,
) -> None:
    """Reject divergence between native AP2 constraints and the signed policy."""
    expected_items = [
        {
            "id": str(item["id"]),
            "acceptable_items": [
                {"id": str(value["id"]), "title": str(value["title"])}
                for value in item["acceptable_items"]
            ],
            "quantity": int(item["quantity"]),
        }
        for item in constraints.get("items", [])
    ]
    actual_items = [
        {
            "id": str(item["id"]),
            "acceptable_items": [
                {"id": str(value["id"]), "title": str(value["title"])}
                for value in item["acceptable_items"]
            ],
            "quantity": int(item["quantity"]),
        }
        for item in item_requirements
    ]
    expected = {
        "merchant_id": str(merchant_id),
        "merchant_name": merchant_name,
        "items": actual_items,
        "currency": currency.upper(),
        "max_amount_minor": max_amount_minor,
        "max_total_minor": max_total_minor,
        "frequency": frequency,
        "max_occurrences": max_occurrences,
        "first_run_at": first_run_at.isoformat(),
        "expires_at": expires_at.isoformat(),
    }
    actual = {
        "merchant_id": str(constraints.get("merchant_id", "")),
        "merchant_name": constraints.get("merchant_name"),
        "items": expected_items,
        "currency": str(constraints.get("currency", "")).upper(),
        "max_amount_minor": constraints.get("max_amount_minor"),
        "max_total_minor": constraints.get("max_total_minor"),
        "frequency": constraints.get("frequency"),
        "max_occurrences": constraints.get("max_occurrences"),
        "first_run_at": constraints.get("first_run_at"),
        "expires_at": constraints.get("expires_at"),
    }
    if actual != expected:
        raise ValueError("AP2 scheduled policy diverges from native mandate constraints")
