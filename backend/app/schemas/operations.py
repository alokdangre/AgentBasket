import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.domain.enums import OrderStatus


class OperationsSummaryResponse(BaseModel):
    open_checkouts: int
    orders_preparing: int
    low_stock_variants: int
    captured_revenue_minor: int
    currency: str


class OperationsOrderResponse(BaseModel):
    id: uuid.UUID
    public_number: str
    customer_name: str | None
    fulfillment_type: str
    total_minor: int
    currency: str
    status: OrderStatus
    created_at: datetime


class InventoryRowResponse(BaseModel):
    id: uuid.UUID
    variant_id: uuid.UUID
    sku: str
    product_name: str
    variant_name: str
    location_name: str
    on_hand_quantity: int
    reserved_quantity: int
    available_quantity: int
    reorder_point: int


class ProtocolCapabilityResponse(BaseModel):
    name: str
    version: str | None = None
    status: Literal["active", "metadata_only", "planned"]
    endpoint: str
    detail: str


class PaymentRailResponse(BaseModel):
    name: str
    status: Literal["configured", "unconfigured", "disabled"]
    mode: str
    detail: str


class CommerceReadinessResponse(BaseModel):
    protocols: list[ProtocolCapabilityResponse]
    payments: list[PaymentRailResponse]
    warnings: list[str]


class OperationsDashboardResponse(BaseModel):
    summary: OperationsSummaryResponse
    recent_orders: list[OperationsOrderResponse]
    inventory_attention: list[InventoryRowResponse]
    commerce_readiness: CommerceReadinessResponse


class InventoryUpdateRequest(BaseModel):
    on_hand_quantity: int = Field(ge=0, le=1_000_000)
    reason: str = Field(min_length=3, max_length=300)


class OrderStatusUpdateRequest(BaseModel):
    status: OrderStatus
