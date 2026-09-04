import base64
import uuid
from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest

from app.protocols.ap2.autonomous import (
    close_and_verify_mandates,
    create_open_mandates,
    derive_agent_key,
    public_jwk,
    scheduled_policy_sha256,
    verify_existing_closed_mandates,
)
from app.protocols.ap2.crypto import AP2KeySet, AP2Signer


def _master_key(byte: int = 7) -> str:
    return base64.urlsafe_b64encode(bytes([byte]) * 32).rstrip(b"=").decode()


def _keys() -> AP2KeySet:
    return AP2KeySet(
        merchant=AP2Signer.generate("urn:test:merchant", "merchant-key-1"),
        trusted_surface=AP2Signer.generate("urn:test:agent-provider", "agent-provider-key-1"),
        credentials_provider=AP2Signer.generate("urn:test:cp", "cp-key-1"),
        payment_processor=AP2Signer.generate("urn:test:processor", "processor-key-1"),
        audience="agentbasket-commerce",
    )


def _checkout_jwt(
    keys: AP2KeySet,
    *,
    merchant_id: str,
    variant_id: str,
    amount_minor: int = 25_000,
    quantity: int = 1,
    modifier_id: str,
    location_id: str,
    address_sha256: str,
    scheduled_for: datetime,
) -> str:
    now = datetime.now(UTC)
    checkout_id = str(uuid.uuid4())
    return keys.merchant.sign(
        {
            "iss": keys.merchant.issuer,
            "aud": keys.audience,
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(minutes=10)).timestamp()),
            "jti": f"checkout:{uuid.uuid4()}",
            "id": checkout_id,
            "merchant": {"id": merchant_id, "name": "Ember & Leaf"},
            "line_items": [
                {
                    "id": "line-1",
                    "item": {
                        "id": variant_id,
                        "title": "Ginger Tea",
                        "price": amount_minor // quantity,
                    },
                    "quantity": quantity,
                    "totals": [{"type": "total", "amount": amount_minor}],
                    "agentbasket": {"modifier_option_ids": [modifier_id]},
                }
            ],
            "status": "incomplete",
            "currency": "INR",
            "totals": [{"type": "total", "amount": amount_minor}],
            "links": [
                {
                    "type": "privacy_policy",
                    "url": "https://agentbasket.local/privacy",
                    "title": "Privacy policy",
                }
            ],
            "agentbasket": {
                "source": "scheduled_agent",
                "fulfillment_type": "local_delivery",
                "location_id": location_id,
                "address_sha256": address_sha256,
                "scheduled_for": scheduled_for.isoformat(),
            },
        }
    )


def _strict_constraints(
    variant_id: str,
    *,
    merchant_id: str,
    modifier_id: str,
    location_id: str,
    address_sha256: str,
    first_run: datetime,
    expires_at: datetime,
) -> dict:
    return {
        "version": 1,
        "merchant_id": merchant_id,
        "merchant_name": "Ember & Leaf",
        "items": [
            {
                "id": "scheduled-line-1",
                "acceptable_variant_ids": [variant_id],
                "acceptable_items": [{"id": variant_id, "title": "Ginger Tea"}],
                "quantity": 1,
                "modifier_option_ids": [modifier_id],
            }
        ],
        "fulfillment_type": "local_delivery",
        "location_id": location_id,
        "address_sha256": address_sha256,
        "frequency": "weekly",
        "interval_count": 1,
        "timezone": "Asia/Kolkata",
        "first_run_at": first_run.isoformat(),
        "expires_at": expires_at.isoformat(),
        "currency": "INR",
        "max_amount_minor": 30_000,
        "max_total_minor": 90_000,
        "max_occurrences": 3,
    }


def test_derived_agent_key_is_stable_scoped_and_public_only_when_exported() -> None:
    first_intent = uuid.uuid4()
    first = derive_agent_key(_master_key(), first_intent)
    repeated = derive_agent_key(_master_key(), first_intent)
    different = derive_agent_key(_master_key(), uuid.uuid4())

    assert first.export_private() == repeated.export_private()
    assert first.export_private() != different.export_private()
    assert public_jwk(first) == public_jwk(repeated)
    assert "d" not in public_jwk(first)
    assert public_jwk(first)["kid"] == f"agentbasket-schedule-{first_intent.hex}"


@pytest.mark.parametrize("master", ["", _master_key()[:-2], "not-base64url!!"])
def test_derived_agent_key_rejects_invalid_master_material(master: str) -> None:
    with pytest.raises(ValueError, match="master key"):
        derive_agent_key(master, uuid.uuid4())


def test_open_authorization_can_be_closed_and_independently_verified() -> None:
    keys = _keys()
    intent_id = uuid.uuid4()
    merchant_id = str(uuid.uuid4())
    variant_id = str(uuid.uuid4())
    modifier_id = str(uuid.uuid4())
    location_id = str(uuid.uuid4())
    address_sha256 = "a" * 64
    instrument_id = str(uuid.uuid4())
    now = datetime.now(UTC)
    first_run = now + timedelta(days=2)
    expires_at = now + timedelta(days=30)
    agent_key = derive_agent_key(_master_key(), intent_id)
    constraints = _strict_constraints(
        variant_id,
        merchant_id=merchant_id,
        modifier_id=modifier_id,
        location_id=location_id,
        address_sha256=address_sha256,
        first_run=first_run,
        expires_at=expires_at,
    )
    open_bundle = create_open_mandates(
        keys=keys,
        agent_key=agent_key,
        merchant_id=merchant_id,
        merchant_name="Ember & Leaf",
        item_requirements=[
            {
                "id": "scheduled-line-1",
                "acceptable_items": [{"id": variant_id, "title": "Ginger Tea"}],
                "quantity": 1,
            }
        ],
        payment_instrument_id=instrument_id,
        payment_instrument_type="com.razorpay.upi.autopay",
        currency="INR",
        max_amount_minor=30_000,
        max_total_minor=90_000,
        frequency="weekly",
        max_occurrences=3,
        first_run_at=first_run,
        expires_at=expires_at,
        strict_constraints=constraints,
    )
    checkout_jwt = _checkout_jwt(
        keys,
        merchant_id=merchant_id,
        variant_id=variant_id,
        modifier_id=modifier_id,
        location_id=location_id,
        address_sha256=address_sha256,
        scheduled_for=first_run,
    )
    nonce = "scheduled-run-nonce-0000000001"
    closed = close_and_verify_mandates(
        keys=keys,
        agent_key=agent_key,
        open_checkout_token=open_bundle.checkout_token,
        open_payment_token=open_bundle.payment_token,
        open_checkout_hash=open_bundle.checkout_hash,
        merchant_checkout_jwt=checkout_jwt,
        amount_minor=25_000,
        currency="INR",
        merchant_id=merchant_id,
        merchant_name="Ember & Leaf",
        payment_instrument_id=instrument_id,
        payment_instrument_type="com.razorpay.upi.autopay",
        execution_at=first_run,
        audience=keys.audience,
        nonce=nonce,
        successful_occurrences=0,
        spent_minor=0,
        last_used_at=None,
        strict_constraints=constraints,
    )

    assert closed.checkout_hash
    assert "~" in closed.checkout_token
    assert "~" in closed.payment_token
    assert open_bundle.policy_sha256 == scheduled_policy_sha256(constraints)
    verify_existing_closed_mandates(
        keys=keys,
        checkout_token=closed.checkout_token,
        payment_token=closed.payment_token,
        open_checkout_hash=open_bundle.checkout_hash,
        merchant_checkout_jwt=checkout_jwt,
        audience=keys.audience,
        nonce=nonce,
        merchant_id=merchant_id,
        payment_instrument_id=instrument_id,
        successful_occurrences=0,
        spent_minor=0,
        last_used_at=None,
        strict_constraints=constraints,
    )

    changed_constraints = deepcopy(constraints)
    changed_modifier_id = str(uuid.uuid4())
    changed_constraints["items"][0]["modifier_option_ids"] = [changed_modifier_id]
    changed_checkout_jwt = _checkout_jwt(
        keys,
        merchant_id=merchant_id,
        variant_id=variant_id,
        modifier_id=changed_modifier_id,
        location_id=location_id,
        address_sha256=address_sha256,
        scheduled_for=first_run,
    )
    with pytest.raises(ValueError, match="user-authorized scheduled policy digest"):
        close_and_verify_mandates(
            keys=keys,
            agent_key=agent_key,
            open_checkout_token=open_bundle.checkout_token,
            open_payment_token=open_bundle.payment_token,
            open_checkout_hash=open_bundle.checkout_hash,
            merchant_checkout_jwt=changed_checkout_jwt,
            amount_minor=25_000,
            currency="INR",
            merchant_id=merchant_id,
            merchant_name="Ember & Leaf",
            payment_instrument_id=instrument_id,
            payment_instrument_type="com.razorpay.upi.autopay",
            execution_at=first_run,
            audience=keys.audience,
            nonce="scheduled-run-policy-tamper-0001",
            successful_occurrences=0,
            spent_minor=0,
            last_used_at=None,
            strict_constraints=changed_constraints,
        )

    changed_constraints = deepcopy(constraints)
    changed_constraints["max_total_minor"] = 120_000
    with pytest.raises(ValueError, match="user-authorized scheduled policy digest"):
        verify_existing_closed_mandates(
            keys=keys,
            checkout_token=closed.checkout_token,
            payment_token=closed.payment_token,
            open_checkout_hash=open_bundle.checkout_hash,
            merchant_checkout_jwt=checkout_jwt,
            audience=keys.audience,
            nonce=nonce,
            merchant_id=merchant_id,
            payment_instrument_id=instrument_id,
            successful_occurrences=0,
            spent_minor=0,
            last_used_at=None,
            strict_constraints=changed_constraints,
        )


def test_later_calendar_slot_remains_valid_after_an_unsuccessful_attempt() -> None:
    keys = _keys()
    intent_id = uuid.uuid4()
    merchant_id = str(uuid.uuid4())
    variant_id = str(uuid.uuid4())
    modifier_id = str(uuid.uuid4())
    location_id = str(uuid.uuid4())
    address_sha256 = "c" * 64
    instrument_id = str(uuid.uuid4())
    now = datetime.now(UTC)
    first_run = now + timedelta(days=2)
    second_run = first_run + timedelta(weeks=1)
    expires_at = now + timedelta(days=30)
    agent_key = derive_agent_key(_master_key(), intent_id)
    constraints = _strict_constraints(
        variant_id,
        merchant_id=merchant_id,
        modifier_id=modifier_id,
        location_id=location_id,
        address_sha256=address_sha256,
        first_run=first_run,
        expires_at=expires_at,
    )
    open_bundle = create_open_mandates(
        keys=keys,
        agent_key=agent_key,
        merchant_id=merchant_id,
        merchant_name="Ember & Leaf",
        item_requirements=[
            {
                "id": "scheduled-line-1",
                "acceptable_items": [{"id": variant_id, "title": "Ginger Tea"}],
                "quantity": 1,
            }
        ],
        payment_instrument_id=instrument_id,
        payment_instrument_type="com.razorpay.upi.autopay",
        currency="INR",
        max_amount_minor=30_000,
        max_total_minor=90_000,
        frequency="weekly",
        max_occurrences=3,
        first_run_at=first_run,
        expires_at=expires_at,
        strict_constraints=constraints,
    )
    checkout_jwt = _checkout_jwt(
        keys,
        merchant_id=merchant_id,
        variant_id=variant_id,
        modifier_id=modifier_id,
        location_id=location_id,
        address_sha256=address_sha256,
        scheduled_for=second_run,
    )

    closed = close_and_verify_mandates(
        keys=keys,
        agent_key=agent_key,
        open_checkout_token=open_bundle.checkout_token,
        open_payment_token=open_bundle.payment_token,
        open_checkout_hash=open_bundle.checkout_hash,
        merchant_checkout_jwt=checkout_jwt,
        amount_minor=25_000,
        currency="INR",
        merchant_id=merchant_id,
        merchant_name="Ember & Leaf",
        payment_instrument_id=instrument_id,
        payment_instrument_type="com.razorpay.upi.autopay",
        execution_at=second_run,
        audience=keys.audience,
        nonce="scheduled-run-after-failure-0001",
        successful_occurrences=0,
        spent_minor=0,
        last_used_at=None,
        strict_constraints=constraints,
    )

    assert closed.checkout_hash


def test_closing_rejects_checkout_outside_signed_budget() -> None:
    keys = _keys()
    intent_id = uuid.uuid4()
    merchant_id = str(uuid.uuid4())
    variant_id = str(uuid.uuid4())
    modifier_id = str(uuid.uuid4())
    location_id = str(uuid.uuid4())
    address_sha256 = "b" * 64
    instrument_id = str(uuid.uuid4())
    now = datetime.now(UTC)
    first_run = now + timedelta(days=2)
    expires_at = now + timedelta(days=30)
    agent_key = derive_agent_key(_master_key(), intent_id)
    constraints = _strict_constraints(
        variant_id,
        merchant_id=merchant_id,
        modifier_id=modifier_id,
        location_id=location_id,
        address_sha256=address_sha256,
        first_run=first_run,
        expires_at=expires_at,
    )
    open_bundle = create_open_mandates(
        keys=keys,
        agent_key=agent_key,
        merchant_id=merchant_id,
        merchant_name="Ember & Leaf",
        item_requirements=[
            {
                "id": "scheduled-line-1",
                "acceptable_items": [{"id": variant_id, "title": "Ginger Tea"}],
                "quantity": 1,
            }
        ],
        payment_instrument_id=instrument_id,
        payment_instrument_type="com.razorpay.upi.autopay",
        currency="INR",
        max_amount_minor=30_000,
        max_total_minor=90_000,
        frequency="weekly",
        max_occurrences=3,
        first_run_at=first_run,
        expires_at=expires_at,
        strict_constraints=constraints,
    )
    checkout_jwt = _checkout_jwt(
        keys,
        merchant_id=merchant_id,
        variant_id=variant_id,
        amount_minor=31_000,
        modifier_id=modifier_id,
        location_id=location_id,
        address_sha256=address_sha256,
        scheduled_for=first_run,
    )

    with pytest.raises(ValueError, match="amount constraint"):
        close_and_verify_mandates(
            keys=keys,
            agent_key=agent_key,
            open_checkout_token=open_bundle.checkout_token,
            open_payment_token=open_bundle.payment_token,
            open_checkout_hash=open_bundle.checkout_hash,
            merchant_checkout_jwt=checkout_jwt,
            amount_minor=31_000,
            currency="INR",
            merchant_id=merchant_id,
            merchant_name="Ember & Leaf",
            payment_instrument_id=instrument_id,
            payment_instrument_type="com.razorpay.upi.autopay",
            execution_at=first_run,
            audience=keys.audience,
            nonce="scheduled-run-nonce-0000000002",
            successful_occurrences=0,
            spent_minor=0,
            last_used_at=None,
            strict_constraints=constraints,
        )
