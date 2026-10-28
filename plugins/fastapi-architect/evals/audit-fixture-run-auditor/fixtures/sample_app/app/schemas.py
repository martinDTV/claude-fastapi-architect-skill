from pydantic import BaseModel


class ItemRead(BaseModel):
    id: int
    name: str
