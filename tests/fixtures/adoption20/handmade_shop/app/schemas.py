"""Pydantic schemas.  NOTE: the odd spacing below is intentional (hand-written style)."""

from pydantic import BaseModel, Field


class ItemIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    price: float = Field(ge=0)   # euros, two decimals by convention


class ItemOut(ItemIn):
    id: int


class UserIn(BaseModel):
    email: str
    nickname: str | None = None


class UserOut(UserIn):
    id: int
