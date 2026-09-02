from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from app.domain.enums import Gender, Role


class RegisterIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    display_name: str = Field(min_length=2, max_length=80)
    role: Role = Role.PARTICIPANT
    #: Optional, and used for one thing: picking a discussion name that fits. Leaving it
    #: unset is fine — those seats draw from the whole pool.
    gender: Gender = Gender.UNSPECIFIED


class LoginIn(BaseModel):
    email: EmailStr
    password: str


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int


class InterestIn(BaseModel):
    topic_id: uuid.UUID
    proficiency: int = Field(ge=1, le=5, default=3)


class InterestsIn(BaseModel):
    interests: list[InterestIn] = Field(default_factory=list, max_length=50)


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: str
    display_name: str
    role: Role
    is_active: bool
    created_at: datetime


class SessionUser(BaseModel):
    """The authenticated caller, as every dependency and service sees them."""

    id: uuid.UUID
    email: str
    display_name: str
    role: Role

    @property
    def is_super_user(self) -> bool:
        return self.role is Role.SUPER_USER
