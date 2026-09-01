import uuid

from fastapi import APIRouter, Depends, Header, Response
from sqlalchemy.orm import Session

from app.api.dependencies import get_current_user, get_trusted_surface
from app.core.database import get_db
from app.core.errors import DomainError
from app.db.models import UserAccount
from app.schemas.account import (
    AddressCreateRequest,
    AddressListResponse,
    AddressResponse,
    AddressUpdateRequest,
    AuthResponse,
    LoginRequest,
    ProfileUpdateRequest,
    RegisterRequest,
    UserResponse,
)
from app.schemas.trusted_surface import (
    PasskeyListOut,
    PasskeyOut,
    PasskeyRegistrationOptionsOut,
    PasskeyRegistrationVerify,
    PaymentInstrumentListOut,
)
from app.services.account import AccountService
from app.services.credentials_provider import CredentialsProviderService
from app.services.trusted_surface import TrustedSurfaceService

router = APIRouter(tags=["accounts"])


@router.post("/auth/register", response_model=AuthResponse, status_code=201)
def register(payload: RegisterRequest, db: Session = Depends(get_db)) -> AuthResponse:
    return AccountService(db).register(payload)


@router.post("/auth/login", response_model=AuthResponse)
def login(payload: LoginRequest, db: Session = Depends(get_db)) -> AuthResponse:
    return AccountService(db).login(payload)


@router.post("/auth/logout", status_code=204)
def logout(
    authorization: str | None = Header(default=None),
    _: UserAccount = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Response:
    _, _, token = (authorization or "").partition(" ")
    if not token:
        raise DomainError("authentication_required", "Sign in to continue.", 401)
    AccountService(db).logout(token)
    return Response(status_code=204)


@router.get("/me", response_model=UserResponse)
def get_profile(user: UserAccount = Depends(get_current_user)) -> UserResponse:
    return UserResponse.model_validate(user)


@router.patch("/me", response_model=UserResponse)
def update_profile(
    payload: ProfileUpdateRequest,
    user: UserAccount = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> UserResponse:
    return AccountService(db).update_profile(user, payload)


@router.get("/me/addresses", response_model=AddressListResponse)
def list_addresses(
    user: UserAccount = Depends(get_current_user), db: Session = Depends(get_db)
) -> AddressListResponse:
    return AccountService(db).list_addresses(user)


@router.post("/me/addresses", response_model=AddressResponse, status_code=201)
def create_address(
    payload: AddressCreateRequest,
    user: UserAccount = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> AddressResponse:
    return AccountService(db).create_address(user, payload)


@router.patch("/me/addresses/{address_id}", response_model=AddressResponse)
def update_address(
    address_id: uuid.UUID,
    payload: AddressUpdateRequest,
    user: UserAccount = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> AddressResponse:
    return AccountService(db).update_address(user, address_id, payload)


@router.delete("/me/addresses/{address_id}", status_code=204)
def delete_address(
    address_id: uuid.UUID,
    user: UserAccount = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Response:
    AccountService(db).delete_address(user, address_id)
    return Response(status_code=204)


@router.get("/me/passkeys", response_model=PasskeyListOut)
def list_passkeys(
    user: UserAccount = Depends(get_current_user),
    trusted_surface: TrustedSurfaceService = Depends(get_trusted_surface),
) -> PasskeyListOut:
    return trusted_surface.list_passkeys(user)


@router.post(
    "/me/passkeys/registration/options",
    response_model=PasskeyRegistrationOptionsOut,
    status_code=201,
)
def create_passkey_registration(
    user: UserAccount = Depends(get_current_user),
    trusted_surface: TrustedSurfaceService = Depends(get_trusted_surface),
) -> PasskeyRegistrationOptionsOut:
    return trusted_surface.registration_options(user)


@router.post("/me/passkeys/registration/verify", response_model=PasskeyOut, status_code=201)
def verify_passkey_registration(
    payload: PasskeyRegistrationVerify,
    user: UserAccount = Depends(get_current_user),
    trusted_surface: TrustedSurfaceService = Depends(get_trusted_surface),
) -> PasskeyOut:
    return trusted_surface.verify_registration(user, payload)


@router.get("/credential-provider/payment-instruments", response_model=PaymentInstrumentListOut)
def list_payment_instruments(
    user: UserAccount = Depends(get_current_user), db: Session = Depends(get_db)
) -> PaymentInstrumentListOut:
    return CredentialsProviderService(db).list_instruments(user)
