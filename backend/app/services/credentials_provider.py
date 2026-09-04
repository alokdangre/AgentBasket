from __future__ import annotations

import hashlib
import json
import secrets
import uuid
from datetime import UTC, datetime

from ap2.sdk.generated.payment_mandate import PaymentMandate
from ap2.sdk.mandate import MandateClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import ConflictError, NotFoundError
from app.db.models import (
    Ap2ConsentChallenge,
    Ap2Mandate,
    Checkout,
    PaymentCredentialGrant,
    PaymentInstrument,
    ScheduledPurchaseIntent,
    ScheduledPurchaseRun,
    UserAccount,
)
from app.protocols.ap2.autonomous import verify_existing_closed_mandates
from app.protocols.ap2.crypto import AP2KeySet, get_ap2_key_set
from app.protocols.ap2.models import PaymentCredentialGrantOut
from app.schemas.trusted_surface import PaymentInstrumentListOut, PaymentInstrumentOut


def utc_now() -> datetime:
    return datetime.now(UTC)


def recurring_instrument_ready(
    instrument: PaymentInstrument,
    intent: ScheduledPurchaseIntent | None = None,
) -> bool:
    settings = get_settings()
    metadata = instrument.instrument_metadata or {}
    ready = bool(
        settings.razorpay_recurring_enabled
        and instrument.instrument_type == "com.razorpay.upi.autopay"
        and instrument.provider_customer_id
        and instrument.provider_token_reference
        and metadata.get("token_status") == "confirmed"
    )
    if not ready or intent is None:
        return ready
    try:
        mandate_max = int(metadata["mandate_max_amount_minor"])
        mandate_expiry = int(metadata["mandate_expires_at"])
    except (KeyError, TypeError, ValueError):
        return False
    if intent.expires_at is None:
        return False
    expires_at = (
        intent.expires_at if intent.expires_at.tzinfo else intent.expires_at.replace(tzinfo=UTC)
    )
    return mandate_max >= intent.max_amount_minor and mandate_expiry >= int(expires_at.timestamp())


class CredentialsProviderService:
    """Deterministic CP: validates a mandate and releases only a scoped grant."""

    def __init__(self, db: Session, keys: AP2KeySet | None = None) -> None:
        self.db = db
        self._keys = keys

    @property
    def keys(self) -> AP2KeySet:
        if self._keys is None:
            self._keys = get_ap2_key_set()
        return self._keys

    def list_instruments(self, user: UserAccount) -> PaymentInstrumentListOut:
        with self.db.begin():
            rows = list(
                self.db.scalars(
                    select(PaymentInstrument)
                    .where(
                        PaymentInstrument.user_id == user.id,
                        PaymentInstrument.status == "active",
                    )
                    .order_by(PaymentInstrument.is_default.desc(), PaymentInstrument.created_at)
                )
            )
            settings = get_settings()
            test_mode = settings.app_env.casefold() != "production" or (
                settings.razorpay_key_id or ""
            ).startswith("rzp_test_")
            if not any(row.instrument_type.startswith("com.razorpay.standard") for row in rows):
                instrument = PaymentInstrument(
                    user_id=user.id,
                    provider="razorpay_test" if test_mode else "razorpay",
                    instrument_type=(
                        "com.razorpay.standard.test" if test_mode else "com.razorpay.standard"
                    ),
                    alias="Razorpay Test Checkout" if test_mode else "Razorpay Checkout",
                    status="active",
                    is_default=True,
                    instrument_metadata={
                        "mode": "test" if test_mode else "live",
                        "requires_provider_checkout": True,
                        "stores_pan": False,
                    },
                )
                self.db.add(instrument)
                self.db.flush()
                rows.append(instrument)
            if settings.razorpay_recurring_enabled and not any(
                row.instrument_type == "com.razorpay.upi.autopay" for row in rows
            ):
                recurring = PaymentInstrument(
                    user_id=user.id,
                    provider="razorpay_test" if test_mode else "razorpay",
                    instrument_type="com.razorpay.upi.autopay",
                    alias="Razorpay UPI Autopay",
                    status="active",
                    is_default=False,
                    instrument_metadata={
                        "mode": "test" if test_mode else "live",
                        "requires_provider_checkout": True,
                        "stores_pan": False,
                        "token_status": "not_started",
                    },
                )
                self.db.add(recurring)
                self.db.flush()
                rows.append(recurring)
            return PaymentInstrumentListOut(
                payment_instruments=[self.instrument_out(row) for row in rows]
            )

    def issue_grant(
        self,
        challenge: Ap2ConsentChallenge,
        checkout: Checkout,
        customer: UserAccount,
        payment_mandate_record: Ap2Mandate,
    ) -> PaymentCredentialGrant:
        verified = (
            MandateClient()
            .verify(
                token=payment_mandate_record.signed_jwt,
                key_or_provider=self.keys.trusted_surface.public_jwk,
                payload_type=PaymentMandate,
            )
            .mandate_payload
        )
        instrument = self.db.scalar(
            select(PaymentInstrument).where(
                PaymentInstrument.id == challenge.payment_instrument_id,
                PaymentInstrument.user_id == customer.id,
                PaymentInstrument.status == "active",
            )
        )
        if instrument is None:
            raise NotFoundError("payment_instrument_not_found", "Payment method was not found.")
        if (
            verified.transaction_id != challenge.checkout_hash
            or verified.payment_instrument.id != str(instrument.id)
            or verified.payment_amount.amount != checkout.total_minor
            or verified.payment_amount.currency.upper() != checkout.currency.upper()
        ):
            raise ConflictError(
                "credential_scope_mismatch", "The payment mandate does not match the credential."
            )
        token = secrets.token_urlsafe(48)
        grant_id = uuid.uuid4()
        signed = self.keys.credentials_provider.sign(
            {
                "iss": self.keys.credentials_provider.issuer,
                "sub": str(customer.id),
                "aud": self.keys.audience,
                "jti": f"ap2-cp-grant:{grant_id}",
                "iat": int(utc_now().timestamp()),
                "exp": int(self._as_utc(challenge.expires_at).timestamp()),
                "grant_id": str(grant_id),
                "checkout_id": str(checkout.id),
                "checkout_hash": challenge.checkout_hash,
                "payment_instrument_id": str(instrument.id),
                "credential_kind": "razorpay_hosted_checkout_authorization",
                "amount_minor": checkout.total_minor,
                "currency": checkout.currency,
                "bearer_token_sha256": hashlib.sha256(token.encode()).hexdigest(),
            }
        )
        self.keys.credentials_provider.verify(signed, self.keys.audience)
        grant = PaymentCredentialGrant(
            id=grant_id,
            user_id=customer.id,
            checkout_id=checkout.id,
            payment_mandate_id=payment_mandate_record.id,
            payment_instrument_id=instrument.id,
            token_sha256=hashlib.sha256(token.encode()).hexdigest(),
            signed_credential=signed,
            credential_kind="razorpay_hosted_checkout_authorization",
            checkout_hash=challenge.checkout_hash,
            amount_minor=checkout.total_minor,
            currency=checkout.currency,
            expires_at=challenge.expires_at,
        )
        self.db.add(grant)
        self.db.flush()
        return grant

    def require_grant(self, checkout: Checkout, customer_id: uuid.UUID) -> PaymentCredentialGrant:
        grant = self.db.scalar(
            select(PaymentCredentialGrant)
            .where(
                PaymentCredentialGrant.checkout_id == checkout.id,
                PaymentCredentialGrant.user_id == customer_id,
            )
            .with_for_update()
        )
        if grant is None or grant.status not in {"issued", "presented"}:
            raise ConflictError(
                "credential_grant_required", "The Credentials Provider grant is missing."
            )
        if self._as_utc(grant.expires_at) <= utc_now():
            grant.status = "expired"
            raise ConflictError("credential_grant_expired", "The payment authorization expired.")
        if grant.amount_minor != checkout.total_minor or grant.currency != checkout.currency:
            raise ConflictError(
                "credential_scope_mismatch", "The payment authorization terms changed."
            )
        self.keys.credentials_provider.verify(grant.signed_credential, self.keys.audience)
        return grant

    def release_recurring_token(
        self,
        intent: ScheduledPurchaseIntent,
        run: ScheduledPurchaseRun,
        checkout: Checkout,
        customer: UserAccount,
        *,
        nonce: str,
    ) -> tuple[PaymentInstrument, str, str]:
        """Release a provider token only after independently verifying AP2 chains."""
        instrument = self.db.scalar(
            select(PaymentInstrument).where(
                PaymentInstrument.id == intent.payment_instrument_id,
                PaymentInstrument.user_id == customer.id,
                PaymentInstrument.status == "active",
            )
        )
        if instrument is None:
            raise NotFoundError("payment_instrument_not_found", "Payment method was not found.")
        provider_ready = recurring_instrument_ready(instrument, intent)
        if not provider_ready:
            raise ConflictError(
                "recurring_payment_authorization_required",
                "A confirmed Razorpay UPI Autopay mandate is required.",
            )
        if (
            intent.payment_token_reference != instrument.provider_token_reference
            or run.amount_minor != checkout.total_minor
            or run.currency.upper() != checkout.currency.upper()
            or intent.spent_minor + checkout.total_minor > intent.max_total_minor
        ):
            raise ConflictError(
                "credential_scope_mismatch",
                "The recurring credential does not match the authorized execution.",
            )
        if not (
            run.closed_checkout_mandate
            and run.closed_payment_mandate
            and run.merchant_checkout_jwt
            and intent.open_checkout_hash
        ):
            raise ConflictError("ap2_evidence_incomplete", "Autonomous AP2 evidence is incomplete.")
        try:
            verify_existing_closed_mandates(
                keys=self.keys,
                checkout_token=run.closed_checkout_mandate,
                payment_token=run.closed_payment_mandate,
                open_checkout_hash=intent.open_checkout_hash,
                merchant_checkout_jwt=run.merchant_checkout_jwt,
                audience=self.keys.audience,
                nonce=nonce,
                merchant_id=str(intent.merchant_id),
                payment_instrument_id=str(instrument.id),
                successful_occurrences=intent.successful_occurrences,
                spent_minor=intent.spent_minor,
                last_used_at=intent.last_executed_at,
                strict_constraints=intent.constraints,
            )
        except ValueError as error:
            raise ConflictError(
                "ap2_autonomous_verification_failed",
                "The autonomous AP2 mandate chain violated its signed bounds.",
            ) from error
        token = instrument.provider_token_reference
        customer_id = instrument.provider_customer_id
        if token is None or customer_id is None:
            raise ConflictError(
                "recurring_payment_authorization_required",
                "A confirmed Razorpay UPI Autopay mandate is required.",
            )
        grant = self.keys.credentials_provider.sign(
            {
                "iss": self.keys.credentials_provider.issuer,
                "sub": str(customer.id),
                "aud": self.keys.audience,
                "jti": f"ap2-autonomous-cp-grant:{run.id}",
                "iat": int(utc_now().timestamp()),
                "exp": int(intent.expires_at.timestamp()),
                "scheduled_purchase_id": str(intent.id),
                "scheduled_run_id": str(run.id),
                "checkout_hash": run.merchant_checkout_hash,
                "payment_instrument_id": str(instrument.id),
                "amount_minor": checkout.total_minor,
                "currency": checkout.currency,
                "provider_token_sha256": hashlib.sha256(token.encode()).hexdigest(),
            }
        )
        self.keys.credentials_provider.verify(grant, self.keys.audience)
        evidence = dict(run.evidence or {})
        evidence["credentials_provider"] = {
            "grant_sha256": hashlib.sha256(grant.encode()).hexdigest(),
            "signed_grant": grant,
            "scope_sha256": hashlib.sha256(
                json.dumps(
                    {
                        "run_id": str(run.id),
                        "checkout_hash": run.merchant_checkout_hash,
                        "amount_minor": checkout.total_minor,
                        "currency": checkout.currency,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest(),
        }
        run.evidence = evidence
        return instrument, customer_id, token

    @staticmethod
    def grant_out(
        grant: PaymentCredentialGrant, instrument: PaymentInstrument
    ) -> PaymentCredentialGrantOut:
        return PaymentCredentialGrantOut(
            id=grant.id,
            credential_kind=grant.credential_kind,
            instrument_alias=instrument.alias,
            status=grant.status,
            expires_at=grant.expires_at,
        )

    @staticmethod
    def instrument_out(row: PaymentInstrument) -> PaymentInstrumentOut:
        metadata = row.instrument_metadata or {}
        return PaymentInstrumentOut(
            id=row.id,
            provider=row.provider,
            instrument_type=row.instrument_type,
            alias=row.alias,
            network=row.network,
            last4=row.last4,
            is_default=row.is_default,
            requires_provider_checkout=(
                row.instrument_type != "com.razorpay.upi.autopay"
                or not recurring_instrument_ready(row)
            ),
            recurring_ready=recurring_instrument_ready(row),
            recurring_status=(
                str(metadata.get("token_status"))
                if row.instrument_type == "com.razorpay.upi.autopay"
                else None
            ),
        )

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        return value if value.tzinfo else value.replace(tzinfo=UTC)
