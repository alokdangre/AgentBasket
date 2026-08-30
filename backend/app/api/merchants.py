from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.schemas.merchant import MerchantOut
from app.services.location import LocationService

router = APIRouter(prefix="/merchants", tags=["merchants"])


@router.get("/{merchant_slug}", response_model=MerchantOut)
def get_merchant(merchant_slug: str, db: Session = Depends(get_db)) -> MerchantOut:
    return MerchantOut.model_validate(LocationService(db).merchant_by_slug(merchant_slug))
