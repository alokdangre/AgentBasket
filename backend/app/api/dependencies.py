from datetime import UTC, datetime

from fastapi import Depends, Header
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.core.database import get_db
from app.core.errors import DomainError
from app.core.security import hash_session_token
from app.db.models import AuthSession, UserAccount
from app.domain.enums import UserRole
from app.services.trusted_surface import TrustedSurfaceService


def _bearer_token(authorization: str | None) -> str:
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise DomainError("authentication_required", "Sign in to continue.", 401)
    return token


def get_current_user(
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> UserAccount:
    token_hash = hash_session_token(_bearer_token(authorization))
    session = db.scalar(
        select(AuthSession)
        .options(joinedload(AuthSession.user))
        .where(AuthSession.token_sha256 == token_hash)
    )
    now = datetime.now(UTC)
    expires_at = session.expires_at if session is not None else None
    if expires_at is not None and expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    if (
        session is None
        or session.revoked_at is not None
        or expires_at is None
        or expires_at <= now
        or not session.user.active
    ):
        raise DomainError("invalid_session", "Your session is invalid or expired.", 401)
    user = session.user
    db.expunge(user)
    db.rollback()
    return user


def get_merchant_user(user: UserAccount = Depends(get_current_user)) -> UserAccount:
    if user.role not in {UserRole.MERCHANT_ADMIN, UserRole.MERCHANT_STAFF} or not user.merchant_id:
        raise DomainError("merchant_access_required", "Merchant access is required.", 403)
    return user


def get_merchant_admin(user: UserAccount = Depends(get_merchant_user)) -> UserAccount:
    if user.role is not UserRole.MERCHANT_ADMIN:
        raise DomainError(
            "merchant_admin_required", "Merchant administrator access is required.", 403
        )
    return user


def get_trusted_surface(db: Session = Depends(get_db)) -> TrustedSurfaceService:
    return TrustedSurfaceService(db)
