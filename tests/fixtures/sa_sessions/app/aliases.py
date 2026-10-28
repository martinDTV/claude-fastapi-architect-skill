import sqlalchemy.orm as orm
from sqlalchemy.ext.asyncio import AsyncSession as AS

from app.db import SessionLocal as Factory, engine

shared = Factory()                                     # factory imported under an alias
legacy = orm.Session(engine)                           # constructor via module alias


async def typed(db: AS) -> None:
    db.execute("select 1")                             # AsyncSession via alias: missing await
