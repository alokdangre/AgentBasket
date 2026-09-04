from __future__ import annotations

import hashlib
import json
import re
from typing import Annotated
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.api.dependencies import get_current_user
from app.core.config import Settings, get_settings
from app.core.database import get_db
from app.db.models import UserAccount
from app.protocols.ucp.models import (
    UcpCatalogLookupRequest,
    UcpCatalogLookupResponse,
    UcpCatalogSearchRequest,
    UcpCatalogSearchResponse,
    UcpCheckoutClaimResponse,
    UcpCheckoutCreateRequest,
    UcpCheckoutResponse,
    UcpErrorResponse,
    UcpGetProductRequest,
    UcpGetProductResponse,
)
from app.schemas.checkout import CheckoutFromCartCreate, CheckoutOut
from app.services.ucp import UcpCatalogService
from app.services.ucp_checkout import UcpCheckoutService

router = APIRouter(tags=["ucp"])
handoff_router = APIRouter(prefix="/api/v1/ucp/checkout-handoffs", tags=["ucp-handoff"])

_UCP_AGENT_PROFILE = re.compile(r'(?:^|,)\s*profile="([^"]+)"(?:\s*;[^,]+)?(?:,|$)')


def require_ucp_agent(
    request_id: Annotated[
        str,
        Header(alias="Request-Id", min_length=1, max_length=200),
    ],
    ucp_agent: Annotated[
        str,
        Header(alias="UCP-Agent", min_length=1, max_length=1000),
    ],
) -> str:
    match = _UCP_AGENT_PROFILE.search(ucp_agent)
    if match is None:
        raise HTTPException(
            status_code=400,
            detail='UCP-Agent must contain profile="https://.../.well-known/ucp"',
        )
    profile = urlparse(match.group(1))
    if profile.scheme != "https" or not profile.netloc or profile.path != "/.well-known/ucp":
        raise HTTPException(
            status_code=400,
            detail="UCP-Agent profile must be an HTTPS /.well-known/ucp URL",
        )
    return match.group(1)


def ucp_service(
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> UcpCatalogService:
    return UcpCatalogService(db, settings)


def ucp_checkout_service(
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> UcpCheckoutService:
    return UcpCheckoutService(db, settings)


@router.get("/.well-known/ucp", include_in_schema=False)
def ucp_discovery(
    request: Request,
    settings: Settings = Depends(get_settings),
    db: Session = Depends(get_db),
) -> Response:
    profile = UcpCatalogService(db, settings).discovery_profile()
    content = profile.model_dump(mode="json", by_alias=True, exclude_none=True)
    serialized = json.dumps(content, sort_keys=True, separators=(",", ":"))
    etag = f'"{hashlib.sha256(serialized.encode()).hexdigest()}"'
    headers = {
        "Cache-Control": "public, max-age=300, stale-while-revalidate=60",
        "ETag": etag,
    }
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    return JSONResponse(content=content, headers=headers)


@router.post(
    "/ucp/catalog/search",
    response_model=UcpCatalogSearchResponse,
    response_model_exclude_none=True,
    dependencies=[Depends(require_ucp_agent)],
)
def search_catalog(
    payload: UcpCatalogSearchRequest,
    service: UcpCatalogService = Depends(ucp_service),
) -> UcpCatalogSearchResponse:
    return service.search(payload)


@router.post(
    "/ucp/catalog/lookup",
    response_model=UcpCatalogLookupResponse,
    response_model_exclude_none=True,
    dependencies=[Depends(require_ucp_agent)],
)
def lookup_catalog(
    payload: UcpCatalogLookupRequest,
    service: UcpCatalogService = Depends(ucp_service),
) -> UcpCatalogLookupResponse:
    return service.lookup(payload)


@router.post(
    "/ucp/catalog/product",
    response_model=UcpGetProductResponse | UcpErrorResponse,
    response_model_exclude_none=True,
    dependencies=[Depends(require_ucp_agent)],
)
def get_product(
    payload: UcpGetProductRequest,
    service: UcpCatalogService = Depends(ucp_service),
) -> UcpGetProductResponse | UcpErrorResponse:
    return service.get_product(payload)


@router.post(
    "/ucp/checkout-sessions",
    response_model=UcpCheckoutResponse,
    response_model_exclude_none=True,
    status_code=201,
)
def create_checkout(
    payload: UcpCheckoutCreateRequest,
    idempotency_key: Annotated[
        str,
        Header(alias="Idempotency-Key", min_length=8, max_length=128),
    ],
    agent_profile_url: str = Depends(require_ucp_agent),
    service: UcpCheckoutService = Depends(ucp_checkout_service),
) -> UcpCheckoutResponse:
    return service.create(
        payload,
        agent_profile_url=agent_profile_url,
        idempotency_key=idempotency_key,
    )


@router.get(
    "/ucp/checkout-sessions/{session_id}",
    response_model=UcpCheckoutResponse,
    response_model_exclude_none=True,
)
def get_checkout(
    session_id: str,
    agent_profile_url: str = Depends(require_ucp_agent),
    service: UcpCheckoutService = Depends(ucp_checkout_service),
) -> UcpCheckoutResponse:
    return service.get(_session_uuid(session_id), agent_profile_url=agent_profile_url)


@router.put(
    "/ucp/checkout-sessions/{session_id}",
    response_model=UcpCheckoutResponse,
    response_model_exclude_none=True,
)
def update_checkout(
    session_id: str,
    payload: UcpCheckoutCreateRequest,
    agent_profile_url: str = Depends(require_ucp_agent),
    service: UcpCheckoutService = Depends(ucp_checkout_service),
) -> UcpCheckoutResponse:
    return service.update(
        _session_uuid(session_id),
        payload,
        agent_profile_url=agent_profile_url,
    )


@router.post(
    "/ucp/checkout-sessions/{session_id}/complete",
    response_model=UcpCheckoutResponse,
    response_model_exclude_none=True,
)
def complete_checkout(
    session_id: str,
    agent_profile_url: str = Depends(require_ucp_agent),
    service: UcpCheckoutService = Depends(ucp_checkout_service),
) -> UcpCheckoutResponse:
    return service.complete(_session_uuid(session_id), agent_profile_url=agent_profile_url)


@router.post(
    "/ucp/checkout-sessions/{session_id}/cancel",
    response_model=UcpCheckoutResponse,
    response_model_exclude_none=True,
)
def cancel_checkout(
    session_id: str,
    agent_profile_url: str = Depends(require_ucp_agent),
    service: UcpCheckoutService = Depends(ucp_checkout_service),
) -> UcpCheckoutResponse:
    return service.cancel(_session_uuid(session_id), agent_profile_url=agent_profile_url)


@handoff_router.get(
    "/{session_id}",
    response_model=UcpCheckoutResponse,
    response_model_exclude_none=True,
)
def get_checkout_handoff(
    session_id: str,
    service: UcpCheckoutService = Depends(ucp_checkout_service),
) -> UcpCheckoutResponse:
    return service.get_handoff(_session_uuid(session_id))


@handoff_router.post("/{session_id}/claim", response_model=UcpCheckoutClaimResponse)
def claim_checkout_handoff(
    session_id: str,
    user: UserAccount = Depends(get_current_user),
    service: UcpCheckoutService = Depends(ucp_checkout_service),
) -> UcpCheckoutClaimResponse:
    return service.claim(_session_uuid(session_id), user)


@handoff_router.post("/{session_id}/checkout", response_model=CheckoutOut, status_code=201)
def prepare_checkout_handoff(
    session_id: str,
    payload: CheckoutFromCartCreate,
    idempotency_key: Annotated[
        str,
        Header(alias="Idempotency-Key", min_length=8, max_length=128),
    ],
    user: UserAccount = Depends(get_current_user),
    service: UcpCheckoutService = Depends(ucp_checkout_service),
) -> CheckoutOut:
    return service.prepare_checkout(
        _session_uuid(session_id),
        payload,
        user,
        idempotency_key=idempotency_key,
    )


def _session_uuid(value: str):
    import uuid

    try:
        return uuid.UUID(value)
    except ValueError as error:
        raise HTTPException(status_code=404, detail="UCP checkout was not found") from error
