from dataclasses import dataclass

from sqlalchemy import Column                         # infrastructure inside the domain


@dataclass(frozen=True)
class Money:
    amount: int
