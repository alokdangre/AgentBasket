from typing import Any

import httpx
import pytest

from app.core.config import Settings
from app.core.errors import DomainError
from app.payments.razorpay import RazorpayHttpGateway


def _gateway() -> RazorpayHttpGateway:
    return RazorpayHttpGateway(
        Settings(
            _env_file=None,
            razorpay_key_id="rzp_test_recurring",
            razorpay_key_secret="recurring-test-secret",
            razorpay_api_url="https://api.razorpay.com/v1",
            razorpay_timeout_seconds=9,
        )
    )


def _response(payload: dict[str, Any]) -> httpx.Response:
    return httpx.Response(
        200,
        json=payload,
        request=httpx.Request("POST", "https://api.razorpay.com/v1/orders"),
    )


def test_create_customer_sends_server_side_registration_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def request(method: str, url: str, **kwargs: Any) -> httpx.Response:
        captured.update(method=method, url=url, **kwargs)
        return _response(
            {
                "id": "cust_recurring_001",
                "name": "Aarav Mehta",
                "email": "aarav@example.com",
                "contact": "+919876543210",
            }
        )

    monkeypatch.setattr(httpx, "request", request)

    customer = _gateway().create_customer(
        name="Aarav Mehta",
        email="aarav@example.com",
        contact="+919876543210",
        notes={"agentbasket_customer_id": "customer-001"},
    )

    assert captured == {
        "method": "POST",
        "url": "https://api.razorpay.com/v1/customers",
        "auth": ("rzp_test_recurring", "recurring-test-secret"),
        "json": {
            "name": "Aarav Mehta",
            "email": "aarav@example.com",
            "contact": "+919876543210",
            "fail_existing": "0",
            "notes": {"agentbasket_customer_id": "customer-001"},
        },
        "params": None,
        "timeout": 9.0,
    }
    assert customer.id == "cust_recurring_001"


def test_create_mandate_order_sends_bounded_upi_autopay_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def request(method: str, url: str, **kwargs: Any) -> httpx.Response:
        captured.update(method=method, url=url, **kwargs)
        return _response(
            {
                "id": "order_mandate_001",
                "amount": 100,
                "currency": "INR",
                "receipt": "abm-schedule-001",
                "status": "created",
            }
        )

    monkeypatch.setattr(httpx, "request", request)

    order = _gateway().create_mandate_order(
        amount=100,
        currency="INR",
        receipt="abm-schedule-001",
        customer_id="cust_recurring_001",
        max_amount=30_000,
        frequency="as_presented",
        expire_at=1_788_320_000,
        notes={"scheduled_purchase_id": "schedule-001"},
    )

    assert captured == {
        "method": "POST",
        "url": "https://api.razorpay.com/v1/orders",
        "auth": ("rzp_test_recurring", "recurring-test-secret"),
        "json": {
            "amount": 100,
            "currency": "INR",
            "receipt": "abm-schedule-001",
            "method": "upi",
            "customer_id": "cust_recurring_001",
            "token": {
                "max_amount": 30_000,
                "frequency": "as_presented",
                "type": "single_block_multiple_debit",
                "expire_at": 1_788_320_000,
            },
            "notes": {"scheduled_purchase_id": "schedule-001"},
        },
        "params": None,
        "timeout": 9.0,
    }
    assert order.id == "order_mandate_001"


def test_create_recurring_order_sends_official_notification_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def request(method: str, url: str, **kwargs: Any) -> httpx.Response:
        captured.update(method=method, url=url, **kwargs)
        return _response(
            {
                "id": "order_recurring_001",
                "entity": "order",
                "amount": 29900,
                "amount_paid": 0,
                "amount_due": 29900,
                "currency": "INR",
                "receipt": "ab-scheduled-run-001",
                "status": "created",
                "notification": {
                    "id": "notification_001",
                    "token_id": "token_upi_001",
                    "payment_after": 1_788_320_000,
                },
            }
        )

    monkeypatch.setattr(httpx, "request", request)

    result = _gateway().create_recurring_order(
        amount=29900,
        currency="INR",
        receipt="ab-scheduled-run-001",
        token_id="token_upi_001",
        payment_after=1_788_320_000,
        notes={"scheduled_run_id": "run-001"},
    )

    assert captured == {
        "method": "POST",
        "url": "https://api.razorpay.com/v1/orders",
        "auth": ("rzp_test_recurring", "recurring-test-secret"),
        "json": {
            "amount": 29900,
            "currency": "INR",
            "payment_capture": True,
            "receipt": "ab-scheduled-run-001",
            "notification": {
                "token_id": "token_upi_001",
                "payment_after": 1_788_320_000,
            },
            "notes": {"scheduled_run_id": "run-001"},
        },
        "params": None,
        "timeout": 9.0,
    }
    assert result.order.id == "order_recurring_001"
    assert result.order.status == "created"
    assert result.notification.id == "notification_001"
    assert result.notification.status is None
    assert result.notification.delivered_at is None


def test_create_recurring_payment_sends_official_server_to_server_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def request(method: str, url: str, **kwargs: Any) -> httpx.Response:
        captured.update(method=method, url=url, **kwargs)
        return _response({"razorpay_payment_id": "pay_recurring_001"})

    monkeypatch.setattr(httpx, "request", request)

    result = _gateway().create_recurring_payment(
        email="customer@example.com",
        contact="+919876543210",
        amount=29900,
        currency="INR",
        order_id="order_recurring_001",
        customer_id="cust_recurring_001",
        token_id="token_upi_001",
        description="Scheduled ginger tea order",
        notes={"scheduled_run_id": "run-001"},
    )

    assert captured == {
        "method": "POST",
        "url": "https://api.razorpay.com/v1/payments/create/recurring",
        "auth": ("rzp_test_recurring", "recurring-test-secret"),
        "json": {
            "email": "customer@example.com",
            "contact": "+919876543210",
            "amount": 29900,
            "currency": "INR",
            "order_id": "order_recurring_001",
            "customer_id": "cust_recurring_001",
            "token": "token_upi_001",
            "recurring": True,
            "description": "Scheduled ginger tea order",
            "notes": {"scheduled_run_id": "run-001"},
        },
        "params": None,
        "timeout": 9.0,
    }
    assert result.payment_id == "pay_recurring_001"
    assert result.order_id == "order_recurring_001"
    assert result.signature is None


def test_create_recurring_order_rejects_notification_for_another_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def request(method: str, url: str, **kwargs: Any) -> httpx.Response:
        return _response(
            {
                "id": "order_recurring_001",
                "amount": 29900,
                "currency": "INR",
                "receipt": "ab-scheduled-run-001",
                "status": "created",
                "notification": {
                    "id": "notification_001",
                    "token_id": "token_other",
                    "payment_after": 1_788_320_000,
                },
            }
        )

    monkeypatch.setattr(httpx, "request", request)

    with pytest.raises(DomainError) as failure:
        _gateway().create_recurring_order(
            amount=29900,
            currency="INR",
            receipt="ab-scheduled-run-001",
            token_id="token_upi_001",
            payment_after=1_788_320_000,
            notes={},
        )

    assert failure.value.code == "razorpay_recurring_notification_mismatch"
    assert failure.value.status_code == 502


def test_create_recurring_payment_rejects_provider_order_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def request(method: str, url: str, **kwargs: Any) -> httpx.Response:
        return _response(
            {
                "razorpay_payment_id": "pay_recurring_001",
                "razorpay_order_id": "order_other",
            }
        )

    monkeypatch.setattr(httpx, "request", request)

    with pytest.raises(DomainError) as failure:
        _gateway().create_recurring_payment(
            email="customer@example.com",
            contact="+919876543210",
            amount=29900,
            currency="INR",
            order_id="order_recurring_001",
            customer_id="cust_recurring_001",
            token_id="token_upi_001",
            description="Scheduled ginger tea order",
            notes={},
        )

    assert failure.value.code == "razorpay_recurring_payment_mismatch"
    assert failure.value.status_code == 502
