from shop.domain.cart import Cart
from shop.domain.ports import CartRepository


def checkout(repo: CartRepository, cart_id: int) -> Cart:
    return repo.get(cart_id)
