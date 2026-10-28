from typing import Protocol


class CartRepository(Protocol):
    def get(self, cart_id: int): ...
