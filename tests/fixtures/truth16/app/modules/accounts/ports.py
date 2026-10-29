from typing import Protocol
from uuid import UUID


class AccountPort(Protocol):
    """Two structural implementations exist (AccountRepository, MemoryAccountRepository): ambiguous."""

    async def get(self, account_id: UUID): ...

    async def save(self, account) -> None: ...


class NotifierPort(Protocol):
    """Exactly one implementation exists (notify.email.EmailNotifier): resolvable with inferred evidence."""

    def send(self, to: str, body: str) -> None: ...
