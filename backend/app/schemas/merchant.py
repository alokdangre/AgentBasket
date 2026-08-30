import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict

from app.domain.enums import MerchantStatus


class MerchantOut(BaseModel):
    id: uuid.UUID
    slug: str
    name: str
    currency: str
    timezone: str
    status: MerchantStatus
    settings: dict[str, Any]

    model_config = ConfigDict(from_attributes=True)
