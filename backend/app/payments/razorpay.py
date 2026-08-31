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
            )
        except (KeyError, TypeError, ValueError) as error:
            raise DomainError(
                "razorpay_invalid_response", "Razorpay returned an invalid payment response.", 502
            ) from error

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


def verify_webhook_signature(raw_body: bytes, signature: str, secret: str) -> bool:
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)
