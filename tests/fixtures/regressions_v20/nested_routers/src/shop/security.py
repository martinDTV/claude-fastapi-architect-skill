"""Authentication dependency used at the top of the router tree."""

from fastapi import Header, HTTPException


def require_api_key(x_api_key: str | None = Header(default=None, alias="X-API-Key")) -> None:
    if x_api_key != "secret":
        raise HTTPException(status_code=401)
