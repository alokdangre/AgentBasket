import uuid

from fastapi import APIRouter, Depends, Header
from sqlalchemy.orm import Session

from app.api.dependencies import get_current_user
from app.core.database import get_db
from app.db.models import UserAccount
from app.protocols.ap2.crypto import AP2KeySet, get_ap2_key_set
from app.protocols.ap2.models import (
    AP2ApprovalCreate,
    AP2ApprovalOut,
    AP2ChallengeOut,
    AP2EvidenceOut,
)
from app.schemas.checkout import (
    CheckoutApprovalCreate,
    CheckoutApprovalOut,
    CheckoutCancelOut,
    CheckoutCreate,
    CheckoutFromCartCreate,
    CheckoutOut,
)
from app.services.ap2 import AP2Service
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


@router.post("/{checkout_id}/ap2/challenge", response_model=AP2ChallengeOut, status_code=201)
def create_ap2_challenge(
    checkout_id: uuid.UUID,
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=8, max_length=128),
    user: UserAccount = Depends(get_current_user),
    db: Session = Depends(get_db),
    keys: AP2KeySet = Depends(get_ap2_key_set),
) -> AP2ChallengeOut:
    return AP2Service(db, keys).create_challenge(checkout_id, idempotency_key, user)


@router.post("/{checkout_id}/ap2/approve", response_model=AP2ApprovalOut)
def approve_ap2_checkout(
    checkout_id: uuid.UUID,
    payload: AP2ApprovalCreate,
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=8, max_length=128),
    user: UserAccount = Depends(get_current_user),
    db: Session = Depends(get_db),
    keys: AP2KeySet = Depends(get_ap2_key_set),
) -> AP2ApprovalOut:
    return AP2Service(db, keys).approve(checkout_id, payload, idempotency_key, user)


@router.get("/{checkout_id}/ap2/evidence", response_model=AP2EvidenceOut)
def get_ap2_evidence(
    checkout_id: uuid.UUID,
    user: UserAccount = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> AP2EvidenceOut:
    return AP2Service(db).evidence(checkout_id, user)
