from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.security import RequestContext


async def close(session: AsyncSession, account_id: UUID) -> None:
    """Receives only the resource id: whoever calls it decides who may close what."""
    await session.execute(text("update accounts set closed = true where id = :id"), {"id": account_id})
    await session.commit()


async def freeze(session: AsyncSession, ctx: RequestContext, account_id: UUID) -> None:
    """Receives the principal with the resource: the relation can be checked here."""
    if ctx.user.role not in {"owner", "admin"}:
        raise PermissionError
    await session.execute(text("update accounts set frozen = true where id = :id"), {"id": account_id})
    await session.commit()


async def archive(session: AsyncSession, account_id: UUID) -> None:
    await session.execute(text("update accounts set archived = true where id = :id"), {"id": account_id})
    await session.commit()


async def tag(session: AsyncSession, account_id: UUID, label: str) -> None:
    await session.execute(text("insert into tags (account_id, label) values (:id, :l)"), {"id": account_id, "l": label})
    await session.commit()


async def compute_summary(session: AsyncSession) -> dict:
    rows = await session.execute(text("select count(*) from accounts"))
    return {"count": rows.scalar_one()}
