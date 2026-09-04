import hashlib
import hmac
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from app.core.config import Settings, get_settings
from app.core.errors import DomainError


@dataclass(frozen=True)
class RazorpayOrder:
    id: str
    amount: int
    currency: str
    receipt: str
    status: str
    amount_paid: int = 0


@dataclass(frozen=True)
class RazorpayPayment:
    id: str
    order_id: str
    amount: int
    currency: str
    status: str
    captured: bool
    network_confirmation_id: str | None = None
    token_id: str | None = None


@dataclass(frozen=True)
class RazorpayCustomer:
    id: str
    name: str
    email: str
    contact: str


@dataclass(frozen=True)
class RazorpayRecurringNotification:
    id: str
    token_id: str
    payment_after: int
    status: str | None = None
    delivered_at: int | None = None


@dataclass(frozen=True)
class RazorpayRecurringOrder:
    order: RazorpayOrder
    notification: RazorpayRecurringNotification


@dataclass(frozen=True)
class RazorpayRecurringDebit:
    payment_id: str
    order_id: str
    signature: str | None = None


class RazorpayGateway(Protocol):
    @property
    def key_id(self) -> str: ...

    def create_order(
        self, *, amount: int, currency: str, receipt: str, notes: dict[str, str]
    ) -> RazorpayOrder: ...

    def find_order_by_receipt(self, receipt: str) -> RazorpayOrder | None: ...

    def fetch_order(self, order_id: str) -> RazorpayOrder: ...

    def fetch_payment(self, payment_id: str) -> RazorpayPayment: ...

    def verify_checkout_signature(
        self, *, order_id: str, payment_id: str, signature: str
    ) -> bool: ...


class RazorpayRecurringGateway(Protocol):
    @property
    def key_id(self) -> str: ...

    def create_customer(
        self,
        *,
        name: str,
        email: str,
        contact: str,
        notes: dict[str, str],
    ) -> RazorpayCustomer: ...

    def create_mandate_order(
        self,
        *,
        amount: int,
        currency: str,
        receipt: str,
        customer_id: str,
        max_amount: int,
        frequency: str,
        expire_at: int,
        notes: dict[str, str],
    ) -> RazorpayOrder: ...

    def find_order_by_receipt(self, receipt: str) -> RazorpayOrder | None: ...

    def fetch_payment(self, payment_id: str) -> RazorpayPayment: ...

    def verify_checkout_signature(
        self, *, order_id: str, payment_id: str, signature: str
    ) -> bool: ...

    def create_recurring_order(
        self,
        *,
        amount: int,
        currency: str,
        receipt: str,
        token_id: str,
        payment_after: int,
        notes: dict[str, str],
    ) -> RazorpayRecurringOrder: ...

    def create_recurring_payment(
        self,
        *,
        email: str,
        contact: str,
        amount: int,
        currency: str,
        order_id: str,
        customer_id: str,
        token_id: str,
        description: str,
        notes: dict[str, str],
    ) -> RazorpayRecurringDebit: ...


class RazorpayHttpGateway:
    def __init__(self, settings: Settings) -> None:
        if not settings.razorpay_key_id or not settings.razorpay_key_secret:
            raise DomainError(
                "razorpay_not_configured",
                "Razorpay test-mode credentials are not configured.",
                503,
            )
        self._key_id = settings.razorpay_key_id
        self._key_secret = settings.razorpay_key_secret
        self._base_url = settings.razorpay_api_url.rstrip("/")
        self._timeout = settings.razorpay_timeout_seconds

    @property
    def key_id(self) -> str:
        return self._key_id

    def create_order(
        self, *, amount: int, currency: str, receipt: str, notes: dict[str, str]
    ) -> RazorpayOrder:
        payload = self._request(
            "POST",
            "/orders",
            json={
                "amount": amount,
                "currency": currency,
                "receipt": receipt,
                "notes": notes,
                "partial_payment": False,
            },
        )
        return self._order(payload)

    def find_order_by_receipt(self, receipt: str) -> RazorpayOrder | None:
        payload = self._request("GET", "/orders", params={"receipt": receipt, "count": 10})
        for item in payload.get("items", []):
            if isinstance(item, dict) and item.get("receipt") == receipt:
                return self._order(item)
        return None

    def fetch_order(self, order_id: str) -> RazorpayOrder:
        return self._order(self._request("GET", f"/orders/{order_id}"))

    def fetch_payment(self, payment_id: str) -> RazorpayPayment:
        payload = self._request("GET", f"/payments/{payment_id}")
        try:
            acquirer_data = payload.get("acquirer_data") or {}
            return RazorpayPayment(
                id=str(payload["id"]),
                order_id=str(payload["order_id"]),
                amount=int(payload["amount"]),
                currency=str(payload["currency"]).upper(),
                status=str(payload["status"]),
                captured=bool(payload["captured"]),
                network_confirmation_id=next(
                    (
                        str(acquirer_data[key])
                        for key in ("rrn", "upi_transaction_id", "auth_code")
                        if acquirer_data.get(key)
                    ),
                    None,
                ),
                token_id=(str(payload["token_id"]) if payload.get("token_id") else None),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise DomainError(
                "razorpay_invalid_response", "Razorpay returned an invalid payment response.", 502
            ) from error

    def create_customer(
        self,
        *,
        name: str,
        email: str,
        contact: str,
        notes: dict[str, str],
    ) -> RazorpayCustomer:
        payload = self._request(
            "POST",
            "/customers",
            json={
                "name": name,
                "email": email,
                "contact": contact,
                "fail_existing": "0",
                "notes": notes,
            },
        )
        try:
            customer = RazorpayCustomer(
                id=str(payload["id"]),
                name=str(payload["name"]),
                email=str(payload["email"]),
                contact=str(payload["contact"]),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise DomainError(
                "razorpay_invalid_response",
                "Razorpay returned an invalid customer response.",
                502,
            ) from error
        if not customer.id.startswith("cust_"):
            raise DomainError(
                "razorpay_invalid_response",
                "Razorpay returned an invalid customer identifier.",
                502,
            )
        return customer

    def create_mandate_order(
        self,
        *,
        amount: int,
        currency: str,
        receipt: str,
        customer_id: str,
        max_amount: int,
        frequency: str,
        expire_at: int,
        notes: dict[str, str],
    ) -> RazorpayOrder:
        payload = self._request(
            "POST",
            "/orders",
            json={
                "amount": amount,
                "currency": currency,
                "receipt": receipt,
                "method": "upi",
                "customer_id": customer_id,
                "token": {
                    "max_amount": max_amount,
                    "frequency": frequency,
                    "type": "single_block_multiple_debit",
                    "expire_at": expire_at,
                },
                "notes": notes,
            },
        )
        return self._order(payload)

    def create_recurring_order(
        self,
        *,
        amount: int,
        currency: str,
        receipt: str,
        token_id: str,
        payment_after: int,
        notes: dict[str, str],
    ) -> RazorpayRecurringOrder:
        payload = self._request(
            "POST",
            "/orders",
            json={
                "amount": amount,
                "currency": currency,
                "payment_capture": True,
                "receipt": receipt,
                "notification": {
                    "token_id": token_id,
                    "payment_after": payment_after,
                },
                "notes": notes,
            },
        )
        order = self._order(payload)
        try:
            notification_payload = payload["notification"]
            if not isinstance(notification_payload, dict):
                raise TypeError("notification is not an object")
            notification_id = notification_payload["id"]
            response_token_id = notification_payload["token_id"]
            if (
                not isinstance(notification_id, str)
                or not notification_id
                or not isinstance(response_token_id, str)
                or not response_token_id
            ):
                raise ValueError("empty recurring notification identifier")
            notification = RazorpayRecurringNotification(
                id=notification_id,
                token_id=response_token_id,
                payment_after=int(notification_payload["payment_after"]),
                status=(
                    str(notification_payload["status"])
                    if notification_payload.get("status") is not None
                    else None
                ),
                delivered_at=(
                    int(notification_payload["delivered_at"])
                    if notification_payload.get("delivered_at") is not None
                    else None
                ),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise DomainError(
                "razorpay_invalid_response",
                "Razorpay returned an invalid recurring notification response.",
                502,
            ) from error
        if notification.token_id != token_id or notification.payment_after != payment_after:
            raise DomainError(
                "razorpay_recurring_notification_mismatch",
                "Razorpay recurring notification does not match the requested mandate.",
                502,
            )
        return RazorpayRecurringOrder(order=order, notification=notification)

    def create_recurring_payment(
        self,
        *,
        email: str,
        contact: str,
        amount: int,
        currency: str,
        order_id: str,
        customer_id: str,
        token_id: str,
        description: str,
        notes: dict[str, str],
    ) -> RazorpayRecurringDebit:
        payload = self._request(
            "POST",
            "/payments/create/recurring",
            json={
                "email": email,
                "contact": contact,
                "amount": amount,
                "currency": currency,
                "order_id": order_id,
                "customer_id": customer_id,
                "token": token_id,
                "recurring": True,
                "description": description,
                "notes": notes,
            },
        )
        try:
            payment_id = payload["razorpay_payment_id"]
            response_order_id = payload.get("razorpay_order_id") or order_id
            if not isinstance(payment_id, str) or not isinstance(response_order_id, str):
                raise TypeError("recurring payment identifiers are not strings")
            signature = (
                str(payload["razorpay_signature"])
                if payload.get("razorpay_signature") is not None
                else None
            )
            if not payment_id or not response_order_id:
                raise ValueError("empty recurring payment identifier")
        except (KeyError, TypeError, ValueError) as error:
            raise DomainError(
                "razorpay_invalid_response",
                "Razorpay returned an invalid recurring payment response.",
                502,
            ) from error
        if response_order_id != order_id:
            raise DomainError(
                "razorpay_recurring_payment_mismatch",
                "Razorpay recurring payment does not match the requested order.",
                502,
            )
        return RazorpayRecurringDebit(
            payment_id=payment_id,
            order_id=response_order_id,
            signature=signature,
        )

    def verify_checkout_signature(self, *, order_id: str, payment_id: str, signature: str) -> bool:
        expected = hmac.new(
            self._key_secret.encode(),
            f"{order_id}|{payment_id}".encode(),
            hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(expected, signature)

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        try:
            response = httpx.request(
                method,
                f"{self._base_url}{path}",
                auth=(self._key_id, self._key_secret),
                json=json,
                params=params,
                timeout=self._timeout,
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError("Razorpay response is not an object")
            return payload
        except (httpx.HTTPError, ValueError) as error:
            raise DomainError(
                "razorpay_unavailable",
                "Razorpay could not complete the request. Retry with the same checkout.",
                502,
            ) from error

    @staticmethod
    def _order(payload: dict[str, Any]) -> RazorpayOrder:
        try:
            return RazorpayOrder(
                id=str(payload["id"]),
                amount=int(payload["amount"]),
                currency=str(payload["currency"]).upper(),
                receipt=str(payload["receipt"]),
                status=str(payload["status"]),
                amount_paid=int(payload.get("amount_paid", 0)),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise DomainError(
                "razorpay_invalid_response", "Razorpay returned an invalid order response.", 502
            ) from error


def get_razorpay_gateway() -> RazorpayGateway:
    return RazorpayHttpGateway(get_settings())


def get_razorpay_recurring_gateway() -> RazorpayRecurringGateway:
    return RazorpayHttpGateway(get_settings())


def verify_webhook_signature(raw_body: bytes, signature: str, secret: str) -> bool:
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)
