from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, EmailStr, Field

Role = Literal["super_admin", "operator"]


class LoginRequest(BaseModel):
    email: EmailStr


class DraftEdit(BaseModel):
    body: str = Field(..., min_length=1)


class SettingsForm(BaseModel):
    provider: str = ""
    base_url: str = ""
    api_key_encrypted: str = ""
    model_general: str = ""
    model_classifier: str = ""
    resend_from: EmailStr | None = None
    draft_recipient: EmailStr | None = None


class UserCreate(BaseModel):
    email: EmailStr
    role: Role
    name: str = ""


class AuditLogFilter(BaseModel):
    user_id: int | None = None
    action: str | None = None
    from_date: date | None = None
    to_date: date | None = None
