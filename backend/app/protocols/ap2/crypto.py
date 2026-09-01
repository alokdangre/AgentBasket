from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass
from typing import Any

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from jwcrypto.jwk import JWK

from app.core.config import Settings, get_settings
from app.core.errors import DomainError


def digest_b64url(value: str) -> str:
    digest = hashlib.sha256(value.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def digest_hex(payload: str) -> str:
    return hashlib.sha256(payload.encode()).hexdigest()


@dataclass(frozen=True)
class AP2Signer:
    issuer: str
    key_id: str
    private_key: ec.EllipticCurvePrivateKey

    @classmethod
    def from_pem(cls, issuer: str, key_id: str, pem: str) -> AP2Signer:
        try:
            key = serialization.load_pem_private_key(pem.replace("\\n", "\n").encode(), None)
        except (TypeError, ValueError) as error:
            raise DomainError(
                "ap2_invalid_signing_key", "An AP2 signing key is invalid.", 503
            ) from error
        if not isinstance(key, ec.EllipticCurvePrivateKey) or not isinstance(
            key.curve, ec.SECP256R1
        ):
            raise DomainError(
                "ap2_invalid_signing_key",
                "AP2 signing keys must be ES256 P-256 private keys.",
                503,
            )
        return cls(issuer=issuer, key_id=key_id, private_key=key)

    @classmethod
    def generate(cls, issuer: str, key_id: str) -> AP2Signer:
        return cls(issuer, key_id, ec.generate_private_key(ec.SECP256R1()))

    @property
    def public_key_pem(self) -> str:
        return (
            self.private_key.public_key()
            .public_bytes(
                serialization.Encoding.PEM,
                serialization.PublicFormat.SubjectPublicKeyInfo,
            )
            .decode()
        )

    @property
    def private_jwk(self) -> JWK:
        key = JWK.from_pyca(self.private_key)
        key.update(kid=self.key_id)
        return key

    @property
    def public_jwk(self) -> JWK:
        key = JWK.from_pyca(self.private_key.public_key())
        key.update(kid=self.key_id)
        return key

    def sign(self, claims: dict[str, Any]) -> str:
        return jwt.encode(
            claims,
            self.private_key,
            algorithm="ES256",
            headers={"kid": self.key_id, "typ": "JWT"},
        )

    def verify(
        self, token: str, audience: str, *, require_expiration: bool = True
    ) -> dict[str, Any]:
        try:
            header = jwt.get_unverified_header(token)
            if header.get("alg") != "ES256" or header.get("kid") != self.key_id:
                raise jwt.InvalidTokenError("Unexpected key metadata")
            required = ["iss", "aud", "iat", "jti"]
            if require_expiration:
                required.append("exp")
            return jwt.decode(
                token,
                self.private_key.public_key(),
                algorithms=["ES256"],
                audience=audience,
                issuer=self.issuer,
                options={"require": required},
            )
        except jwt.PyJWTError as error:
            raise DomainError(
                "ap2_invalid_signature", "AP2 evidence could not be verified."
            ) from error


@dataclass(frozen=True)
class AP2KeySet:
    merchant: AP2Signer
    # Backward-compatible field name. This is the Agent Provider signing key;
    # the browser Trusted Surface only performs WebAuthn and never receives it.
    trusted_surface: AP2Signer
    credentials_provider: AP2Signer
    payment_processor: AP2Signer
    audience: str

    @classmethod
    def from_settings(cls, settings: Settings) -> AP2KeySet:
        agent_provider_key = (
            settings.ap2_agent_provider_private_key_pem
            or settings.ap2_trusted_surface_private_key_pem
        )
        values = (
            settings.ap2_merchant_private_key_pem,
            agent_provider_key,
            settings.ap2_credentials_provider_private_key_pem,
            settings.ap2_payment_processor_private_key_pem,
        )
        if any(value is None for value in values):
            raise DomainError(
                "ap2_not_configured",
                "AP2 test issuers are not configured; no payment session was created.",
                503,
            )
        merchant_pem, surface_pem, credentials_provider_pem, processor_pem = (
            value.get_secret_value() for value in values if value is not None
        )
        return cls(
            merchant=AP2Signer.from_pem(
                settings.ap2_merchant_issuer, settings.ap2_merchant_key_id, merchant_pem
            ),
            trusted_surface=AP2Signer.from_pem(
                settings.ap2_agent_provider_issuer or settings.ap2_trusted_surface_issuer,
                settings.ap2_agent_provider_key_id or settings.ap2_trusted_surface_key_id,
                surface_pem,
            ),
            credentials_provider=AP2Signer.from_pem(
                settings.ap2_credentials_provider_issuer,
                settings.ap2_credentials_provider_key_id,
                credentials_provider_pem,
            ),
            payment_processor=AP2Signer.from_pem(
                settings.ap2_payment_processor_issuer,
                settings.ap2_payment_processor_key_id,
                processor_pem,
            ),
            audience=settings.ap2_audience,
        )


def get_ap2_key_set() -> AP2KeySet:
    return AP2KeySet.from_settings(get_settings())
