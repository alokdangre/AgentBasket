import uuid

from fastapi import APIRouter, Depends, Header, Request
from sqlalchemy.orm import Session

from app.api.dependencies import get_current_user
from app.core.database import get_db
from app.db.models import UserAccount
from app.payments.razorpay import RazorpayGateway, get_razorpay_gateway
from app.schemas.payment import (
    OrderReceiptOut,
    OrderSummaryOut,
    RazorpaySessionOut,
    RazorpayVerifyRequest,
    WebhookResultOut,
)
from app.services.payment import PaymentService

router = APIRouter(tags=["payments"])


@router.post("/checkouts/{checkout_id}/payment-session", response_model=RazorpaySessionOut)
def create_payment_session(
    checkout_id: uuid.UUID,
    user: UserAccount = Depends(get_current_user),
    db: Session = Depends(get_db),
    gateway: RazorpayGateway = Depends(get_razorpay_gateway),
) -> RazorpaySessionOut:
    return PaymentService(db, gateway).create_session(checkout_id, user)


@router.post("/payments/razorpay/verify", response_model=OrderReceiptOut)
def verify_payment(
    payload: RazorpayVerifyRequest,
    user: UserAccount = Depends(get_current_user),
    db: Session = Depends(get_db),
    gateway: RazorpayGateway = Depends(get_razorpay_gateway),
) -> OrderReceiptOut:
    return PaymentService(db, gateway).verify_checkout_result(payload, user)


@router.get("/orders", response_model=list[OrderSummaryOut])
def list_orders(
    user: UserAccount = Depends(get_current_user), db: Session = Depends(get_db)
) -> list[OrderSummaryOut]:
    return PaymentService(db).orders(user)


@router.get("/orders/{order_id}", response_model=OrderReceiptOut)
def get_order(
    order_id: uuid.UUID,
    user: UserAccount = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> OrderReceiptOut:
    return PaymentService(db).receipt(order_id, user)


@router.post("/webhooks/razorpay", response_model=WebhookResultOut)
async def razorpay_webhook(
    request: Request,
    x_razorpay_signature: str = Header(default=""),
    x_razorpay_event_id: str = Header(default=""),
    db: Session = Depends(get_db),
) -> WebhookResultOut:
    raw_body = await request.body()
    return PaymentService(db).handle_webhook(raw_body, x_razorpay_signature, x_razorpay_event_id)
