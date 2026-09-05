from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.core.config import Settings
from app.core.errors import ConflictError, DomainError, NotFoundError
from app.db.models import (
    AuditEvent,
    InventoryItem,
    Merchant,
    ModifierGroup,
    Product,
    ProductVariant,
    UcpCheckoutSession,
    UserAccount,
)
from app.domain.enums import ProductStatus, UserRole
from app.protocols.ucp.models import (
    UCP_CHECKOUT,
    UCP_VERSION,
    UcpCheckoutClaimItem,
    UcpCheckoutClaimResponse,
    UcpCheckoutCreateRequest,
    UcpCheckoutItem,
    UcpCheckoutLine,
    UcpCheckoutLink,
    UcpCheckoutResponse,
    UcpCheckoutTotal,
    UcpEntity,
    UcpMessage,
    UcpMetadata,
)
from app.schemas.cart import CartItemCreateRequest
from app.schemas.checkout import CheckoutFromCartCreate, CheckoutOut
from app.services.cart import CartService
from app.services.checkout import CheckoutService


def utc_now() -> datetime:
    return datetime.now(UTC)


class UcpCheckoutService:
    """Deterministic UCP quote-to-trusted-surface handoff.

    The external agent can select exact variants, inspect current base prices,
    update or cancel the session, and hand the buyer to AgentBasket. Payment is
    deliberately not exposed as a UCP payment handler: AP2 approval and Razorpay
    stay on the signed-in, human-present merchant checkout surface.
    """

    def __init__(self, db: Session, settings: Settings) -> None:
        self.db = db
        self.settings = settings

    def create(
        self,
        payload: UcpCheckoutCreateRequest,
        *,
        agent_profile_url: str,
        idempotency_key: str,
    ) -> UcpCheckoutResponse:
        request_hash = self._request_hash(payload)
        with self.db.begin():
            merchant = self._merchant()
            existing = self.db.scalar(
                select(UcpCheckoutSession).where(
                    UcpCheckoutSession.merchant_id == merchant.id,
                    UcpCheckoutSession.agent_profile_url == agent_profile_url,
                    UcpCheckoutSession.idempotency_key == idempotency_key,
                )
            )
            if existing is not None:
                if existing.request_hash != request_hash:
                    raise ConflictError(
                        "idempotency_key_reused",
                        "Idempotency-Key was already used for a different UCP checkout.",
                    )
                return self._response(existing)

            recent_checkouts = self.db.scalar(
                select(func.count())
                .select_from(UcpCheckoutSession)
                .where(
                    UcpCheckoutSession.agent_profile_url == agent_profile_url,
                    UcpCheckoutSession.created_at >= utc_now() - timedelta(minutes=1),
                )
            )
            if int(recent_checkouts or 0) >= self.settings.ucp_agent_max_checkouts_per_minute:
                raise DomainError(
                    "ucp_agent_rate_limit_exceeded",
                    "This external agent has created too many checkout handoffs. Try again later.",
                    429,
                )

            snapshots, subtotal = self._resolve_lines(merchant, payload)
            session = UcpCheckoutSession(
                merchant_id=merchant.id,
                agent_profile_url=agent_profile_url,
                protocol_version=UCP_VERSION,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                status="requires_escalation",
                currency=merchant.currency,
                line_items=snapshots,
                subtotal_minor=subtotal,
                expires_at=utc_now() + timedelta(minutes=self.settings.checkout_ttl_minutes),
            )
            self.db.add(session)
            self.db.flush()
            self.db.add(
                AuditEvent(
                    merchant_id=merchant.id,
                    actor_type="external_agent",
                    actor_id=agent_profile_url,
                    event_type="ucp.checkout_handoff.created",
                    aggregate_type="ucp_checkout_session",
                    aggregate_id=str(session.id),
                    payload={
                        "subtotal_minor": subtotal,
                        "currency": merchant.currency,
                        "line_count": len(snapshots),
                        "payment_mode": "trusted_surface_handoff",
                    },
                )
            )
            return self._response(session)

    def get(self, session_id: uuid.UUID, *, agent_profile_url: str) -> UcpCheckoutResponse:
        return self._response(self._owned_session(session_id, agent_profile_url))

    def get_handoff(self, session_id: uuid.UUID) -> UcpCheckoutResponse:
        return self._response(self._session(session_id))

    def update(
        self,
        session_id: uuid.UUID,
        payload: UcpCheckoutCreateRequest,
        *,
        agent_profile_url: str,
    ) -> UcpCheckoutResponse:
        with self.db.begin():
            session = self._owned_session(session_id, agent_profile_url, lock=True)
            self._require_mutable(session)
            merchant = self.db.get(Merchant, session.merchant_id)
            if merchant is None:
                raise NotFoundError("merchant_not_found", "Merchant was not found.")
            snapshots, subtotal = self._resolve_lines(merchant, payload)
            session.line_items = snapshots
            session.subtotal_minor = subtotal
            session.request_hash = self._request_hash(payload)
            session.expires_at = utc_now() + timedelta(minutes=self.settings.checkout_ttl_minutes)
            self.db.add(
                AuditEvent(
                    merchant_id=merchant.id,
                    actor_type="external_agent",
                    actor_id=agent_profile_url,
                    event_type="ucp.checkout_handoff.updated",
                    aggregate_type="ucp_checkout_session",
                    aggregate_id=str(session.id),
                    payload={"subtotal_minor": subtotal, "line_count": len(snapshots)},
                )
            )
            self.db.flush()
            return self._response(session)

    def complete(self, session_id: uuid.UUID, *, agent_profile_url: str) -> UcpCheckoutResponse:
        session = self._owned_session(session_id, agent_profile_url)
        response = self._response(session)
        if response.status == "requires_escalation":
            response.messages.append(
                UcpMessage(
                    type="error",
                    code="buyer_handoff_required",
                    content=(
                        "This merchant does not accept agent-submitted payment credentials. "
                        "The buyer must review AP2 evidence and pay with Razorpay on the "
                        "trusted AgentBasket checkout surface."
                    ),
                    severity="requires_buyer_review",
                )
            )
        return response

    def cancel(self, session_id: uuid.UUID, *, agent_profile_url: str) -> UcpCheckoutResponse:
        with self.db.begin():
            session = self._owned_session(session_id, agent_profile_url, lock=True)
            if session.status == "canceled":
                return self._response(session)
            if session.claimed_at is not None:
                raise ConflictError(
                    "checkout_already_claimed",
                    "The buyer already continued this checkout on the merchant surface.",
                )
            session.status = "canceled"
            session.canceled_at = utc_now()
            self.db.add(
                AuditEvent(
                    merchant_id=session.merchant_id,
                    actor_type="external_agent",
                    actor_id=agent_profile_url,
                    event_type="ucp.checkout_handoff.canceled",
                    aggregate_type="ucp_checkout_session",
                    aggregate_id=str(session.id),
                    payload={},
                )
            )
            self.db.flush()
            return self._response(session)

    def claim(self, session_id: uuid.UUID, customer: UserAccount) -> UcpCheckoutClaimResponse:
        if customer.role is not UserRole.CUSTOMER:
            raise DomainError(
                "customer_account_required",
                "Use a customer account to continue an external-agent checkout.",
                403,
            )
        with self.db.begin():
            session = self._session(session_id, lock=True)
            self._require_mutable(session, allow_claimed_by=customer.id)
            if session.claimed_at is not None:
                if session.claimed_result is None:
                    raise ConflictError(
                        "checkout_claim_incomplete", "The checkout handoff could not be recovered."
                    )
                return UcpCheckoutClaimResponse.model_validate(session.claimed_result)

            ready: list[CartItemCreateRequest] = []
            configuration_required: list[UcpCheckoutClaimItem] = []
            for line in session.line_items:
                if line["requires_configuration"]:
                    configuration_required.append(
                        UcpCheckoutClaimItem(
                            variant_id=line["variant_id"],
                            product_name=line["product_name"],
                            product_slug=line["product_slug"],
                            shop_url=f"/shop?product={line['product_slug']}",
                        )
                    )
                else:
                    ready.append(
                        CartItemCreateRequest(
                            variant_id=uuid.UUID(line["variant_id"]),
                            quantity=line["quantity"],
                            modifier_option_ids=[],
                        )
                    )

            if ready:
                CartService(self.db).add_items_in_transaction(
                    customer,
                    "ember-and-leaf",
                    ready,
                )
            next_url = (
                configuration_required[0].shop_url
                if configuration_required and not ready
                else f"/cart?ucp_handoff={session.id}"
            )
            result = UcpCheckoutClaimResponse(
                session_id=str(session.id),
                status="claimed",
                imported_item_count=sum(item.quantity for item in ready),
                configuration_required=configuration_required,
                next_url=next_url,
            )
            session.status = "claimed"
            session.claimed_by_user_id = customer.id
            session.claimed_at = utc_now()
            session.claimed_result = result.model_dump(mode="json")
            self.db.add(
                AuditEvent(
                    merchant_id=session.merchant_id,
                    actor_type="customer",
                    actor_id=str(customer.id),
                    event_type="ucp.checkout_handoff.claimed",
                    aggregate_type="ucp_checkout_session",
                    aggregate_id=str(session.id),
                    payload={
                        "imported_item_count": result.imported_item_count,
                        "configuration_required_count": len(configuration_required),
                    },
                )
            )
            self.db.flush()
            return result

    def prepare_checkout(
        self,
        session_id: uuid.UUID,
        payload: CheckoutFromCartCreate,
        customer: UserAccount,
        *,
        idempotency_key: str,
    ) -> CheckoutOut:
        session = self._session(session_id)
        if self._is_expired(session.expires_at):
            raise ConflictError("checkout_expired", "The UCP checkout handoff has expired.")
        if session.claimed_at is None or session.claimed_by_user_id != customer.id:
            raise DomainError(
                "checkout_handoff_not_claimed",
                "Continue this UCP checkout with the signed-in customer before preparing payment.",
                403,
            )
        self.db.rollback()
        return CheckoutService(self.db).create_from_cart(
            payload,
            idempotency_key,
            customer,
            source="ucp_agent",
        )

    def _resolve_lines(
        self, merchant: Merchant, payload: UcpCheckoutCreateRequest
    ) -> tuple[list[dict[str, Any]], int]:
        snapshots: list[dict[str, Any]] = []
        subtotal = 0
        for requested in payload.line_items:
            variant_uuid = self._uuid_or_none(requested.item.id)
            identifier_filter = ProductVariant.sku == requested.item.id
            if variant_uuid is not None:
                identifier_filter = or_(
                    ProductVariant.id == variant_uuid,
                    identifier_filter,
                )
            variant = self.db.scalar(
                select(ProductVariant)
                .join(Product, ProductVariant.product_id == Product.id)
                .where(
                    ProductVariant.merchant_id == merchant.id,
                    Product.merchant_id == merchant.id,
                    ProductVariant.sellable.is_(True),
                    Product.status == ProductStatus.ACTIVE,
                    identifier_filter,
                )
                .options(
                    selectinload(ProductVariant.product)
                    .selectinload(Product.modifier_groups)
                    .selectinload(ModifierGroup.options)
                )
            )
            if variant is None:
                raise NotFoundError(
                    "variant_not_found",
                    f"UCP checkout item is unavailable: {requested.item.id}",
                )
            if variant.track_inventory:
                available = sum(
                    row.on_hand_quantity - row.reserved_quantity
                    for row in self.db.scalars(
                        select(InventoryItem).where(InventoryItem.variant_id == variant.id)
                    )
                )
                if available < requested.quantity:
                    raise ConflictError(
                        "insufficient_inventory",
                        f"Requested quantity is unavailable for {variant.sku}.",
                    )

            unit_price = variant.price_minor
            line_total = unit_price * requested.quantity
            subtotal += line_total
            requires_configuration = any(
                group.required or group.minimum_selections > 0
                for group in variant.product.modifier_groups
                if any(option.active for option in group.options)
            )
            snapshots.append(
                {
                    "line_id": requested.id or str(uuid.uuid4()),
                    "variant_id": str(variant.id),
                    "sku": variant.sku,
                    "product_name": variant.product.name,
                    "product_slug": variant.product.slug,
                    "variant_name": variant.name,
                    "title": f"{variant.product.name} — {variant.name}",
                    "image_url": variant.product.image_urls[0]
                    if variant.product.image_urls
                    else None,
                    "unit_price_minor": unit_price,
                    "quantity": requested.quantity,
                    "line_total_minor": line_total,
                    "requires_configuration": requires_configuration,
                }
            )
        return snapshots, subtotal

    def _response(self, session: UcpCheckoutSession) -> UcpCheckoutResponse:
        expired = self._is_expired(session.expires_at)
        canceled = session.status == "canceled" or expired
        messages = [] if canceled else self._handoff_messages(session.line_items)
        if expired:
            messages.append(
                UcpMessage(
                    type="error",
                    code="checkout_expired",
                    content=(
                        "This checkout handoff expired. Create a new checkout for current prices."
                    ),
                    severity="unrecoverable",
                )
            )
        totals = [
            UcpCheckoutTotal(type="subtotal", amount=session.subtotal_minor),
            UcpCheckoutTotal(type="total", amount=session.subtotal_minor),
        ]
        return UcpCheckoutResponse(
            ucp=UcpMetadata(
                status="success",
                capabilities={UCP_CHECKOUT: [UcpEntity()]},
                payment_handlers={},
            ),
            id=str(session.id),
            status="canceled" if canceled else "requires_escalation",
            currency=session.currency,
            line_items=[
                UcpCheckoutLine(
                    id=line["line_id"],
                    item=UcpCheckoutItem(
                        id=line["variant_id"],
                        title=line["title"],
                        price=line["unit_price_minor"],
                        image_url=line.get("image_url"),
                    ),
                    quantity=line["quantity"],
                    totals=[
                        UcpCheckoutTotal(type="subtotal", amount=line["line_total_minor"]),
                        UcpCheckoutTotal(type="total", amount=line["line_total_minor"]),
                    ],
                )
                for line in session.line_items
            ],
            totals=totals,
            links=self._links(),
            messages=messages,
            expires_at=self._iso(session.expires_at),
            continue_url=(
                None
                if canceled
                else (
                    f"{self.settings.storefront_public_base_url.rstrip('/')}"
                    f"/checkout/handoff/{session.id}"
                )
            ),
        )

    def _handoff_messages(self, lines: list[dict[str, Any]]) -> list[UcpMessage]:
        messages = [
            UcpMessage(
                type="warning",
                code="buyer_review_required",
                content=(
                    "Continue on AgentBasket to choose fulfillment, review the authoritative "
                    "total, approve AP2 evidence, and pay through Razorpay."
                ),
                severity="requires_buyer_review",
            )
        ]
        messages.extend(
            UcpMessage(
                type="warning",
                code="item_configuration_required",
                content=f"{line['product_name']} requires buyer-selected options.",
                severity="requires_buyer_input",
                path=f"$.line_items[{index}]",
            )
            for index, line in enumerate(lines)
            if line["requires_configuration"]
        )
        return messages

    def _owned_session(
        self, session_id: uuid.UUID, agent_profile_url: str, *, lock: bool = False
    ) -> UcpCheckoutSession:
        statement = select(UcpCheckoutSession).where(
            UcpCheckoutSession.id == session_id,
            UcpCheckoutSession.agent_profile_url == agent_profile_url,
        )
        if lock:
            statement = statement.with_for_update()
        session = self.db.scalar(statement)
        if session is None:
            raise NotFoundError("ucp_checkout_not_found", "UCP checkout was not found.")
        return session

    def _session(self, session_id: uuid.UUID, *, lock: bool = False) -> UcpCheckoutSession:
        statement = select(UcpCheckoutSession).where(UcpCheckoutSession.id == session_id)
        if lock:
            statement = statement.with_for_update()
        session = self.db.scalar(statement)
        if session is None:
            raise NotFoundError("ucp_checkout_not_found", "UCP checkout was not found.")
        return session

    def _require_mutable(
        self,
        session: UcpCheckoutSession,
        *,
        allow_claimed_by: uuid.UUID | None = None,
    ) -> None:
        if self._is_expired(session.expires_at):
            raise ConflictError("checkout_expired", "The UCP checkout handoff has expired.")
        if session.status == "canceled":
            raise ConflictError("checkout_canceled", "The UCP checkout was canceled.")
        if session.claimed_at is not None and session.claimed_by_user_id != allow_claimed_by:
            raise ConflictError(
                "checkout_already_claimed",
                "The checkout was already continued on the merchant surface.",
            )

    def _merchant(self) -> Merchant:
        merchant = self.db.scalar(select(Merchant).where(Merchant.slug == "ember-and-leaf"))
        if merchant is None:
            raise NotFoundError("merchant_not_found", "Merchant was not found.")
        return merchant

    def _links(self) -> list[UcpCheckoutLink]:
        base = self.settings.storefront_public_base_url.rstrip("/")
        return [
            UcpCheckoutLink(
                type="terms_of_service",
                title="Terms of service",
                url=f"{base}/policies#terms",
            ),
            UcpCheckoutLink(
                type="privacy_policy",
                title="Privacy policy",
                url=f"{base}/policies#privacy",
            ),
            UcpCheckoutLink(
                type="refund_policy",
                title="Refund policy",
                url=f"{base}/policies#refunds",
            ),
        ]

    @staticmethod
    def _request_hash(payload: UcpCheckoutCreateRequest) -> str:
        canonical = json.dumps(
            payload.model_dump(mode="json", exclude_none=True),
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode()).hexdigest()

    @staticmethod
    def _uuid_or_none(value: str) -> uuid.UUID | None:
        try:
            return uuid.UUID(value)
        except ValueError:
            return None

    @staticmethod
    def _is_expired(value: datetime) -> bool:
        comparable = value if value.tzinfo else value.replace(tzinfo=UTC)
        return comparable <= utc_now()

    @staticmethod
    def _iso(value: datetime) -> str:
        comparable = value if value.tzinfo else value.replace(tzinfo=UTC)
        return comparable.isoformat().replace("+00:00", "Z")
