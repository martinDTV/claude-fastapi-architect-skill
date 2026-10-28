from sqlalchemy import select

from shop.domain.cart import Cart


class SqlCartRepository:
    def get(self, cart_id: int) -> Cart:
        return Cart(items=[])
