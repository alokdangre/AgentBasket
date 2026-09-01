from __future__ import annotations

import hashlib
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
    UserAccount,
)
from app.protocols.ap2.crypto import AP2KeySet, get_ap2_key_set
from app.protocols.ap2.models import PaymentCredentialGrantOut
from app.schemas.trusted_surface import PaymentInstrumentListOut, PaymentInstrumentOut


def utc_now() -> datetime:
    return datetime.now(UTC)


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
            if not rows:
                settings = get_settings()
                test_mode = settings.app_env.casefold() != "production" or (
                    settings.razorpay_key_id or ""
                ).startswith("rzp_test_")
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
                rows = [instrument]
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
        return PaymentInstrumentOut(
            id=row.id,
            provider=row.provider,
            instrument_type=row.instrument_type,
            alias=row.alias,
            network=row.network,
            last4=row.last4,
            is_default=row.is_default,
            requires_provider_checkout=row.provider_token_reference is None,
        )

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        return value if value.tzinfo else value.replace(tzinfo=UTC)
