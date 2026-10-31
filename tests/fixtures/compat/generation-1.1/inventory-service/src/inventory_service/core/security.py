"""API-key authentication: a constant-time comparison against the configured key (header X-API-Key)."""

import secrets
from typing import Annotated

from fastapi import Header, HTTPException, status

from inventory_service.core.config import SettingsDep

ApiKeyHeader = Annotated[str | None, Header(alias="X-API-Key")]


def require_api_key(settings: SettingsDep, x_api_key: ApiKeyHeader = None) -> None:
    expected = settings.api_key.get_secret_value()
    if not expected:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="API key is not configured")
    if x_api_key is None or not secrets.compare_digest(x_api_key, expected):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid or missing API key")
