import uuid

from fastapi import APIRouter, Depends, Header
from sqlalchemy.orm import Session

from app.api.dependencies import get_current_user
from app.core.database import get_db
from app.db.models import UserAccount
from app.schemas.checkout import (
    CheckoutApprovalCreate,
    CheckoutApprovalOut,
    CheckoutCancelOut,
    CheckoutCreate,
    CheckoutFromCartCreate,
    CheckoutOut,
)
from app.services.checkout import CheckoutService

router = APIRouter(prefix="/checkouts", tags=["checkouts"])


@router.post("", response_model=CheckoutOut, status_code=201)
def create_checkout(
    payload: CheckoutCreate,
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=8, max_length=128),
    user: UserAccount = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> CheckoutOut:
    return CheckoutService(db).create(payload, idempotency_key, user)


@router.post("/from-cart", response_model=CheckoutOut, status_code=201)
def create_checkout_from_cart(
    payload: CheckoutFromCartCreate,
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=8, max_length=128),
    user: UserAccount = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> CheckoutOut:
    return CheckoutService(db).create_from_cart(payload, idempotency_key, user)


@router.get("/{checkout_id}", response_model=CheckoutOut)
def get_checkout(
    checkout_id: uuid.UUID,
    user: UserAccount = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> CheckoutOut:
    return CheckoutService(db).get(checkout_id, user)


@router.post("/{checkout_id}/cancel", response_model=CheckoutCancelOut)
def cancel_checkout(
    checkout_id: uuid.UUID,
    user: UserAccount = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> CheckoutCancelOut:
    return CheckoutService(db).cancel(checkout_id, user)


@router.post("/{checkout_id}/approve", response_model=CheckoutApprovalOut)
def approve_checkout(
    checkout_id: uuid.UUID,
    payload: CheckoutApprovalCreate,
    user: UserAccount = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> CheckoutApprovalOut:
    return CheckoutService(db).approve(checkout_id, payload, user)
