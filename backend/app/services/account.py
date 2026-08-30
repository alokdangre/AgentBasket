import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import ConflictError, DomainError, NotFoundError
from app.core.security import (
    create_session_token,
    hash_password,
    hash_session_token,
    verify_password,
)
from app.db.models import AuthSession, CustomerAddress, UserAccount
from app.domain.enums import UserRole
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


def utc_now() -> datetime:
    return datetime.now(UTC)


class AccountService:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.settings = get_settings()

    def register(self, payload: RegisterRequest) -> AuthResponse:
        with self.db.begin():
            if self.db.scalar(select(UserAccount.id).where(UserAccount.email == payload.email)):
                raise ConflictError(
                    "email_already_registered", "An account already uses this email."
                )
            user = UserAccount(
                email=payload.email,
                password_hash=hash_password(payload.password),
                full_name=payload.full_name,
                phone=payload.phone,
                role=UserRole.CUSTOMER,
            )
            self.db.add(user)
            self.db.flush()
            return self._new_session(user)

    def login(self, payload: LoginRequest) -> AuthResponse:
        with self.db.begin():
            user = self.db.scalar(select(UserAccount).where(UserAccount.email == payload.email))
            if (
                user is None
                or not user.active
                or not verify_password(payload.password, user.password_hash)
            ):
                raise DomainError("invalid_credentials", "Email or password is incorrect.", 401)
            user.last_login_at = utc_now()
            return self._new_session(user)

    def logout(self, token: str) -> None:
        with self.db.begin():
            session = self.db.scalar(
                select(AuthSession).where(AuthSession.token_sha256 == hash_session_token(token))
            )
            if session is not None and session.revoked_at is None:
                session.revoked_at = utc_now()

    def update_profile(self, user: UserAccount, payload: ProfileUpdateRequest) -> UserResponse:
        with self.db.begin():
            persisted = self.db.get(UserAccount, user.id)
            if persisted is None:
                raise NotFoundError("account_not_found", "Account was not found.")
            updates = payload.model_dump(exclude_unset=True)
            for field, value in updates.items():
                setattr(persisted, field, value.strip() if isinstance(value, str) else value)
            self.db.flush()
            return UserResponse.model_validate(persisted)

    def list_addresses(self, user: UserAccount) -> AddressListResponse:
        addresses = list(
            self.db.scalars(
                select(CustomerAddress)
                .where(CustomerAddress.user_id == user.id)
                .order_by(CustomerAddress.is_default.desc(), CustomerAddress.created_at)
            )
        )
        return AddressListResponse(addresses=[AddressResponse.model_validate(a) for a in addresses])

    def create_address(self, user: UserAccount, payload: AddressCreateRequest) -> AddressResponse:
        with self.db.begin():
            has_address = self.db.scalar(
                select(CustomerAddress.id).where(CustomerAddress.user_id == user.id).limit(1)
            )
            make_default = payload.is_default or has_address is None
            if make_default:
                self._clear_default(user.id)
            address = CustomerAddress(
                user_id=user.id,
                **payload.model_dump(exclude={"is_default"}),
                is_default=make_default,
            )
            self.db.add(address)
            self.db.flush()
            return AddressResponse.model_validate(address)

    def update_address(
        self, user: UserAccount, address_id: uuid.UUID, payload: AddressUpdateRequest
    ) -> AddressResponse:
        with self.db.begin():
            address = self._address_for_user(user.id, address_id)
            updates = payload.model_dump(exclude_unset=True)
            if updates.get("is_default"):
                self._clear_default(user.id)
            for field, value in updates.items():
                setattr(address, field, value.strip() if isinstance(value, str) else value)
            self.db.flush()
            return AddressResponse.model_validate(address)

    def delete_address(self, user: UserAccount, address_id: uuid.UUID) -> None:
        with self.db.begin():
            address = self._address_for_user(user.id, address_id)
            was_default = address.is_default
            self.db.delete(address)
            self.db.flush()
            if was_default:
                next_address = self.db.scalar(
                    select(CustomerAddress)
                    .where(CustomerAddress.user_id == user.id)
                    .order_by(CustomerAddress.created_at)
                    .limit(1)
                )
                if next_address is not None:
                    next_address.is_default = True

    def _new_session(self, user: UserAccount) -> AuthResponse:
        raw_token = create_session_token()
        expires_at = utc_now() + timedelta(days=self.settings.auth_session_ttl_days)
        self.db.add(
            AuthSession(
                user_id=user.id,
                token_sha256=hash_session_token(raw_token),
                expires_at=expires_at,
            )
        )
        self.db.flush()
        return AuthResponse(
            access_token=raw_token,
            expires_at=expires_at,
            user=UserResponse.model_validate(user),
        )

    def _address_for_user(self, user_id: uuid.UUID, address_id: uuid.UUID) -> CustomerAddress:
        address = self.db.scalar(
            select(CustomerAddress).where(
                CustomerAddress.id == address_id, CustomerAddress.user_id == user_id
            )
        )
        if address is None:
            raise NotFoundError("address_not_found", "Address was not found.")
        return address

    def _clear_default(self, user_id: uuid.UUID) -> None:
        self.db.execute(
            update(CustomerAddress)
            .where(CustomerAddress.user_id == user_id, CustomerAddress.is_default.is_(True))
            .values(is_default=False)
        )
