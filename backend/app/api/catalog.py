from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.domain.enums import FulfillmentType
from app.schemas.catalog import CatalogResponse
from app.services.catalog import CatalogService

router = APIRouter(prefix="/merchants/{merchant_slug}/catalog", tags=["catalog"])


@router.get("", response_model=CatalogResponse)
def search_catalog(
    merchant_slug: str,
    query: str | None = Query(default=None, min_length=1, max_length=120),
    postal_code: str | None = Query(default=None, min_length=3, max_length=20),
    fulfillment_type: FulfillmentType = Query(default=FulfillmentType.LOCAL_DELIVERY),
    db: Session = Depends(get_db),
) -> CatalogResponse:
    return CatalogService(db).search(
        merchant_slug=merchant_slug,
        query=query,
        postal_code=postal_code,
        fulfillment_type=fulfillment_type,
    )
