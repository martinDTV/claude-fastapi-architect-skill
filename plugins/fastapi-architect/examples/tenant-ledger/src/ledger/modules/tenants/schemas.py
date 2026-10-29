from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field


class TenantCreate(BaseModel):
    slug: str = Field(min_length=2, max_length=40, pattern=r"^[a-z0-9-]+$")
    name: str = Field(min_length=1, max_length=120)


class TenantRead(BaseModel):
    id: UUID
    slug: str
    name: str
    role: str


class MemberAdd(BaseModel):
    username: str = Field(min_length=1, max_length=40)
    role: Literal["owner", "member"] = "member"


class MemberRead(BaseModel):
    user_id: UUID
    username: str
    role: str
