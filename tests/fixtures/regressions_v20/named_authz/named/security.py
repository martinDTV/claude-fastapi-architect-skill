"""Dependencies that look like authorization and dependencies that are authorization.

The first group carries a policy-like name and no decision about the principal; the second group shows the
decision in its code (a condition over a role or scope attribute, a predicate, a Security(scopes=...)).
"""

from dataclasses import dataclass

from fastapi import Depends, Header, HTTPException, Request


@dataclass
class User:
    sub: str
    role: str
    scopes: tuple


async def get_current_user(x_user: str = Header()) -> User:
    if not x_user:
        raise HTTPException(401)
    return User(sub=x_user, role="member", scopes=("items:read",))


# --- named like a policy, no policy --------------------------------------------------------------------------- #


async def require_nothing(user: User = Depends(get_current_user)) -> User:
    """Case 1: no check at all; the name promises one."""
    return user


async def require_admin_none(user: User = Depends(get_current_user)) -> None:
    """Case 2: returns None and checks nothing."""
    return None


async def require_any_token(authorization: str = Header(None)) -> User:
    """Case 3: accepts any token, or none, and still yields a principal."""
    return User(sub=authorization or "anonymous", role="member", scopes=())


async def require_authenticated(user: User = Depends(get_current_user)) -> User:
    """Case 4: authentication only (the 401 belongs to get_current_user); no authorization."""
    if user is None:
        raise HTTPException(401)
    return user


async def require_editor(user: User = Depends(get_current_user)) -> User:
    """Case 5: hands the principal on without validating any permission."""
    return User(sub=user.sub, role=user.role, scopes=user.scopes)


async def require_owner(request: Request) -> None:
    """Case 6: a reassuring name that never looks at the principal."""
    request.state.checked = True


async def get_ctx(user: User = Depends(require_admin_none)) -> dict:
    """Case 7: a neutral name over a nested dependency that only looks like a policy."""
    return {"user": user}


# --- policies that show their decision ------------------------------------------------------------------------ #


async def require_admin(user: User = Depends(get_current_user)) -> User:
    if user.role != "admin":
        raise HTTPException(403)
    return user


def require_scope(scope: str):
    async def dependency(user: User = Depends(get_current_user)) -> User:
        if scope not in user.scopes:
            raise HTTPException(403)
        return user

    return dependency


class RoleChecker:
    def __init__(self, role: str) -> None:
        self.role = role

    async def __call__(self, user: User = Depends(get_current_user)) -> User:
        if user.role != self.role:
            raise HTTPException(403)
        return user


async def get_admin_ctx(admin: User = Depends(require_admin)) -> dict:
    """A neutral name over a nested dependency that decides."""
    return {"user": admin}


async def guard(user: User = Depends(get_current_user)) -> User:
    """A neutral-looking predicate: the decision is a call, not a name of the dependency."""
    if not user_is_allowed(user):
        raise HTTPException(403)
    return user


def user_is_allowed(user: User) -> bool:
    return bool(set(user.scopes) & {"items:write"})


def require_role(role: str):
    """A decorator named like a policy that decorates nothing: the handler runs unchanged."""

    def decorate(handler):
        return handler

    return decorate


def authorize_user(user: User, item_id) -> None:
    """A policy call that decides: the resource must belong to the caller's scope."""
    if "items:write" not in user.scopes:
        raise HTTPException(403)


def check_access(user: User) -> bool:
    """Named like a policy call, decides nothing."""
    return True


async def gate(user: User = Depends(get_current_user)) -> User:
    """Delegates the whole decision: nothing in this body raises or reads a privilege; the callee does."""
    return gate_outcome(user)


def gate_outcome(user: User) -> User:
    if not user_is_allowed(user):
        raise HTTPException(403)
    return user
