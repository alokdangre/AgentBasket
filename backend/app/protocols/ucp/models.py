from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.domain.enums import FulfillmentType

UCP_VERSION = "2026-08-25"
UCP_SHOPPING_SERVICE = "dev.ucp.shopping"
UCP_CATALOG_SEARCH = "dev.ucp.shopping.catalog.search"
UCP_CATALOG_LOOKUP = "dev.ucp.shopping.catalog.lookup"
UCP_CHECKOUT = "dev.ucp.shopping.checkout"


class UcpEntity(BaseModel):
    version: str = UCP_VERSION
    spec: str | None = None
    schema_url: str | None = Field(default=None, serialization_alias="schema")
    transport: str | None = None
    endpoint: str | None = None

    model_config = ConfigDict(serialize_by_alias=True)


class UcpMetadata(BaseModel):
    version: str = UCP_VERSION
    status: Literal["success", "error"] | None = None
    services: dict[str, list[UcpEntity]] | None = None
    capabilities: dict[str, list[UcpEntity]] | None = None
    payment_handlers: dict[str, list[UcpEntity]] | None = None


class UcpDiscoveryProfile(BaseModel):
    ucp: UcpMetadata


class UcpContext(BaseModel):
    address_country: str | None = Field(default=None, min_length=2, max_length=80)
    address_region: str | None = Field(default=None, max_length=120)
    postal_code: str | None = Field(default=None, min_length=3, max_length=20)
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    fulfillment_type: FulfillmentType = FulfillmentType.LOCAL_DELIVERY
    intent: str | None = Field(default=None, max_length=500)

    model_config = ConfigDict(extra="allow")


class UcpPriceFilter(BaseModel):
    min: int | None = Field(default=None, ge=0)
    max: int | None = Field(default=None, ge=0)


class UcpSearchFilters(BaseModel):
    categories: list[str] = Field(default_factory=list, max_length=20)
    price: UcpPriceFilter | None = None

    model_config = ConfigDict(extra="allow")


class UcpPaginationRequest(BaseModel):
    cursor: str | None = Field(default=None, max_length=200)
    limit: int = Field(default=10, ge=1, le=50)


class UcpCatalogSearchRequest(BaseModel):
    query: str | None = Field(default=None, max_length=500)
    context: UcpContext | None = None
    signals: dict[str, Any] | None = None
    attribution: dict[str, Any] | None = None
    filters: UcpSearchFilters | None = None
    pagination: UcpPaginationRequest = Field(default_factory=UcpPaginationRequest)


class UcpCatalogLookupRequest(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=50)
    context: UcpContext | None = None
    signals: dict[str, Any] | None = None
    attribution: dict[str, Any] | None = None
    filters: UcpSearchFilters | None = None


class UcpSelectedOption(BaseModel):
    name: str
    label: str


class UcpGetProductRequest(BaseModel):
    id: str = Field(min_length=1, max_length=200)
    selected: list[UcpSelectedOption] = Field(default_factory=list, max_length=30)
    preferences: list[str] = Field(default_factory=list, max_length=30)
    context: UcpContext | None = None
    signals: dict[str, Any] | None = None
    attribution: dict[str, Any] | None = None
    filters: UcpSearchFilters | None = None


class UcpDescription(BaseModel):
    plain: str


class UcpPrice(BaseModel):
    amount: int = Field(ge=0)
    currency: str = Field(min_length=3, max_length=3)


class UcpPriceRange(BaseModel):
    min: UcpPrice
    max: UcpPrice


class UcpAvailability(BaseModel):
    available: bool


class UcpMedia(BaseModel):
    type: Literal["image"] = "image"
    url: str
    alt_text: str | None = None


class UcpCategory(BaseModel):
    value: str
    taxonomy: str | None = None


class UcpOptionValue(BaseModel):
    label: str
    available: bool | None = None
    exists: bool | None = None


class UcpProductOption(BaseModel):
    name: str
    values: list[UcpOptionValue]


class UcpInputCorrelation(BaseModel):
    id: str
    match: Literal["exact", "featured"]


class UcpSeller(BaseModel):
    name: str


class UcpVariant(BaseModel):
    id: str
    sku: str
    title: str
    description: UcpDescription
    url: str
    price: UcpPrice
    availability: UcpAvailability
    options: list[UcpSelectedOption] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    seller: UcpSeller | None = None
    inputs: list[UcpInputCorrelation] | None = None


class UcpProduct(BaseModel):
    id: str
    handle: str
    title: str
    description: UcpDescription
    url: str
    categories: list[UcpCategory]
    price_range: UcpPriceRange
    media: list[UcpMedia] = Field(default_factory=list)
    options: list[UcpProductOption] = Field(default_factory=list)
    variants: list[UcpVariant]
    tags: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    selected: list[UcpSelectedOption] | None = None


class UcpPaginationResponse(BaseModel):
    cursor: str | None = None
    has_next_page: bool
    total_count: int = Field(ge=0)


class UcpMessage(BaseModel):
    type: Literal["error", "warning", "info"]
    code: str
    content: str
    severity: (
        Literal[
            "recoverable",
            "requires_buyer_input",
            "requires_buyer_review",
            "unrecoverable",
        ]
        | None
    ) = None
    path: str | None = None


class UcpCatalogSearchResponse(BaseModel):
    ucp: UcpMetadata
    products: list[UcpProduct]
    pagination: UcpPaginationResponse
    messages: list[UcpMessage] = Field(default_factory=list)


class UcpCatalogLookupResponse(BaseModel):
    ucp: UcpMetadata
    products: list[UcpProduct]
    messages: list[UcpMessage] = Field(default_factory=list)


class UcpGetProductResponse(BaseModel):
    ucp: UcpMetadata
    product: UcpProduct
    messages: list[UcpMessage] = Field(default_factory=list)


class UcpErrorResponse(BaseModel):
    ucp: UcpMetadata
    messages: list[UcpMessage]
    continue_url: str | None = None


class UcpCheckoutItemCreate(BaseModel):
    id: str = Field(min_length=1, max_length=200)

    model_config = ConfigDict(extra="ignore")


class UcpCheckoutLineCreate(BaseModel):
    id: str | None = Field(default=None, max_length=200)
    item: UcpCheckoutItemCreate
    quantity: int = Field(ge=1, le=25)

    model_config = ConfigDict(extra="ignore")


class UcpCheckoutCreateRequest(BaseModel):
    line_items: list[UcpCheckoutLineCreate] = Field(min_length=1, max_length=30)
    buyer: dict[str, Any] | None = None
    context: dict[str, Any] | None = None
    signals: dict[str, Any] | None = None
    attribution: dict[str, Any] | None = None

    model_config = ConfigDict(extra="ignore")


class UcpCheckoutItem(BaseModel):
    id: str
    title: str
    price: int = Field(ge=0)
    image_url: str | None = None


class UcpCheckoutTotal(BaseModel):
    type: Literal["subtotal", "total"]
    amount: int = Field(ge=0)


class UcpCheckoutLine(BaseModel):
    id: str
    item: UcpCheckoutItem
    quantity: int = Field(ge=1)
    totals: list[UcpCheckoutTotal]


class UcpCheckoutLink(BaseModel):
    type: str
    url: str
    title: str | None = None


class UcpCheckoutResponse(BaseModel):
    ucp: UcpMetadata
    id: str
    status: Literal["requires_escalation", "canceled"]
    currency: str = Field(min_length=3, max_length=3)
    line_items: list[UcpCheckoutLine]
    totals: list[UcpCheckoutTotal]
    links: list[UcpCheckoutLink]
    messages: list[UcpMessage] = Field(default_factory=list)
    expires_at: str
    continue_url: str | None = None


class UcpCheckoutClaimItem(BaseModel):
    variant_id: str
    product_name: str
    product_slug: str
    shop_url: str


class UcpCheckoutClaimResponse(BaseModel):
    session_id: str
    status: Literal["claimed"]
    imported_item_count: int = Field(ge=0)
    configuration_required: list[UcpCheckoutClaimItem] = Field(default_factory=list)
    next_url: str
