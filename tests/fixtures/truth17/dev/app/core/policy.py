"""An authorization service: the resource-principal relation lives here, not in a dependency."""

from uuid import UUID

from fastapi import HTTPException

from .security import User


class Policy:
    def can(self, user: User, action: str, resource_id: UUID) -> bool:
        return user.role == "owner" or action == "read"


policy = Policy()


def authorize(user: User, action: str, resource_id: UUID) -> None:
    if not policy.can(user, action, resource_id):
        raise HTTPException(403)
