from __future__ import annotations

import hashlib
import json
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.core.config import get_settings
from app.core.errors import ConflictError, DomainError, NotFoundError
from app.db.models import (
    Ap2ConsentChallenge,
    Ap2Mandate,
    Ap2Receipt,
    Ap2TrustedIssuer,
    AuditEvent,
    Checkout,
    CheckoutApproval,
    CheckoutLineItem,
    InventoryItem,
    Order,
    Payment,
    UserAccount,
)
from app.db.models import (
    Merchant as MerchantRecord,
)
from app.domain.enums import CheckoutStatus, ReservationStatus
from app.protocols.ap2.crypto import AP2KeySet, digest_b64url, digest_hex, get_ap2_key_set
from app.protocols.ap2.models import (
    Amount,
    AP2ApprovalCreate,
    AP2ApprovalOut,
    AP2ChallengeOut,
    AP2EvidenceOut,
    AP2MandateOut,
    AP2ReceiptOut,
    CheckoutMandate,
    CheckoutReceiptSuccess,
    Merchant,
    PaymentInstrument,
    PaymentMandate,
    PaymentReceiptSuccess,
)
from app.schemas.checkout import CheckoutApprovalOut


def utc_now() -> datetime:
    return datetime.now(UTC)


class AP2Service:
    """Human-present AP2 v0.2 gate around agent-prepared checkouts."""

    def __init__(self, db: Session, keys: AP2KeySet | None = None) -> None:
        self.db = db
        self._keys = keys
        self.settings = get_settings()

    @property
    def keys(self) -> AP2KeySet:
        if self._keys is None:
            self._keys = get_ap2_key_set()
        return self._keys

    def create_challenge(
        self,
        checkout_id: uuid.UUID,
        idempotency_key: str,
        customer: UserAccount,
    ) -> AP2ChallengeOut:
        request_sha256 = self._digest(
            {"checkout_id": str(checkout_id), "customer_id": str(customer.id)}
        )
        with self.db.begin():
            checkout = self._checkout(checkout_id, customer.id, lock=True)
            if checkout is None:
                raise NotFoundError("checkout_not_found", "Checkout was not found.")
            existing = self.db.scalar(
                select(Ap2ConsentChallenge).where(
                    Ap2ConsentChallenge.customer_id == customer.id,
                    Ap2ConsentChallenge.idempotency_key == idempotency_key,
                )
            )
            if existing is not None:
                if existing.request_sha256 != request_sha256:
                    raise ConflictError(
                        "idempotency_key_reused",
                        "Idempotency-Key was used for a different AP2 challenge.",
                    )
                return self._challenge_out(existing)
            if checkout.source != "agent":
                raise ConflictError(
                    "ap2_agent_checkout_required",
                    "This AP2 flow is reserved for an agent-prepared checkout.",
                )
            if checkout.status != CheckoutStatus.READY_FOR_APPROVAL:
                raise ConflictError(
                    "checkout_not_approvable",
                    f"Checkout cannot be approved while {checkout.status.value}.",
                )
            if self._is_expired(checkout.expires_at):
                self._expire_checkout(checkout)
                raise ConflictError("checkout_expired", "The checkout quote has expired.")
            if self.db.scalar(
                select(CheckoutApproval.id).where(CheckoutApproval.checkout_id == checkout.id)
            ):
                raise ConflictError(
                    "approval_already_recorded", "This checkout already has approval evidence."
                )

            merchant = self.db.get(MerchantRecord, checkout.merchant_id)
            if merchant is None:
                raise NotFoundError("merchant_not_found", "Merchant was not found.")
            self._sync_trust_registry()
            issued_at = int(utc_now().timestamp())
            expires_at = min(
                self._as_utc(checkout.expires_at),
                utc_now() + timedelta(seconds=self.settings.ap2_challenge_ttl_seconds),
            )
            checkout_claims = self._checkout_claims(checkout, merchant, issued_at, expires_at)
            checkout_jwt = self.keys.merchant.sign(checkout_claims)
            checkout_hash = digest_b64url(checkout_jwt)
            nonce = secrets.token_urlsafe(32)
            display = self._display(checkout, merchant, checkout_hash)
            challenge = Ap2ConsentChallenge(
                checkout_id=checkout.id,
                customer_id=customer.id,
                idempotency_key=idempotency_key,
                request_sha256=request_sha256,
                nonce=nonce,
                checkout_jwt=checkout_jwt,
                checkout_hash=checkout_hash,
                display_sha256=self._digest(display),
                display_payload=display,
                status="pending",
                expires_at=expires_at,
            )
            self.db.add(challenge)
            self.db.flush()
            self.db.add(
                AuditEvent(
                    merchant_id=checkout.merchant_id,
                    actor_type="trusted_surface",
                    actor_id=str(customer.id),
                    event_type="ap2.challenge.created",
                    aggregate_type="checkout",
                    aggregate_id=str(checkout.id),
                    payload={
                        "challenge_id": str(challenge.id),
                        "checkout_hash": checkout_hash,
                        "display_sha256": challenge.display_sha256,
                        "expires_at": expires_at.isoformat(),
                    },
                )
            )
            return self._challenge_out(challenge)

    def approve(
        self,
        checkout_id: uuid.UUID,
        payload: AP2ApprovalCreate,
        idempotency_key: str,
        customer: UserAccount,
    ) -> AP2ApprovalOut:
        request_sha256 = self._digest(payload.model_dump(mode="json"))
        failure: ConflictError | None = None
        output: AP2ApprovalOut | None = None
        with self.db.begin():
            checkout = self._checkout(checkout_id, customer.id, lock=True)
            challenge = self.db.scalar(
                select(Ap2ConsentChallenge)
                .where(
                    Ap2ConsentChallenge.id == payload.challenge_id,
                    Ap2ConsentChallenge.checkout_id == checkout_id,
                    Ap2ConsentChallenge.customer_id == customer.id,
                )
                .with_for_update()
            )
            if checkout is None or challenge is None:
                raise NotFoundError("ap2_challenge_not_found", "AP2 challenge was not found.")
            if challenge.status == "accepted":
                if (
                    challenge.approval_idempotency_key != idempotency_key
                    or challenge.approval_request_sha256 != request_sha256
                ):
                    raise ConflictError(
                        "ap2_challenge_consumed", "This AP2 challenge was already consumed."
                    )
                return self._approval_out(challenge)
            if challenge.status != "pending":
                raise ConflictError(
                    "ap2_challenge_consumed", "This AP2 challenge cannot be used again."
                )

            failure = self._approval_failure(checkout, challenge, payload)
            if failure is not None:
                challenge.status = "rejected"
                challenge.consumed_at = utc_now()
                challenge.approval_idempotency_key = idempotency_key
                challenge.approval_request_sha256 = request_sha256
                self.db.add(
                    AuditEvent(
                        merchant_id=checkout.merchant_id,
                        actor_type="trusted_surface",
                        actor_id=str(customer.id),
                        event_type="ap2.approval.rejected",
                        aggregate_type="checkout",
                        aggregate_id=str(checkout.id),
                        payload={"challenge_id": str(challenge.id), "error_code": failure.code},
                    )
                )
            else:
                now = utc_now()
                issued_at = int(now.timestamp())
                expires_at = int(self._as_utc(challenge.expires_at).timestamp())
                subject = str(customer.id)
                checkout_mandate = self._checkout_mandate(challenge, issued_at)
                payment_mandate = self._payment_mandate(challenge, issued_at)
                checkout_token = self.keys.trusted_surface.sign(
                    {
                        "iss": self.keys.trusted_surface.issuer,
                        "sub": subject,
                        "aud": self.keys.audience,
                        "jti": f"ap2-checkout:{challenge.id}",
                        "nonce": challenge.nonce,
                        **checkout_mandate.model_dump(exclude_none=True),
                    }
                )
                payment_token = self.keys.trusted_surface.sign(
                    {
                        "iss": self.keys.trusted_surface.issuer,
                        "sub": subject,
                        "aud": self.keys.audience,
                        "jti": f"ap2-payment:{challenge.id}",
                        "nonce": challenge.nonce,
                        **payment_mandate.model_dump(exclude_none=True),
                    }
                )
                checkout_claims = self.keys.trusted_surface.verify(
                    checkout_token, self.keys.audience
                )
                payment_claims = self.keys.trusted_surface.verify(payment_token, self.keys.audience)
                self._verify_closed_mandates(
                    challenge,
                    checkout,
                    checkout_claims,
                    payment_claims,
                    issued_at,
                    expires_at,
                )
                approval = CheckoutApproval(
                    checkout_id=checkout.id,
                    customer_id=customer.id,
                    quote_version=checkout.quote_version,
                    approved_total_minor=checkout.total_minor,
                    currency=checkout.currency,
                    evidence_sha256=self._approval_evidence(checkout, customer.id),
                )
                self.db.add(approval)
                self.db.flush()
                mandates = [
                    self._mandate_record(
                        challenge, customer.id, "checkout", checkout_mandate.vct, checkout_token
                    ),
                    self._mandate_record(
                        challenge, customer.id, "payment", payment_mandate.vct, payment_token
                    ),
                ]
                self.db.add_all(mandates)
                self.db.flush()
                checkout.status = CheckoutStatus.APPROVED
                challenge.status = "accepted"
                challenge.consumed_at = now
                challenge.approval_id = approval.id
                challenge.approval_idempotency_key = idempotency_key
                challenge.approval_request_sha256 = request_sha256
                self.db.add_all(
                    [
                        AuditEvent(
                            merchant_id=checkout.merchant_id,
                            actor_type="customer",
                            actor_id=str(customer.id),
                            event_type="ap2.mandates.verified",
                            aggregate_type="checkout",
                            aggregate_id=str(checkout.id),
                            payload={
                                "challenge_id": str(challenge.id),
                                "checkout_hash": challenge.checkout_hash,
                                "mandate_ids": [str(item.id) for item in mandates],
                            },
                        ),
                        AuditEvent(
                            merchant_id=checkout.merchant_id,
                            actor_type="customer",
                            actor_id=str(customer.id),
                            event_type="checkout.approved",
                            aggregate_type="checkout",
                            aggregate_id=str(checkout.id),
                            payload={
                                "approval_mode": "ap2_human_present",
                                "total_minor": checkout.total_minor,
                                "currency": checkout.currency,
                                "quote_version": checkout.quote_version,
                                "evidence_sha256": approval.evidence_sha256,
                            },
                        ),
                    ]
                )
                self.db.flush()
                output = self._approval_out(challenge)
        if failure is not None:
            raise failure
        if output is None:
            raise RuntimeError("AP2 approval output was not created")
        return output

    def evidence(self, checkout_id: uuid.UUID, customer: UserAccount) -> AP2EvidenceOut:
        challenge = self.db.scalar(
            select(Ap2ConsentChallenge)
            .join(Checkout, Checkout.id == Ap2ConsentChallenge.checkout_id)
            .where(
                Ap2ConsentChallenge.checkout_id == checkout_id,
                Ap2ConsentChallenge.customer_id == customer.id,
                Checkout.customer_id == customer.id,
                Ap2ConsentChallenge.status == "accepted",
            )
            .order_by(Ap2ConsentChallenge.created_at.desc())
        )
        if challenge is None:
            raise NotFoundError("ap2_evidence_not_found", "AP2 evidence was not found.")
        mandates = list(
            self.db.scalars(
                select(Ap2Mandate)
                .where(Ap2Mandate.challenge_id == challenge.id)
                .order_by(Ap2Mandate.created_at)
            )
        )
        receipts = list(
            self.db.scalars(
                select(Ap2Receipt)
                .where(Ap2Receipt.checkout_id == checkout_id)
                .order_by(Ap2Receipt.created_at)
            )
        )
        output = AP2EvidenceOut(
            checkout_id=checkout_id,
            challenge_id=challenge.id,
            checkout_hash=challenge.checkout_hash,
            status=challenge.status,
            mandates=[self._mandate_out(item) for item in mandates],
            receipts=[self._receipt_out(item) for item in receipts],
        )
        self.db.rollback()
        return output

    def record_success_receipts(
        self,
        checkout: Checkout,
        order: Order,
        payment: Payment,
        network_confirmation_id: str | None,
    ) -> None:
        challenge = self.db.scalar(
            select(Ap2ConsentChallenge).where(
                Ap2ConsentChallenge.checkout_id == checkout.id,
                Ap2ConsentChallenge.approval_id == payment.approval_id,
                Ap2ConsentChallenge.status == "accepted",
            )
        )
        if challenge is None:
            return
        existing = list(
            self.db.scalars(select(Ap2Receipt).where(Ap2Receipt.checkout_id == checkout.id))
        )
        if existing:
            return
        mandates = {
            item.mandate_type: item
            for item in self.db.scalars(
                select(Ap2Mandate).where(Ap2Mandate.challenge_id == challenge.id)
            )
        }
        if set(mandates) != {"checkout", "payment"} or payment.provider_payment_id is None:
            raise ConflictError(
                "ap2_evidence_incomplete", "AP2 receipts cannot be issued without both mandates."
            )
        now = int(utc_now().timestamp())
        checkout_receipt = CheckoutReceiptSuccess(
            iss=self.keys.merchant.issuer,
            iat=now,
            reference=digest_b64url(mandates["checkout"].signed_jwt),
            order_id=str(order.id),
        )
        if network_confirmation_id is None and self.settings.app_env == "production":
            raise ConflictError(
                "ap2_network_confirmation_missing",
                "The payment processor did not provide a network confirmation identifier.",
            )
        payment_receipt = PaymentReceiptSuccess(
            iss=self.keys.payment_processor.issuer,
            iat=now,
            reference=digest_b64url(mandates["payment"].signed_jwt),
            payment_id=str(payment.id),
            psp_confirmation_id=payment.provider_payment_id,
            network_confirmation_id=network_confirmation_id
            or f"razorpay-test:{payment.provider_payment_id}",
        )
        records = [
            self._receipt_record(
                checkout,
                order,
                payment,
                "checkout",
                checkout_receipt.model_dump(exclude_none=True),
                self.keys.merchant,
            ),
            self._receipt_record(
                checkout,
                order,
                payment,
                "payment",
                payment_receipt.model_dump(exclude_none=True),
                self.keys.payment_processor,
            ),
        ]
        self.db.add_all(records)
        self.db.flush()
        self.db.add(
            AuditEvent(
                merchant_id=checkout.merchant_id,
                actor_type="commerce_core",
                actor_id=None,
                event_type="ap2.receipts.issued",
                aggregate_type="checkout",
                aggregate_id=str(checkout.id),
                payload={
                    "challenge_id": str(challenge.id),
                    "receipt_ids": [str(item.id) for item in records],
                },
            )
        )

    def require_verified_mandates(self, checkout: Checkout, approval: CheckoutApproval) -> None:
        if checkout.source != "agent":
            return
        challenge = self.db.scalar(
            select(Ap2ConsentChallenge).where(
                Ap2ConsentChallenge.checkout_id == checkout.id,
                Ap2ConsentChallenge.approval_id == approval.id,
                Ap2ConsentChallenge.status == "accepted",
            )
        )
        if challenge is None:
            raise ConflictError(
                "ap2_approval_required", "Verify both AP2 mandates before starting payment."
            )
        mandate_types = {
            item.mandate_type
            for item in self.db.scalars(
                select(Ap2Mandate).where(
                    Ap2Mandate.challenge_id == challenge.id,
                    Ap2Mandate.verification_status == "verified",
                )
            )
        }
        if mandate_types != {"checkout", "payment"}:
            raise ConflictError(
                "ap2_approval_required", "Verify both AP2 mandates before starting payment."
            )

    def _approval_failure(
        self,
        checkout: Checkout,
        challenge: Ap2ConsentChallenge,
        payload: AP2ApprovalCreate,
    ) -> ConflictError | None:
        if checkout.status != CheckoutStatus.READY_FOR_APPROVAL:
            return ConflictError(
                "checkout_not_approvable",
                f"Checkout cannot be approved while {checkout.status.value}.",
            )
        if self._is_expired(checkout.expires_at) or self._is_expired(challenge.expires_at):
            self._expire_checkout(checkout)
            return ConflictError("ap2_challenge_expired", "The AP2 approval challenge expired.")
        expected = (
            payload.nonce == challenge.nonce
            and payload.checkout_hash == challenge.checkout_hash
            and payload.display_sha256 == challenge.display_sha256
            and payload.expected_total_minor == checkout.total_minor
            and payload.currency.upper() == checkout.currency.upper()
            and payload.quote_version == checkout.quote_version
        )
        if not expected:
            return ConflictError(
                "ap2_terms_mismatch",
                "Displayed checkout terms do not match the authoritative quote.",
            )
        return None

    def _verify_closed_mandates(
        self,
        challenge: Ap2ConsentChallenge,
        checkout: Checkout,
        checkout_claims: dict[str, Any],
        payment_claims: dict[str, Any],
        issued_at: int,
        expires_at: int,
    ) -> None:
        merchant_claims = self.keys.merchant.verify(challenge.checkout_jwt, self.keys.audience)
        merchant_checkout = merchant_claims.get("checkout", {})
        if (
            merchant_checkout.get("id") != str(checkout.id)
            or merchant_checkout.get("quote_version") != checkout.quote_version
            or merchant_checkout.get("total_minor") != checkout.total_minor
            or merchant_checkout.get("currency") != checkout.currency
            or digest_b64url(challenge.checkout_jwt) != challenge.checkout_hash
        ):
            raise ConflictError("ap2_checkout_mismatch", "Merchant checkout evidence changed.")
        checkout_mandate = CheckoutMandate.model_validate(
            {key: checkout_claims.get(key) for key in CheckoutMandate.model_fields}
        )
        payment_mandate = PaymentMandate.model_validate(
            {key: payment_claims.get(key) for key in PaymentMandate.model_fields}
        )
        if (
            checkout_claims.get("nonce") != challenge.nonce
            or payment_claims.get("nonce") != challenge.nonce
            or checkout_mandate.checkout_hash != challenge.checkout_hash
            or checkout_mandate.checkout_jwt != challenge.checkout_jwt
            or payment_mandate.transaction_id != challenge.checkout_hash
            or payment_mandate.payment_amount.amount != checkout.total_minor
            or payment_mandate.payment_amount.currency != checkout.currency
            or checkout_mandate.iat != issued_at
            or payment_mandate.iat != issued_at
            or checkout_mandate.exp != expires_at
            or payment_mandate.exp != expires_at
        ):
            raise ConflictError("ap2_mandate_mismatch", "AP2 mandate terms do not match checkout.")

    def _checkout_mandate(
        self, challenge: Ap2ConsentChallenge, issued_at: int | None = None
    ) -> CheckoutMandate:
        return CheckoutMandate(
            checkout_jwt=challenge.checkout_jwt,
            checkout_hash=challenge.checkout_hash,
            iat=issued_at or int(challenge.created_at.timestamp()),
            exp=int(self._as_utc(challenge.expires_at).timestamp()),
        )

    def _payment_mandate(
        self, challenge: Ap2ConsentChallenge, issued_at: int | None = None
    ) -> PaymentMandate:
        display = challenge.display_payload
        return PaymentMandate(
            transaction_id=challenge.checkout_hash,
            payee=Merchant.model_validate(display["merchant"]),
            payment_amount=Amount(amount=display["total_minor"], currency=display["currency"]),
            payment_instrument=PaymentInstrument.model_validate(display["payment_instrument"]),
            risk_data={"channel": "merchant_in_app", "human_present": True},
            iat=issued_at or int(challenge.created_at.timestamp()),
            exp=int(self._as_utc(challenge.expires_at).timestamp()),
        )

    def _challenge_out(self, challenge: Ap2ConsentChallenge) -> AP2ChallengeOut:
        return AP2ChallengeOut(
            id=challenge.id,
            checkout_id=challenge.checkout_id,
            nonce=challenge.nonce,
            checkout_hash=challenge.checkout_hash,
            display_sha256=challenge.display_sha256,
            expires_at=challenge.expires_at,
            display=challenge.display_payload,
            checkout_mandate=self._checkout_mandate(challenge),
            payment_mandate=self._payment_mandate(challenge),
        )

    def _approval_out(self, challenge: Ap2ConsentChallenge) -> AP2ApprovalOut:
        approval = self.db.get(CheckoutApproval, challenge.approval_id)
        if approval is None:
            raise DomainError("ap2_approval_missing", "AP2 approval evidence is incomplete.", 500)
        mandates = list(
            self.db.scalars(
                select(Ap2Mandate)
                .where(Ap2Mandate.challenge_id == challenge.id)
                .order_by(Ap2Mandate.created_at)
            )
        )
        return AP2ApprovalOut(
            challenge_id=challenge.id,
            checkout_hash=challenge.checkout_hash,
            approval=CheckoutApprovalOut(
                id=approval.id,
                checkout_id=approval.checkout_id,
                quote_version=approval.quote_version,
                approved_total_minor=approval.approved_total_minor,
                currency=approval.currency,
                evidence_sha256=approval.evidence_sha256,
                approved_at=approval.approved_at,
            ),
            mandates=[self._mandate_out(item) for item in mandates],
        )

    def _mandate_record(
        self,
        challenge: Ap2ConsentChallenge,
        customer_id: uuid.UUID,
        mandate_type: str,
        vct: str,
        token: str,
    ) -> Ap2Mandate:
        return Ap2Mandate(
            challenge_id=challenge.id,
            checkout_id=challenge.checkout_id,
            customer_id=customer_id,
            mandate_type=mandate_type,
            vct=vct,
            issuer=self.keys.trusted_surface.issuer,
            key_id=self.keys.trusted_surface.key_id,
            signed_jwt=token,
            payload_sha256=digest_hex(token),
            checkout_hash=challenge.checkout_hash,
            verification_status="verified",
            verified_at=utc_now(),
        )

    def _receipt_record(
        self,
        checkout: Checkout,
        order: Order,
        payment: Payment,
        receipt_type: str,
        claims: dict[str, Any],
        signer,
    ) -> Ap2Receipt:
        token = signer.sign(
            {
                "aud": self.keys.audience,
                "jti": f"ap2-{receipt_type}-receipt:{checkout.id}",
                **claims,
            }
        )
        signer.verify(token, self.keys.audience, require_expiration=False)
        return Ap2Receipt(
            checkout_id=checkout.id,
            order_id=order.id,
            payment_id=payment.id,
            receipt_type=receipt_type,
            status="Success",
            issuer=signer.issuer,
            key_id=signer.key_id,
            reference=claims["reference"],
            signed_jwt=token,
            payload_sha256=digest_hex(token),
        )

    def _sync_trust_registry(self) -> None:
        for role, signer in (
            ("merchant", self.keys.merchant),
            ("trusted_surface", self.keys.trusted_surface),
            ("payment_processor", self.keys.payment_processor),
        ):
            existing = self.db.scalar(
                select(Ap2TrustedIssuer).where(
                    Ap2TrustedIssuer.issuer == signer.issuer,
                    Ap2TrustedIssuer.key_id == signer.key_id,
                )
            )
            if existing is None:
                self.db.add(
                    Ap2TrustedIssuer(
                        issuer=signer.issuer,
                        key_id=signer.key_id,
                        role=role,
                        public_key_pem=signer.public_key_pem,
                    )
                )
            elif not existing.active or existing.public_key_pem != signer.public_key_pem:
                raise DomainError(
                    "ap2_untrusted_issuer", "The configured AP2 issuer is not trusted.", 503
                )

    def _checkout(
        self, checkout_id: uuid.UUID, customer_id: uuid.UUID, *, lock: bool
    ) -> Checkout | None:
        statement = (
            select(Checkout)
            .where(Checkout.id == checkout_id, Checkout.customer_id == customer_id)
            .options(
                selectinload(Checkout.lines).selectinload(CheckoutLineItem.modifiers),
                selectinload(Checkout.lines).selectinload(CheckoutLineItem.reservations),
                selectinload(Checkout.fulfillment_options),
            )
        )
        if lock:
            statement = statement.with_for_update()
        return self.db.scalar(statement)

    def _checkout_claims(
        self,
        checkout: Checkout,
        merchant: MerchantRecord,
        issued_at: int,
        expires_at: datetime,
    ) -> dict[str, Any]:
        return {
            "iss": self.keys.merchant.issuer,
            "sub": str(checkout.id),
            "aud": self.keys.audience,
            "jti": f"merchant-checkout:{checkout.id}:{checkout.quote_version}",
            "iat": issued_at,
            "exp": int(expires_at.timestamp()),
            "checkout": {
                "id": str(checkout.id),
                "merchant_id": str(merchant.id),
                "customer_id": str(checkout.customer_id),
                "quote_version": checkout.quote_version,
                "currency": checkout.currency,
                "total_minor": checkout.total_minor,
                "request_hash": checkout.request_hash,
                "fulfillment_type": checkout.fulfillment_type.value,
                "lines": [
                    {
                        "variant_id": str(line.variant_id),
                        "quantity": line.quantity,
                        "line_total_minor": line.line_total_minor,
                        "product": line.product_snapshot,
                    }
                    for line in checkout.lines
                ],
            },
        }

    @staticmethod
    def _display(
        checkout: Checkout, merchant: MerchantRecord, checkout_hash: str
    ) -> dict[str, Any]:
        option = next((item for item in checkout.fulfillment_options if item.selected), None)
        return {
            "checkout_id": str(checkout.id),
            "quote_version": checkout.quote_version,
            "merchant": {"id": str(merchant.id), "name": merchant.name, "website": None},
            "lines": [
                {
                    "id": str(line.id),
                    "product_name": line.product_snapshot["product_name"],
                    "variant_name": line.product_snapshot["variant_name"],
                    "quantity": line.quantity,
                    "line_total_minor": line.line_total_minor,
                }
                for line in checkout.lines
            ],
            "fulfillment": {
                "type": checkout.fulfillment_type.value,
                "title": option.title if option else checkout.fulfillment_type.value,
                "postal_code": checkout.postal_code,
                "eta_min_minutes": option.eta_min_minutes if option else None,
                "eta_max_minutes": option.eta_max_minutes if option else None,
            },
            "subtotal_minor": checkout.subtotal_minor,
            "delivery_minor": checkout.delivery_minor,
            "discount_minor": checkout.discount_minor,
            "tax_minor": checkout.tax_minor,
            "total_minor": checkout.total_minor,
            "currency": checkout.currency,
            "checkout_hash": checkout_hash,
            "payment_instrument": {
                "id": "razorpay-standard-checkout",
                "type": "com.razorpay.standard",
                "description": "Payment instrument selected in Razorpay Standard Checkout",
            },
            "expires_at": checkout.expires_at.isoformat(),
        }

    @staticmethod
    def _mandate_out(mandate: Ap2Mandate) -> AP2MandateOut:
        return AP2MandateOut(
            id=mandate.id,
            mandate_type=mandate.mandate_type,
            vct=mandate.vct,
            issuer=mandate.issuer,
            key_id=mandate.key_id,
            checkout_hash=mandate.checkout_hash,
            verification_status=mandate.verification_status,
            signed_jwt=mandate.signed_jwt,
        )

    @staticmethod
    def _receipt_out(receipt: Ap2Receipt) -> AP2ReceiptOut:
        return AP2ReceiptOut(
            id=receipt.id,
            receipt_type=receipt.receipt_type,
            status=receipt.status,
            issuer=receipt.issuer,
            key_id=receipt.key_id,
            reference=receipt.reference,
            signed_jwt=receipt.signed_jwt,
            created_at=receipt.created_at,
        )

    @staticmethod
    def _approval_evidence(checkout: Checkout, customer_id: uuid.UUID) -> str:
        return AP2Service._digest(
            {
                "checkout_id": str(checkout.id),
                "customer_id": str(customer_id),
                "currency": checkout.currency,
                "quote_version": checkout.quote_version,
                "request_hash": checkout.request_hash,
                "total_minor": checkout.total_minor,
            }
        )

    @staticmethod
    def _digest(payload: dict[str, Any]) -> str:
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()

    def _expire_checkout(self, checkout: Checkout) -> None:
        for line in checkout.lines:
            for reservation in line.reservations:
                if reservation.status != ReservationStatus.ACTIVE:
                    continue
                inventory = self.db.get(InventoryItem, reservation.inventory_item_id)
                if inventory is not None:
                    inventory.reserved_quantity = max(
                        0, inventory.reserved_quantity - reservation.quantity
                    )
                reservation.status = ReservationStatus.EXPIRED
        checkout.status = CheckoutStatus.EXPIRED

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        return value if value.tzinfo else value.replace(tzinfo=UTC)

    @classmethod
    def _is_expired(cls, value: datetime) -> bool:
        return cls._as_utc(value) <= utc_now()
