from __future__ import annotations

import base64
import json
import re
from collections.abc import Iterable

from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.errors import DomainError, NotFoundError
from app.domain.enums import FulfillmentType
from app.protocols.ucp.models import (
    UCP_CATALOG_LOOKUP,
    UCP_CATALOG_SEARCH,
    UCP_CHECKOUT,
    UCP_SHOPPING_SERVICE,
    UCP_VERSION,
    UcpAvailability,
    UcpCatalogLookupRequest,
    UcpCatalogLookupResponse,
    UcpCatalogSearchRequest,
    UcpCatalogSearchResponse,
    UcpCategory,
    UcpDescription,
    UcpDiscoveryProfile,
    UcpEntity,
    UcpErrorResponse,
    UcpGetProductRequest,
    UcpGetProductResponse,
    UcpInputCorrelation,
    UcpMedia,
    UcpMessage,
    UcpMetadata,
    UcpOptionValue,
    UcpPaginationResponse,
    UcpPrice,
    UcpPriceRange,
    UcpProduct,
    UcpProductOption,
    UcpSearchFilters,
    UcpSelectedOption,
    UcpSeller,
    UcpVariant,
)
from app.schemas.catalog import CatalogProductOut, ProductVariantOut
from app.services.catalog import CatalogService

_SEARCH_STOP_WORDS = {
    "a",
    "an",
    "and",
    "for",
    "in",
    "me",
    "of",
    "or",
    "something",
    "the",
    "to",
    "under",
    "with",
}


class UcpCatalogService:
    def __init__(self, db: Session, settings: Settings) -> None:
        self.catalog = CatalogService(db)
        self.settings = settings

    def discovery_profile(self) -> UcpDiscoveryProfile:
        endpoint = f"{self.settings.ucp_public_base_url.rstrip('/')}/ucp"
        return UcpDiscoveryProfile(
            ucp=UcpMetadata(
                services={
                    UCP_SHOPPING_SERVICE: [
                        UcpEntity(
                            spec=f"https://ucp.dev/{UCP_VERSION}/specification/overview",
                            schema_url=(
                                f"https://ucp.dev/{UCP_VERSION}/services/shopping/rest.openapi.json"
                            ),
                            transport="rest",
                            endpoint=endpoint,
                        )
                    ]
                },
                capabilities={
                    UCP_CATALOG_SEARCH: [
                        UcpEntity(
                            spec=(
                                f"https://ucp.dev/{UCP_VERSION}/specification/shopping/"
                                "catalog/search"
                            ),
                            schema_url=(
                                f"https://ucp.dev/{UCP_VERSION}/schemas/shopping/"
                                "catalog_search.json"
                            ),
                        )
                    ],
                    UCP_CATALOG_LOOKUP: [
                        UcpEntity(
                            spec=(
                                f"https://ucp.dev/{UCP_VERSION}/specification/shopping/"
                                "catalog/lookup"
                            ),
                            schema_url=(
                                f"https://ucp.dev/{UCP_VERSION}/schemas/shopping/"
                                "catalog_lookup.json"
                            ),
                        )
                    ],
                    UCP_CHECKOUT: [
                        UcpEntity(
                            spec=(f"https://ucp.dev/{UCP_VERSION}/specification/shopping/checkout"),
                            schema_url=(
                                f"https://ucp.dev/{UCP_VERSION}/schemas/shopping/checkout.json"
                            ),
                        )
                    ],
                },
                payment_handlers={},
            )
        )

    def search(self, request: UcpCatalogSearchRequest) -> UcpCatalogSearchResponse:
        messages: list[UcpMessage] = []
        context = request.context
        if context and context.currency and context.currency.upper() != "INR":
            messages.append(
                UcpMessage(
                    type="info",
                    code="currency_not_supported",
                    content="Ember & Leaf currently presents catalog prices in INR.",
                )
            )

        try:
            catalog = self.catalog.search(
                merchant_slug="ember-and-leaf",
                postal_code=context.postal_code if context else None,
                fulfillment_type=(
                    context.fulfillment_type if context else FulfillmentType.LOCAL_DELIVERY
                ),
            )
        except NotFoundError as error:
            if error.code not in {"address_not_serviceable", "pickup_location_not_found"}:
                raise
            messages.append(UcpMessage(type="info", code=error.code, content=error.message))
            products: list[CatalogProductOut] = []
        else:
            products = list(catalog.products)

        products = self._search_products(products, request.query)
        products = self._filter_products(products, request.filters)
        projected = [self._project_product(product) for product in products]

        start = self._decode_cursor(request.pagination.cursor)
        end = start + request.pagination.limit
        page = projected[start:end]
        has_next_page = end < len(projected)
        return UcpCatalogSearchResponse(
            ucp=self._response_metadata(UCP_CATALOG_SEARCH),
            products=page,
            pagination=UcpPaginationResponse(
                cursor=self._encode_cursor(end) if has_next_page else None,
                has_next_page=has_next_page,
                total_count=len(projected),
            ),
            messages=messages,
        )

    def lookup(self, request: UcpCatalogLookupRequest) -> UcpCatalogLookupResponse:
        products = self._catalog_products(request.context)
        identifiers = list(dict.fromkeys(identifier.strip() for identifier in request.ids))
        found: list[UcpProduct] = []
        matched_ids: set[str] = set()

        for product in products:
            correlations: dict[str, list[UcpInputCorrelation]] = {}
            product_identifiers = {str(product.id), product.slug}
            for identifier in identifiers:
                if identifier in product_identifiers:
                    featured = product.variants[0]
                    correlations.setdefault(str(featured.id), []).append(
                        UcpInputCorrelation(id=identifier, match="featured")
                    )
                    matched_ids.add(identifier)
                    continue
                for variant in product.variants:
                    if identifier in {str(variant.id), variant.sku}:
                        correlations.setdefault(str(variant.id), []).append(
                            UcpInputCorrelation(id=identifier, match="exact")
                        )
                        matched_ids.add(identifier)

            if correlations:
                variants = [
                    variant for variant in product.variants if str(variant.id) in correlations
                ]
                found.append(self._project_product(product, variants, correlations))

        found = self._filter_projected_products(found, request.filters)
        missing = [identifier for identifier in identifiers if identifier not in matched_ids]
        return UcpCatalogLookupResponse(
            ucp=self._response_metadata(UCP_CATALOG_LOOKUP),
            products=found,
            messages=[
                UcpMessage(type="info", code="not_found", content=identifier)
                for identifier in missing
            ],
        )

    def get_product(
        self, request: UcpGetProductRequest
    ) -> UcpGetProductResponse | UcpErrorResponse:
        products = self._catalog_products(request.context)
        product = next(
            (
                candidate
                for candidate in products
                if request.id in {str(candidate.id), candidate.slug}
                or any(
                    request.id in {str(variant.id), variant.sku} for variant in candidate.variants
                )
            ),
            None,
        )
        if product is None:
            return UcpErrorResponse(
                ucp=self._response_metadata(UCP_CATALOG_LOOKUP, status="error"),
                messages=[
                    UcpMessage(
                        type="error",
                        code="not_found",
                        content=f"Product not found: {request.id}",
                        severity="unrecoverable",
                    )
                ],
            )

        variants = list(product.variants)
        selected: list[UcpSelectedOption] = []
        messages: list[UcpMessage] = []
        direct_variant = next(
            (variant for variant in variants if request.id in {str(variant.id), variant.sku}),
            None,
        )
        if direct_variant:
            variants = [direct_variant]
            selected = [UcpSelectedOption(name="Size", label=self._variant_label(direct_variant))]
        else:
            option_values = {
                "size": {self._variant_label(variant).casefold() for variant in product.variants}
            }
            option_values.update(
                {
                    group.name.casefold(): {option.name.casefold() for option in group.options}
                    for group in product.modifier_groups
                }
            )
            for item in request.selected:
                allowed = option_values.get(item.name.casefold())
                if allowed is not None and item.label.casefold() in allowed:
                    selected.append(item)
                else:
                    messages.append(
                        UcpMessage(
                            type="warning",
                            code="invalid_selection",
                            content=f"Unsupported option selection: {item.name}={item.label}",
                        )
                    )

            size_selection = next(
                (item.label for item in selected if item.name.casefold() == "size"), None
            )
            if size_selection:
                variants = [
                    variant
                    for variant in variants
                    if size_selection.casefold()
                    in {variant.name.casefold(), self._variant_label(variant).casefold()}
                ]

        if request.filters:
            variants = self._filter_variants(variants, request.filters)
        if not variants:
            return UcpErrorResponse(
                ucp=self._response_metadata(UCP_CATALOG_LOOKUP, status="error"),
                messages=[
                    UcpMessage(
                        type="error",
                        code="not_found",
                        content=f"No purchasable variant matched: {request.id}",
                        severity="unrecoverable",
                    )
                ],
            )

        projected = self._project_product(product, variants, detail=True)
        projected.selected = selected or None
        return UcpGetProductResponse(
            ucp=self._response_metadata(UCP_CATALOG_LOOKUP),
            product=projected,
            messages=messages,
        )

    def _catalog_products(self, context: object | None) -> list[CatalogProductOut]:
        postal_code = getattr(context, "postal_code", None)
        fulfillment_type = (
            getattr(context, "fulfillment_type", None) or FulfillmentType.LOCAL_DELIVERY
        )
        try:
            result = self.catalog.search(
                merchant_slug="ember-and-leaf",
                postal_code=postal_code,
                fulfillment_type=fulfillment_type,
            )
        except NotFoundError as error:
            if error.code in {"address_not_serviceable", "pickup_location_not_found"}:
                return []
            raise
        return list(result.products)

    def _search_products(
        self, products: list[CatalogProductOut], query: str | None
    ) -> list[CatalogProductOut]:
        if not query or not query.strip():
            return products
        terms = [
            term
            for term in re.findall(r"[\w-]+", query.casefold())
            if len(term) > 1 and term not in _SEARCH_STOP_WORDS
        ]
        if not terms:
            return products

        scored: list[tuple[int, CatalogProductOut]] = []
        for product in products:
            searchable = " ".join(
                [
                    product.name,
                    product.description,
                    product.product_type.value,
                    json.dumps(product.attributes, sort_keys=True),
                ]
            ).casefold()
            score = sum(searchable.count(term) for term in terms)
            if score:
                scored.append((score, product))
        return [product for _, product in sorted(scored, key=lambda item: -item[0])]

    def _filter_products(
        self, products: list[CatalogProductOut], filters: UcpSearchFilters | None
    ) -> list[CatalogProductOut]:
        if filters is None:
            return products
        categories = {category.casefold() for category in filters.categories}
        output: list[CatalogProductOut] = []
        for product in products:
            if categories and product.product_type.value.casefold() not in categories:
                continue
            variants = self._filter_variants(product.variants, filters)
            if variants:
                output.append(product.model_copy(update={"variants": variants}))
        return output

    def _filter_projected_products(
        self, products: list[UcpProduct], filters: UcpSearchFilters | None
    ) -> list[UcpProduct]:
        if filters is None:
            return products
        categories = {category.casefold() for category in filters.categories}
        output: list[UcpProduct] = []
        for product in products:
            product_categories = {category.value.casefold() for category in product.categories}
            if categories and categories.isdisjoint(product_categories):
                continue
            variants = [
                variant
                for variant in product.variants
                if self._price_matches(variant.price.amount, filters)
            ]
            if variants:
                amounts = [variant.price.amount for variant in variants]
                currency = variants[0].price.currency
                output.append(
                    product.model_copy(
                        update={
                            "variants": variants,
                            "price_range": UcpPriceRange(
                                min=UcpPrice(amount=min(amounts), currency=currency),
                                max=UcpPrice(amount=max(amounts), currency=currency),
                            ),
                        }
                    )
                )
        return output

    def _filter_variants(
        self, variants: Iterable[ProductVariantOut], filters: UcpSearchFilters
    ) -> list[ProductVariantOut]:
        return [
            variant for variant in variants if self._price_matches(variant.price_minor, filters)
        ]

    @staticmethod
    def _price_matches(amount: int, filters: UcpSearchFilters) -> bool:
        if filters.price is None:
            return True
        if filters.price.min is not None and amount < filters.price.min:
            return False
        return filters.price.max is None or amount <= filters.price.max

    def _project_product(
        self,
        product: CatalogProductOut,
        variants: list[ProductVariantOut] | None = None,
        correlations: dict[str, list[UcpInputCorrelation]] | None = None,
        *,
        detail: bool = False,
    ) -> UcpProduct:
        selected_variants = variants or list(product.variants)
        product_url = (
            f"{self.settings.storefront_public_base_url.rstrip('/')}/shop?product={product.slug}"
        )
        prices = [variant.price_minor for variant in selected_variants]
        currency = selected_variants[0].currency
        options = [
            UcpProductOption(
                name="Size",
                values=[
                    UcpOptionValue(
                        label=self._variant_label(variant),
                        available=True if detail else None,
                        exists=True if detail else None,
                    )
                    for variant in product.variants
                ],
            )
        ]
        options.extend(
            UcpProductOption(
                name=group.name,
                values=[
                    UcpOptionValue(
                        label=option.name,
                        available=True if detail else None,
                        exists=True if detail else None,
                    )
                    for option in group.options
                ],
            )
            for group in product.modifier_groups
            if group.options
        )
        tags = self._tags(product.attributes)
        return UcpProduct(
            id=str(product.id),
            handle=product.slug,
            title=product.name,
            description=UcpDescription(plain=product.description),
            url=product_url,
            categories=[UcpCategory(value=product.product_type.value, taxonomy="merchant")],
            price_range=UcpPriceRange(
                min=UcpPrice(amount=min(prices), currency=currency),
                max=UcpPrice(amount=max(prices), currency=currency),
            ),
            media=[
                UcpMedia(type="image", url=url, alt_text=product.name) for url in product.image_urls
            ],
            options=options,
            variants=[
                self._project_variant(
                    product,
                    variant,
                    product_url,
                    tags,
                    (correlations or {}).get(str(variant.id)),
                )
                for variant in selected_variants
            ],
            tags=tags,
            metadata={
                "merchant_slug": "ember-and-leaf",
                "product_type": product.product_type.value,
                **product.attributes,
            },
        )

    def _project_variant(
        self,
        product: CatalogProductOut,
        variant: ProductVariantOut,
        product_url: str,
        tags: list[str],
        inputs: list[UcpInputCorrelation] | None,
    ) -> UcpVariant:
        return UcpVariant(
            id=str(variant.id),
            sku=variant.sku,
            title=variant.name,
            description=UcpDescription(plain=f"{product.name} — {variant.name}"),
            url=f"{product_url}&variant={variant.id}",
            price=UcpPrice(amount=variant.price_minor, currency=variant.currency),
            availability=UcpAvailability(
                available=(variant.available_quantity is None or variant.available_quantity > 0)
            ),
            options=[UcpSelectedOption(name="Size", label=self._variant_label(variant))],
            tags=tags,
            metadata={
                "preparation_minutes": variant.preparation_minutes,
                **variant.attributes,
            },
            seller=UcpSeller(name="Ember & Leaf"),
            inputs=inputs,
        )

    @staticmethod
    def _tags(attributes: dict[str, object]) -> list[str]:
        tags: list[str] = []
        for key in ("flavor_tags", "dietary", "occasion", "brew_methods"):
            value = attributes.get(key)
            if isinstance(value, list):
                tags.extend(str(item) for item in value)
        return list(dict.fromkeys(tags))

    @staticmethod
    def _variant_label(variant: ProductVariantOut) -> str:
        return variant.size_label or variant.name

    @staticmethod
    def _response_metadata(capability: str, *, status: str | None = None) -> UcpMetadata:
        return UcpMetadata(
            status=status,
            capabilities={capability: [UcpEntity()]},
        )

    @staticmethod
    def _encode_cursor(offset: int) -> str:
        return base64.urlsafe_b64encode(f"offset:{offset}".encode()).decode().rstrip("=")

    @staticmethod
    def _decode_cursor(cursor: str | None) -> int:
        if cursor is None:
            return 0
        try:
            padded = cursor + "=" * (-len(cursor) % 4)
            decoded = base64.urlsafe_b64decode(padded.encode()).decode()
            prefix, offset = decoded.split(":", 1)
            if prefix != "offset":
                raise ValueError
            parsed = int(offset)
            if parsed < 0:
                raise ValueError
            return parsed
        except (ValueError, UnicodeDecodeError) as error:
            raise DomainError("invalid_cursor", "The UCP pagination cursor is invalid") from error
