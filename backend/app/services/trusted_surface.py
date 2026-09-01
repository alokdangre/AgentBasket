from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session
from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url, options_to_json_dict
from webauthn.helpers.exceptions import InvalidAuthenticationResponse, InvalidRegistrationResponse
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from app.core.config import get_settings
from app.core.errors import ConflictError, DomainError, NotFoundError
from app.db.models import UserAccount, WebAuthnCeremony, WebAuthnCredential
from app.schemas.trusted_surface import (
    PasskeyListOut,
    PasskeyOut,
    PasskeyRegistrationOptionsOut,
    PasskeyRegistrationVerify,
)


def utc_now() -> datetime:
    return datetime.now(UTC)


class TrustedSurfaceService:
    """Deterministic WebAuthn boundary; it never calls the shopping agent or an LLM."""

    def __init__(self, db: Session) -> None:
        self.db = db
        self.settings = get_settings()

    def list_passkeys(self, user: UserAccount) -> PasskeyListOut:
        rows = list(
            self.db.scalars(
                select(WebAuthnCredential)
                .where(WebAuthnCredential.user_id == user.id)
                .order_by(WebAuthnCredential.created_at)
            )
        )
        self.db.rollback()
        return PasskeyListOut(passkeys=[self._passkey_out(row) for row in rows])

    def registration_options(self, user: UserAccount) -> PasskeyRegistrationOptionsOut:
        with self.db.begin():
            existing = list(
                self.db.scalars(
                    select(WebAuthnCredential).where(WebAuthnCredential.user_id == user.id)
                )
            )
            challenge = secrets.token_bytes(32)
            expires_at = utc_now() + timedelta(seconds=self.settings.webauthn_challenge_ttl_seconds)
            ceremony = WebAuthnCeremony(
                user_id=user.id,
                purpose="registration",
                challenge=bytes_to_base64url(challenge),
                expires_at=expires_at,
            )
            self.db.add(ceremony)
            self.db.flush()
            options = generate_registration_options(
                rp_id=self.settings.webauthn_rp_id,
                rp_name=self.settings.webauthn_rp_name,
                user_id=user.id.bytes,
                user_name=user.email,
                user_display_name=user.full_name,
                challenge=challenge,
                timeout=self.settings.webauthn_challenge_ttl_seconds * 1000,
                authenticator_selection=AuthenticatorSelectionCriteria(
                    resident_key=ResidentKeyRequirement.PREFERRED,
                    user_verification=UserVerificationRequirement.REQUIRED,
                ),
                exclude_credentials=[
                    PublicKeyCredentialDescriptor(id=base64url_to_bytes(row.credential_id))
                    for row in existing
                ],
            )
            return PasskeyRegistrationOptionsOut(
                ceremony_id=ceremony.id,
                public_key=options_to_json_dict(options),
                expires_at=expires_at,
            )

    def verify_registration(
        self, user: UserAccount, payload: PasskeyRegistrationVerify
    ) -> PasskeyOut:
        with self.db.begin():
            ceremony = self.db.scalar(
                select(WebAuthnCeremony)
                .where(
                    WebAuthnCeremony.id == payload.ceremony_id,
                    WebAuthnCeremony.user_id == user.id,
                    WebAuthnCeremony.purpose == "registration",
                )
                .with_for_update()
            )
            if ceremony is None:
                raise NotFoundError("passkey_ceremony_not_found", "Passkey setup was not found.")
            if ceremony.status != "pending" or self._is_expired(ceremony.expires_at):
                ceremony.status = "expired"
                ceremony.consumed_at = utc_now()
                raise ConflictError("passkey_ceremony_expired", "Passkey setup expired. Try again.")
            try:
                result = verify_registration_response(
                    credential=payload.credential,
                    expected_challenge=base64url_to_bytes(ceremony.challenge),
                    expected_rp_id=self.settings.webauthn_rp_id,
                    expected_origin=self.settings.webauthn_expected_origin,
                    require_user_verification=True,
                )
            except (InvalidRegistrationResponse, ValueError, KeyError, TypeError) as error:
                raise DomainError(
                    "passkey_registration_rejected", "Passkey proof could not be verified.", 400
                ) from error
            credential_id = bytes_to_base64url(result.credential_id)
            if self.db.scalar(
                select(WebAuthnCredential.id).where(
                    WebAuthnCredential.credential_id == credential_id
                )
            ):
                raise ConflictError("passkey_already_registered", "This passkey is already saved.")
            response = payload.credential.get("response") or {}
            row = WebAuthnCredential(
                user_id=user.id,
                credential_id=credential_id,
                public_key=result.credential_public_key,
                sign_count=result.sign_count,
                transports=list(response.get("transports") or []),
                device_type=result.credential_device_type.value,
                backed_up=result.credential_backed_up,
                label=payload.label.strip(),
            )
            self.db.add(row)
            ceremony.status = "verified"
            ceremony.consumed_at = utc_now()
            self.db.flush()
            return self._passkey_out(row)

    def authentication_options(
        self, user: UserAccount, challenge_value: str | None = None
    ) -> tuple[str, dict[str, Any]]:
        credentials = list(
            self.db.scalars(select(WebAuthnCredential).where(WebAuthnCredential.user_id == user.id))
        )
        if not credentials:
            raise ConflictError(
                "passkey_required",
                "Add a passkey in My account before authorizing an agent purchase.",
            )
        challenge = (
            base64url_to_bytes(challenge_value)
            if challenge_value is not None
            else secrets.token_bytes(32)
        )
        options = generate_authentication_options(
            rp_id=self.settings.webauthn_rp_id,
            challenge=challenge,
            timeout=self.settings.webauthn_challenge_ttl_seconds * 1000,
            allow_credentials=[
                PublicKeyCredentialDescriptor(id=base64url_to_bytes(row.credential_id))
                for row in credentials
            ],
            user_verification=UserVerificationRequirement.REQUIRED,
        )
        return bytes_to_base64url(challenge), options_to_json_dict(options)

    def verify_authentication(
        self,
        user: UserAccount,
        expected_challenge: str,
        credential_payload: dict[str, Any],
    ) -> WebAuthnCredential:
        credential_id = str(credential_payload.get("id") or "")
        row = self.db.scalar(
            select(WebAuthnCredential)
            .where(
                WebAuthnCredential.user_id == user.id,
                WebAuthnCredential.credential_id == credential_id,
            )
            .with_for_update()
        )
        if row is None:
            raise DomainError("passkey_not_recognized", "This passkey is not registered.", 400)
        try:
            result = verify_authentication_response(
                credential=credential_payload,
                expected_challenge=base64url_to_bytes(expected_challenge),
                expected_rp_id=self.settings.webauthn_rp_id,
                expected_origin=self.settings.webauthn_expected_origin,
                credential_public_key=row.public_key,
                credential_current_sign_count=row.sign_count,
                require_user_verification=True,
            )
        except (InvalidAuthenticationResponse, ValueError, KeyError, TypeError) as error:
            raise DomainError(
                "passkey_authentication_rejected",
                "Authorization was not signed by a registered passkey.",
                400,
            ) from error
        row.sign_count = result.new_sign_count
        row.last_used_at = utc_now()
        return row

    @staticmethod
    def _passkey_out(row: WebAuthnCredential) -> PasskeyOut:
        return PasskeyOut(
            id=row.id,
            label=row.label,
            device_type=row.device_type,
            backed_up=row.backed_up,
            created_at=row.created_at,
            last_used_at=row.last_used_at,
        )

    @staticmethod
    def _is_expired(value: datetime) -> bool:
        current = value if value.tzinfo else value.replace(tzinfo=UTC)
        return current <= utc_now()


def get_trusted_surface_service(db: Session) -> TrustedSurfaceService:
    return TrustedSurfaceService(db)
