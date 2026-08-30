import uuid

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.dependencies import get_merchant_admin, get_merchant_user
from app.core.database import get_db
from app.db.models import UserAccount
from app.schemas.operations import (
    InventoryRowResponse,
    InventoryUpdateRequest,
    OperationsDashboardResponse,
    OperationsOrderResponse,
    OrderStatusUpdateRequest,
)
from app.services.operations import OperationsService

router = APIRouter(prefix="/merchant/operations", tags=["merchant-operations"])


@router.get("/dashboard", response_model=OperationsDashboardResponse)
def dashboard(
    user: UserAccount = Depends(get_merchant_user), db: Session = Depends(get_db)
) -> OperationsDashboardResponse:
    return OperationsService(db).dashboard(user)


@router.get("/orders", response_model=list[OperationsOrderResponse])
def list_orders(
    user: UserAccount = Depends(get_merchant_user), db: Session = Depends(get_db)
) -> list[OperationsOrderResponse]:
    return OperationsService(db).orders(user)


@router.patch("/orders/{order_id}", response_model=OperationsOrderResponse)
def update_order_status(
    order_id: uuid.UUID,
    payload: OrderStatusUpdateRequest,
    user: UserAccount = Depends(get_merchant_user),
    db: Session = Depends(get_db),
) -> OperationsOrderResponse:
    return OperationsService(db).update_order_status(user, order_id, payload.status)


@router.get("/inventory", response_model=list[InventoryRowResponse])
def list_inventory(
    user: UserAccount = Depends(get_merchant_user), db: Session = Depends(get_db)
) -> list[InventoryRowResponse]:
    return OperationsService(db).inventory(user)


@router.patch("/inventory/{inventory_id}", response_model=InventoryRowResponse)
def update_inventory(
    inventory_id: uuid.UUID,
    payload: InventoryUpdateRequest,
    user: UserAccount = Depends(get_merchant_admin),
    db: Session = Depends(get_db),
) -> InventoryRowResponse:
    return OperationsService(db).update_inventory(user, inventory_id, payload)
