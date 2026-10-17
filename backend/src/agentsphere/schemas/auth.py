"""Public contracts never include password hashes or session token digests."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field, SecretStr, field_validator


class Login(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: EmailStr = Field(max_length=254)
    password: SecretStr = Field(min_length=1, max_length=128)

    @field_validator("email", mode="after")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        return value.casefold()


class Registration(Login):
    password: SecretStr = Field(min_length=15, max_length=128)
    display_name: str = Field(min_length=1, max_length=100)

    @field_validator("display_name")
    @classmethod
    def clean_name(cls, value):
        value = value.strip()
        if not value or not value.isprintable():
            raise ValueError("Use a nonempty printable name.")
        return value


class UserRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    email: str
    display_name: str
    created_at: datetime


class WorkspaceCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=100)


class WorkspaceRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    name: str
    owner_user_id: UUID
    created_at: datetime
    role: Literal["owner", "editor", "viewer"]


class MembershipChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Literal["editor", "viewer"]


class MembershipCreate(MembershipChange):
    user_id: UUID


class MembershipRead(MembershipCreate):
    model_config = ConfigDict(from_attributes=True)
    workspace_id: UUID


class RegistrationRead(BaseModel):
    user: UserRead
    workspace: WorkspaceRead


class LoginRead(BaseModel):
    user: UserRead
    csrf_token: str
    expires_at: datetime


class CsrfRead(BaseModel):
    csrf_token: str
