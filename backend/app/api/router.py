from fastapi import APIRouter

from app.api import (
    accounts,
    agent,
    cart,
    catalog,
    checkouts,
    locations,
    merchants,
    operations,
    payments,
)

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(merchants.router)
api_router.include_router(locations.router)
api_router.include_router(catalog.router)
api_router.include_router(checkouts.router)
api_router.include_router(accounts.router)
api_router.include_router(cart.router)
api_router.include_router(operations.router)
api_router.include_router(payments.router)
api_router.include_router(agent.router)
