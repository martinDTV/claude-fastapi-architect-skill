from shop.domain.model import Order
from shop.domain.ports import UnitOfWork


async def place_order(uow: UnitOfWork, order_id: int, total: int) -> Order:
    async with uow:
        order = Order(id=order_id, total=total)
        await uow.repo.add(order)
        await uow.commit()                 # the port decides what commit means
    return order
