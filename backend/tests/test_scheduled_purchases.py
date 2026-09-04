from __future__ import annotations

import base64
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy.orm import Session, sessionmaker

from app.api.dependencies import get_trusted_surface
from app.core.config import Settings
from app.db.models import (
    PaymentInstrument,
    ScheduledPurchaseIntent,
    ScheduledPurchaseRun,
    UserAccount,
)
from app.domain.enums import PurchaseIntentStatus, ScheduledRunStatus
from app.main import app
from app.protocols.ap2.crypto import AP2KeySet, AP2Signer, get_ap2_key_set
from app.services.scheduled_purchase import advance_schedule


class FakeTrustedSurface:
    def authentication_options(
        self, user: UserAccount, challenge_value: str | None = None
    ) -> tuple[str, dict[str, Any]]:
        del user
        challenge = challenge_value or "scheduled-test-webauthn-challenge"
        return challenge, {
            "challenge": challenge,
            "rpId": "localhost",
            "allowCredentials": [{"id": "scheduled-test-passkey", "type": "public-key"}],
            "userVerification": "required",
        }

    def verify_authentication(
        self,
        user: UserAccount,
        expected_challenge: str,
        credential_payload: dict[str, Any],
    ) -> object:
        del user
        assert expected_challenge == "scheduled-test-webauthn-challenge"
        assert credential_payload == {"id": "scheduled-test-passkey"}
        return object()


def test_monthly_schedule_stays_anchored_after_a_short_month() -> None:
    first = datetime.fromisoformat("2027-01-31T09:00:00+05:30")
    february = advance_schedule(first, "monthly", 1, "Asia/Kolkata", anchor=first)
    assert february is not None
    march = advance_schedule(february, "monthly", 1, "Asia/Kolkata", anchor=first)

    assert february.astimezone(UTC).isoformat() == "2027-02-28T03:30:00+00:00"
    assert march is not None
    assert march.astimezone(UTC).isoformat() == "2027-03-31T03:30:00+00:00"


def _keys() -> AP2KeySet:
    return AP2KeySet(
        merchant=AP2Signer.generate("urn:test:merchant", "merchant-key-1"),
        trusted_surface=AP2Signer.generate("urn:test:agent-provider", "agent-provider-key-1"),
        credentials_provider=AP2Signer.generate("urn:test:cp", "cp-key-1"),
        payment_processor=AP2Signer.generate("urn:test:processor", "processor-key-1"),
        audience="agentbasket-commerce",
    )


def _settings(*, recurring_enabled: bool = False) -> Settings:
    master = base64.urlsafe_b64encode(b"scheduled-purchase-test-master!!"[:32]).rstrip(b"=")
    assert len(base64.urlsafe_b64decode(master + b"=" * (-len(master) % 4))) == 32
    return Settings(
        _env_file=None,
        app_env="test",
        razorpay_recurring_enabled=recurring_enabled,
        razorpay_recurring_notification_lead_hours=25,
        razorpay_recurring_unattended_limit_minor=1_500_000,
        ap2_autonomous_agent_master_key=master.decode(),
    )


@pytest.mark.anyio
async def test_scheduled_draft_rejects_amount_above_unattended_provider_limit(
    client: httpx.AsyncClient,
    seeded: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_dependencies(monkeypatch)
    body = _draft_body(seeded, total_budget_minor=2_000_000)
    body["max_amount_minor"] = 1_500_001

    response = await client.post(
        "/api/v1/scheduled-purchases",
        headers={
            **_auth(seeded),
            "Idempotency-Key": "schedule-unattended-cap-001",
        },
        json=body,
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "scheduled_unattended_cap_exceeded"


@pytest.mark.anyio
async def test_scheduled_draft_requires_provider_notification_lead_time(
    client: httpx.AsyncClient,
    seeded: dict[str, object],
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_dependencies(monkeypatch)
    instrument_id = _autopay_instrument(session_factory, seeded)
    body = _draft_body(seeded, payment_instrument_id=instrument_id)
    first_run = datetime.now(UTC) + timedelta(hours=2)
    body["first_run_at"] = first_run.isoformat()
    body["expires_at"] = (first_run + timedelta(days=7)).isoformat()

    response = await client.post(
        "/api/v1/scheduled-purchases",
        headers={**_auth(seeded), "Idempotency-Key": "schedule-too-soon-001"},
        json=body,
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "scheduled_notification_window_too_short"


def _auth(seeded: dict[str, object]) -> dict[str, str]:
    return {"Authorization": f"Bearer {seeded['customer_token']}"}


async def _merchant_auth(client: httpx.AsyncClient, seeded: dict[str, object]) -> dict[str, str]:
    response = await client.post(
        "/api/v1/auth/login",
        json={
            "email": seeded["merchant_admin_email"],
            "password": seeded["merchant_admin_password"],
        },
    )
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _autopay_instrument(
    session_factory: sessionmaker[Session], seeded: dict[str, object]
) -> object:
    with session_factory() as db, db.begin():
        instrument = PaymentInstrument(
            user_id=seeded["customer_id"],
            provider="razorpay_test",
            instrument_type="com.razorpay.upi.autopay",
            alias="Razorpay UPI Autopay",
            status="active",
            is_default=False,
            instrument_metadata={"mode": "test", "token_status": "not_started"},
        )
        db.add(instrument)
        db.flush()
        return instrument.id


def _draft_body(
    seeded: dict[str, object],
    *,
    total_budget_minor: int = 90_000,
    payment_instrument_id: object | None = None,
) -> dict:
    first_run = datetime.now(UTC) + timedelta(days=3)
    return {
        "merchant_slug": "ember-and-leaf",
        "fulfillment_type": "local_delivery",
        "address_id": str(seeded["customer_address_id"]),
        "payment_instrument_id": str(payment_instrument_id or seeded["payment_instrument_id"]),
        "items": [
            {
                "acceptable_variant_ids": [str(seeded["drink_variant_id"])],
                "quantity": 1,
                "modifier_option_ids": [str(seeded["unsweetened_id"])],
            }
        ],
        "frequency": "weekly",
        "interval_count": 1,
        "timezone": "Asia/Kolkata",
        "first_run_at": first_run.isoformat(),
        "expires_at": (first_run + timedelta(days=30)).isoformat(),
        "max_occurrences": 3,
        "max_amount_minor": 30_000,
        "max_total_minor": total_budget_minor,
        "currency": "INR",
    }


def _enable_dependencies(monkeypatch: pytest.MonkeyPatch) -> None:
    keys = _keys()
    settings = _settings()
    app.dependency_overrides[get_ap2_key_set] = lambda: keys
    app.dependency_overrides[get_trusted_surface] = lambda: FakeTrustedSurface()
    monkeypatch.setattr("app.services.scheduled_purchase.get_settings", lambda: settings)


@pytest.mark.anyio
async def test_scheduled_draft_is_idempotent_and_rejects_key_reuse(
    client: httpx.AsyncClient,
    seeded: dict[str, object],
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_dependencies(monkeypatch)
    headers = {**_auth(seeded), "Idempotency-Key": "schedule-draft-idempotency-001"}
    instrument_id = _autopay_instrument(session_factory, seeded)
    body = _draft_body(seeded, payment_instrument_id=instrument_id)

    created = await client.post("/api/v1/scheduled-purchases", headers=headers, json=body)
    repeated = await client.post("/api/v1/scheduled-purchases", headers=headers, json=body)

    assert created.status_code == 201
    assert repeated.status_code == 201
    assert repeated.json()["id"] == created.json()["id"]
    assert created.json()["status"] == "draft"
    assert created.json()["provider_ready"] is False
    changed = _draft_body(
        seeded,
        total_budget_minor=120_000,
        payment_instrument_id=instrument_id,
    )
    changed["first_run_at"] = body["first_run_at"]
    changed["expires_at"] = body["expires_at"]
    rejected = await client.post("/api/v1/scheduled-purchases", headers=headers, json=changed)
    assert rejected.status_code == 409
    assert rejected.json()["error"]["code"] == "idempotency_key_reused"


@pytest.mark.anyio
async def test_customer_authorizes_once_then_can_pause_resume_and_revoke(
    client: httpx.AsyncClient,
    seeded: dict[str, object],
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_dependencies(monkeypatch)
    instrument_id = _autopay_instrument(session_factory, seeded)
    created = await client.post(
        "/api/v1/scheduled-purchases",
        headers={**_auth(seeded), "Idempotency-Key": "schedule-lifecycle-draft-001"},
        json=_draft_body(seeded, payment_instrument_id=instrument_id),
    )
    assert created.status_code == 201
    intent_id = uuid.UUID(created.json()["id"])
    challenge = await client.post(
        f"/api/v1/scheduled-purchases/{intent_id}/authorization/challenge",
        headers={**_auth(seeded), "Idempotency-Key": "schedule-challenge-001"},
    )
    assert challenge.status_code == 201
    terms = challenge.json()
    assert terms["display_sha256"] == created.json()["display_sha256"]
    assert terms["display"]["human_not_present"] is True
    assert "d" not in terms["agent_public_jwk"]
    authorization_body = {
        "nonce": terms["nonce"],
        "display_sha256": terms["display_sha256"],
        "webauthn_credential": {"id": "scheduled-test-passkey"},
    }
    approval_headers = {
        **_auth(seeded),
        "Idempotency-Key": "schedule-authorization-001",
    }
    approved = await client.post(
        f"/api/v1/scheduled-purchases/{intent_id}/authorization/approve",
        headers=approval_headers,
        json=authorization_body,
    )
    repeated = await client.post(
        f"/api/v1/scheduled-purchases/{intent_id}/authorization/approve",
        headers=approval_headers,
        json=authorization_body,
    )
    assert approved.status_code == 200
    assert repeated.status_code == 200
    assert approved.json()["scheduled_purchase"]["status"] == ("pending_provider_authorization")
    assert approved.json()["open_checkout_mandate"] == repeated.json()["open_checkout_mandate"]
    assert approved.json()["open_payment_mandate"] == repeated.json()["open_payment_mandate"]

    paused = await client.post(
        f"/api/v1/scheduled-purchases/{intent_id}/pause",
        headers={**_auth(seeded), "Idempotency-Key": "schedule-pause-001"},
    )
    resumed = await client.post(
        f"/api/v1/scheduled-purchases/{intent_id}/resume",
        headers={**_auth(seeded), "Idempotency-Key": "schedule-resume-001"},
    )
    revoked = await client.post(
        f"/api/v1/scheduled-purchases/{intent_id}/revoke",
        headers={**_auth(seeded), "Idempotency-Key": "schedule-revoke-001"},
    )
    assert paused.status_code == 200 and paused.json()["status"] == "paused"
    assert resumed.status_code == 200
    assert resumed.json()["status"] == "pending_provider_authorization"
    assert revoked.status_code == 200 and revoked.json()["status"] == "revoked"


@pytest.mark.anyio
async def test_scheduled_draft_requires_upi_autopay_instrument(
    client: httpx.AsyncClient,
    seeded: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_dependencies(monkeypatch)

    response = await client.post(
        "/api/v1/scheduled-purchases",
        headers={**_auth(seeded), "Idempotency-Key": "schedule-standard-payment-001"},
        json=_draft_body(seeded),
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "scheduled_autopay_instrument_required"


@pytest.mark.anyio
async def test_expired_schedule_state_commits_before_challenge_error(
    client: httpx.AsyncClient,
    seeded: dict[str, object],
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_dependencies(monkeypatch)
    instrument_id = _autopay_instrument(session_factory, seeded)
    created = await client.post(
        "/api/v1/scheduled-purchases",
        headers={**_auth(seeded), "Idempotency-Key": "schedule-expired-draft-001"},
        json=_draft_body(seeded, payment_instrument_id=instrument_id),
    )
    assert created.status_code == 201
    intent_id = uuid.UUID(created.json()["id"])
    with session_factory() as db, db.begin():
        intent = db.get(ScheduledPurchaseIntent, intent_id)
        assert intent is not None
        intent.expires_at = datetime.now(UTC) - timedelta(seconds=1)

    response = await client.post(
        f"/api/v1/scheduled-purchases/{intent_id}/authorization/challenge",
        headers={**_auth(seeded), "Idempotency-Key": "schedule-expired-challenge-001"},
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "scheduled_purchase_expired"
    with session_factory() as db:
        intent = db.get(ScheduledPurchaseIntent, intent_id)
        assert intent is not None and intent.status.value == "expired"


@pytest.mark.anyio
async def test_authorization_rechecks_upi_autopay_before_signing(
    client: httpx.AsyncClient,
    seeded: dict[str, object],
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_dependencies(monkeypatch)
    instrument_id = _autopay_instrument(session_factory, seeded)
    created = await client.post(
        "/api/v1/scheduled-purchases",
        headers={**_auth(seeded), "Idempotency-Key": "schedule-sign-guard-draft-001"},
        json=_draft_body(seeded, payment_instrument_id=instrument_id),
    )
    intent_id = created.json()["id"]
    challenge = await client.post(
        f"/api/v1/scheduled-purchases/{intent_id}/authorization/challenge",
        headers={**_auth(seeded), "Idempotency-Key": "schedule-sign-guard-challenge-001"},
    )
    assert challenge.status_code == 201
    with session_factory() as db, db.begin():
        instrument = db.get(PaymentInstrument, instrument_id)
        assert instrument is not None
        instrument.instrument_type = "com.example.not-autopay"

    terms = challenge.json()
    response = await client.post(
        f"/api/v1/scheduled-purchases/{intent_id}/authorization/approve",
        headers={**_auth(seeded), "Idempotency-Key": "schedule-sign-guard-approve-001"},
        json={
            "nonce": terms["nonce"],
            "display_sha256": terms["display_sha256"],
            "webauthn_credential": {"id": "scheduled-test-passkey"},
        },
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "scheduled_autopay_instrument_required"


@pytest.mark.anyio
async def test_authorization_rejects_policy_changed_after_display(
    client: httpx.AsyncClient,
    seeded: dict[str, object],
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_dependencies(monkeypatch)
    instrument_id = _autopay_instrument(session_factory, seeded)
    created = await client.post(
        "/api/v1/scheduled-purchases",
        headers={**_auth(seeded), "Idempotency-Key": "schedule-policy-draft-001"},
        json=_draft_body(seeded, payment_instrument_id=instrument_id),
    )
    assert created.status_code == 201
    intent_id = uuid.UUID(created.json()["id"])
    challenge = await client.post(
        f"/api/v1/scheduled-purchases/{intent_id}/authorization/challenge",
        headers={**_auth(seeded), "Idempotency-Key": "schedule-policy-challenge-001"},
    )
    assert challenge.status_code == 201
    with session_factory() as db, db.begin():
        intent = db.get(ScheduledPurchaseIntent, intent_id)
        assert intent is not None
        changed = dict(intent.constraints)
        changed["max_total_minor"] = int(changed["max_total_minor"]) + 1
        intent.constraints = changed

    terms = challenge.json()
    response = await client.post(
        f"/api/v1/scheduled-purchases/{intent_id}/authorization/approve",
        headers={**_auth(seeded), "Idempotency-Key": "schedule-policy-approve-001"},
        json={
            "nonce": terms["nonce"],
            "display_sha256": terms["display_sha256"],
            "webauthn_credential": {"id": "scheduled-test-passkey"},
        },
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == ("scheduled_authorization_policy_mismatch")


@pytest.mark.anyio
async def test_merchant_schedule_list_excludes_legacy_rows_missing_hnp_dependencies(
    client: httpx.AsyncClient,
    seeded: dict[str, object],
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as db, db.begin():
        db.add(
            ScheduledPurchaseIntent(
                merchant_id=seeded["merchant_id"],
                customer_id=None,
                customer_reference="legacy-pre-hnp-schedule",
                constraints={"legacy": True},
                fulfillment_type=None,
                display_payload=None,
            )
        )

    response = await client.get(
        "/api/v1/merchant/operations/scheduled-purchases",
        headers=await _merchant_auth(client, seeded),
    )

    assert response.status_code == 200
    assert response.json() == []


@pytest.mark.anyio
async def test_resume_advances_past_a_finished_occurrence_and_rebinds_token(
    client: httpx.AsyncClient,
    seeded: dict[str, object],
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_dependencies(monkeypatch)
    settings = _settings(recurring_enabled=True)
    monkeypatch.setattr("app.services.scheduled_purchase.get_settings", lambda: settings)
    monkeypatch.setattr("app.services.credentials_provider.get_settings", lambda: settings)
    instrument_id = _autopay_instrument(session_factory, seeded)
    created = await client.post(
        "/api/v1/scheduled-purchases",
        headers={**_auth(seeded), "Idempotency-Key": "schedule-recover-draft-001"},
        json=_draft_body(seeded, payment_instrument_id=instrument_id),
    )
    assert created.status_code == 201
    intent_id = uuid.UUID(created.json()["id"])

    with session_factory() as db, db.begin():
        intent = db.get(ScheduledPurchaseIntent, intent_id)
        instrument = db.get(PaymentInstrument, instrument_id)
        assert intent is not None and instrument is not None
        first_execution = intent.next_execution_at
        instrument.provider_customer_id = "cust_resume_test"
        instrument.provider_token_reference = "token_resume_test"
        instrument.instrument_metadata = {
            "token_status": "confirmed",
            "mandate_max_amount_minor": intent.max_amount_minor,
            "mandate_expires_at": int(
                intent.expires_at.replace(tzinfo=UTC).timestamp()
                if intent.expires_at.tzinfo is None
                else intent.expires_at.timestamp()
            ),
        }
        intent.status = PurchaseIntentStatus.NEEDS_ATTENTION
        intent.open_checkout_mandate = "signed-open-checkout"
        intent.open_payment_mandate = "signed-open-payment"
        intent.next_run_at = None
        db.add(
            ScheduledPurchaseRun(
                intent_id=intent.id,
                scheduled_for=first_execution,
                status=ScheduledRunStatus.FAILED,
                idempotency_key=f"schedule:{intent.id}:failed",
                currency=intent.currency,
                failure_code="scheduled_item_unavailable",
            )
        )

    resumed = await client.post(
        f"/api/v1/scheduled-purchases/{intent_id}/resume",
        headers={**_auth(seeded), "Idempotency-Key": "schedule-recover-resume-001"},
    )

    assert resumed.status_code == 200
    assert resumed.json()["status"] == "active"
    with session_factory() as db:
        intent = db.get(ScheduledPurchaseIntent, intent_id)
        assert intent is not None
        assert intent.next_execution_at - first_execution == timedelta(days=7)
        assert intent.payment_token_reference == "token_resume_test"
        assert intent.provider_authorized_at is not None
