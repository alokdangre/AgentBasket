import uuid

from fastapi import APIRouter, Depends, Header
from sqlalchemy.orm import Session

from app.api.dependencies import get_current_user, get_merchant_user, get_trusted_surface
from app.core.database import get_db
from app.db.models import UserAccount
from app.domain.enums import ScheduledRunStatus
from app.payments.razorpay import (
    RazorpayRecurringGateway,
    get_razorpay_recurring_gateway,
)
from app.protocols.ap2.crypto import AP2KeySet, get_ap2_key_set
from app.schemas.scheduled_purchase import (
    ScheduledPurchaseActionOut,
    ScheduledPurchaseAuthorizationCreate,
    ScheduledPurchaseAuthorizationOut,
    ScheduledPurchaseChallengeOut,
    ScheduledPurchaseDraftCreate,
    ScheduledPurchaseListOut,
    ScheduledPurchaseOut,
    ScheduledPurchaseRunOut,
)
from app.services.scheduled_execution import ScheduledPurchaseExecutor
from app.services.scheduled_purchase import ScheduledPurchaseService
from app.services.trusted_surface import TrustedSurfaceService

router = APIRouter(prefix="/scheduled-purchases", tags=["scheduled-purchases"])
merchant_router = APIRouter(
    prefix="/merchant/operations/scheduled-purchases",
    tags=["merchant-operations"],
)


@router.post("", response_model=ScheduledPurchaseOut, status_code=201)
def create_scheduled_purchase(
    payload: ScheduledPurchaseDraftCreate,
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=8, max_length=128),
    user: UserAccount = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ScheduledPurchaseOut:
    return ScheduledPurchaseService(db).create_draft(payload, user, idempotency_key)


@router.get("", response_model=ScheduledPurchaseListOut)
def list_scheduled_purchases(
    user: UserAccount = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ScheduledPurchaseListOut:
    return ScheduledPurchaseService(db).list(user)


@router.get("/{intent_id}", response_model=ScheduledPurchaseOut)
def get_scheduled_purchase(
    intent_id: uuid.UUID,
    user: UserAccount = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ScheduledPurchaseOut:
    return ScheduledPurchaseService(db).get(intent_id, user)


@router.post(
    "/{intent_id}/authorization/challenge",
    response_model=ScheduledPurchaseChallengeOut,
    status_code=201,
)
def create_scheduled_purchase_authorization_challenge(
    intent_id: uuid.UUID,
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=8, max_length=128),
    user: UserAccount = Depends(get_current_user),
    db: Session = Depends(get_db),
    keys: AP2KeySet = Depends(get_ap2_key_set),
    trusted_surface: TrustedSurfaceService = Depends(get_trusted_surface),
) -> ScheduledPurchaseChallengeOut:
    return ScheduledPurchaseService(db, keys, trusted_surface).create_authorization_challenge(
        intent_id, user, idempotency_key
    )


@router.post(
    "/{intent_id}/authorization/approve",
    response_model=ScheduledPurchaseAuthorizationOut,
)
def approve_scheduled_purchase_authorization(
    intent_id: uuid.UUID,
    payload: ScheduledPurchaseAuthorizationCreate,
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=8, max_length=128),
    user: UserAccount = Depends(get_current_user),
    db: Session = Depends(get_db),
    keys: AP2KeySet = Depends(get_ap2_key_set),
    trusted_surface: TrustedSurfaceService = Depends(get_trusted_surface),
) -> ScheduledPurchaseAuthorizationOut:
    return ScheduledPurchaseService(db, keys, trusted_surface).authorize(
        intent_id, payload, user, idempotency_key
    )


@router.post("/{intent_id}/pause", response_model=ScheduledPurchaseActionOut)
def pause_scheduled_purchase(
    intent_id: uuid.UUID,
    _idempotency_key: str = Header(alias="Idempotency-Key", min_length=8, max_length=128),
    user: UserAccount = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ScheduledPurchaseActionOut:
    return ScheduledPurchaseService(db).pause(intent_id, user)


@router.post("/{intent_id}/resume", response_model=ScheduledPurchaseActionOut)
def resume_scheduled_purchase(
    intent_id: uuid.UUID,
    _idempotency_key: str = Header(alias="Idempotency-Key", min_length=8, max_length=128),
    user: UserAccount = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ScheduledPurchaseActionOut:
    return ScheduledPurchaseService(db).resume(intent_id, user)


@router.post("/{intent_id}/revoke", response_model=ScheduledPurchaseActionOut)
def revoke_scheduled_purchase(
    intent_id: uuid.UUID,
    _idempotency_key: str = Header(alias="Idempotency-Key", min_length=8, max_length=128),
    user: UserAccount = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ScheduledPurchaseActionOut:
    return ScheduledPurchaseService(db).revoke(intent_id, user)


@router.post("/{intent_id}/run-now", response_model=ScheduledPurchaseRunOut)
def run_scheduled_purchase_now(
    intent_id: uuid.UUID,
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=8, max_length=128),
    user: UserAccount = Depends(get_current_user),
    db: Session = Depends(get_db),
    keys: AP2KeySet = Depends(get_ap2_key_set),
    gateway: RazorpayRecurringGateway = Depends(get_razorpay_recurring_gateway),
) -> ScheduledPurchaseRunOut:
    executor = ScheduledPurchaseExecutor(db, keys)
    run_id = executor.claim_for_test(intent_id, user, idempotency_key)
    current = executor.get_run(run_id)
    if current.status != ScheduledRunStatus.CLAIMED:
        return current
    return executor.execute_claimed(run_id, gateway)


@merchant_router.get("", response_model=list[ScheduledPurchaseOut])
def list_merchant_scheduled_purchases(
    user: UserAccount = Depends(get_merchant_user),
    db: Session = Depends(get_db),
) -> list[ScheduledPurchaseOut]:
    return ScheduledPurchaseService(db).merchant_list(user)
