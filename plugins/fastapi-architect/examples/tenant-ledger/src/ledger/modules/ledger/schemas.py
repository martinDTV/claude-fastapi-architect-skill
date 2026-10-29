from uuid import UUID

from pydantic import BaseModel, Field


class AccountCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80)


class AccountRename(BaseModel):
    name: str = Field(min_length=1, max_length=80)


class AccountRead(BaseModel):
    id: UUID
    name: str
    balance: int
    version: int


class MovementCreate(BaseModel):
    amount: int = Field(gt=0, description="minor units")


class MovementRead(BaseModel):
    account_id: UUID
    entry_id: UUID
    kind: str
    amount: int
    balance: int
    version: int


class EntryRead(BaseModel):
    id: UUID
    kind: str
    amount: int
