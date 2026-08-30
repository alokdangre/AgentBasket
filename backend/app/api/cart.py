import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.dependencies import get_current_user
from app.core.database import get_db
from app.db.models import UserAccount
from app.schemas.cart import CartItemCreateRequest, CartItemUpdateRequest, CartResponse
from app.services.cart import CartService

router = APIRouter(prefix="/cart", tags=["cart"])


@router.get("", response_model=CartResponse)
def get_cart(
    merchant_slug: str = Query(default="ember-and-leaf", min_length=1, max_length=80),
    user: UserAccount = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> CartResponse:
    return CartService(db).get(user, merchant_slug)


@router.post("/items", response_model=CartResponse, status_code=201)
def add_cart_item(
    payload: CartItemCreateRequest,
    merchant_slug: str = Query(default="ember-and-leaf", min_length=1, max_length=80),
    user: UserAccount = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> CartResponse:
    return CartService(db).add_item(user, merchant_slug, payload)


@router.patch("/items/{item_id}", response_model=CartResponse)
def update_cart_item(
    item_id: uuid.UUID,
    payload: CartItemUpdateRequest,
    merchant_slug: str = Query(default="ember-and-leaf", min_length=1, max_length=80),
    user: UserAccount = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> CartResponse:
    return CartService(db).update_item(user, merchant_slug, item_id, payload)


@router.delete("/items/{item_id}", response_model=CartResponse)
def remove_cart_item(
    item_id: uuid.UUID,
    merchant_slug: str = Query(default="ember-and-leaf", min_length=1, max_length=80),
    user: UserAccount = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> CartResponse:
    return CartService(db).remove_item(user, merchant_slug, item_id)
