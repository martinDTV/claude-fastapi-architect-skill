from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

engine = create_async_engine("postgresql+asyncpg://crm@localhost/crm")
Sessions = async_sessionmaker(engine, expire_on_commit=False)


class Contact:
    def __init__(self, email: str) -> None:
        self.email = email

    def domain(self) -> str:
        return self.email.split("@", 1)[1]


async def load_contact(session: AsyncSession, email: str) -> Contact:
    return Contact(email)


async def render(session: AsyncSession, contact: Contact) -> str:
    return contact.email
