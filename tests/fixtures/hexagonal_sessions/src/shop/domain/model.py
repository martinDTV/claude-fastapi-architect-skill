from dataclasses import dataclass


@dataclass
class Order:
    id: int
    total: int
