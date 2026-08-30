import re
import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.domain.enums import UserRole

EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


def normalize_email(value: str) -> str:
    normalized = value.strip().casefold()
    if len(normalized) > 320 or not EMAIL_PATTERN.fullmatch(normalized):
        raise ValueError("Enter a valid email address.")
    return normalized


class RegisterRequest(BaseModel):
    email: str
    password: str = Field(min_length=12, max_length=128)
    full_name: str = Field(min_length=2, max_length=160)
    phone: str | None = Field(default=None, min_length=7, max_length=32)

    @field_validator("email")
    @classmethod
    def validate_email(cls, value: str) -> str:
        return normalize_email(value)

    @field_validator("full_name", "phone")
    @classmethod
    def strip_text(cls, value: str | None) -> str | None:
        return value.strip() if value else value


class LoginRequest(BaseModel):
    email: str
    password: str = Field(min_length=1, max_length=128)

    @field_validator("email")
    @classmethod
    def validate_email(cls, value: str) -> str:
        return normalize_email(value)


class ProfileUpdateRequest(BaseModel):
    full_name: str | None = Field(default=None, min_length=2, max_length=160)
    phone: str | None = Field(default=None, min_length=7, max_length=32)

    @model_validator(mode="after")
    def require_update(self) -> "ProfileUpdateRequest":
        if self.full_name is None and self.phone is None:
            raise ValueError("Provide at least one profile field.")
        return self


class UserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: str
    full_name: str
    phone: str | None
    role: UserRole
    merchant_id: uuid.UUID | None


class AuthResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_at: datetime
    user: UserResponse


class AddressCreateRequest(BaseModel):
    label: str = Field(min_length=1, max_length=80)
    recipient_name: str = Field(min_length=2, max_length=160)
    phone: str = Field(min_length=7, max_length=32)
    line_one: str = Field(min_length=3, max_length=200)
    line_two: str | None = Field(default=None, max_length=200)
    landmark: str | None = Field(default=None, max_length=160)
    city: str = Field(min_length=2, max_length=120)
    region: str = Field(min_length=2, max_length=120)
    postal_code: str = Field(pattern=r"^[0-9]{6}$")
    country_code: str = Field(default="IN", pattern=r"^[A-Z]{2}$")
    is_default: bool = False

    @field_validator(
        "label",
        "recipient_name",
        "phone",
        "line_one",
        "line_two",
        "landmark",
        "city",
        "region",
    )
    @classmethod
    def strip_fields(cls, value: str | None) -> str | None:
        return value.strip() if value else value


class AddressUpdateRequest(BaseModel):
    label: str | None = Field(default=None, min_length=1, max_length=80)
    recipient_name: str | None = Field(default=None, min_length=2, max_length=160)
    phone: str | None = Field(default=None, min_length=7, max_length=32)
    line_one: str | None = Field(default=None, min_length=3, max_length=200)
    line_two: str | None = Field(default=None, max_length=200)
    landmark: str | None = Field(default=None, max_length=160)
    city: str | None = Field(default=None, min_length=2, max_length=120)
    region: str | None = Field(default=None, min_length=2, max_length=120)
    postal_code: str | None = Field(default=None, pattern=r"^[0-9]{6}$")
    country_code: str | None = Field(default=None, pattern=r"^[A-Z]{2}$")
    is_default: bool | None = None


class AddressResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    label: str
    recipient_name: str
    phone: str
    line_one: str
    line_two: str | None
    landmark: str | None
    city: str
    region: str
    postal_code: str
    country_code: str
    is_default: bool


class AddressListResponse(BaseModel):
    addresses: list[AddressResponse]
