---
name: propose-concurrency-test
tags: [v1.4, positive, diagnosis]
max_turns: 8
timeout_seconds: 420
allowed_tools: [Read, Glob, Grep, Skill]
---

In our FastAPI service this handler decrements stock:

async def reserve(session: AsyncSession, product_id: int, qty: int) -> None:
    product = await session.get(Product, product_id)
    if product.stock < qty:
        raise OutOfStock()
    product.stock -= qty
    await session.commit()

Static analysis says "possible lost update, not verifiable statically". Before we add locking, how do we prove whether the problem is real, and which fix fits?
