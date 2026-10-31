"""Settings: environment variables prefixed ORDERS_PLATFORM_ (and an optional .env file); no secret has a default."""

from functools import lru_cache
from typing import Annotated
from uuid import UUID

from fastapi import Depends, Request
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ORDERS_PLATFORM_", env_file=".env", extra="ignore")

    app_name: str = "orders-platform"
    environment: str = "local"
    # JSON object in ORDERS_PLATFORM_API_KEYS mapping each key to the one tenant it is bound to (empty: nobody)
    api_keys: dict[str, UUID] = {}
    # credentials come from ORDERS_PLATFORM_DATABASE_URL, never from defaults
    database_url: str = "postgresql+asyncpg://localhost:5432/orders_platform"
    payments_base_url: str = "http://localhost:8080"
    payments_timeout_s: float = 5.0


@lru_cache
def get_settings() -> Settings:
    """Settings read from the environment, used when the application is created without explicit settings."""
    return Settings()


def current_settings(request: Request) -> Settings:
    """The settings the running application was created with (tests pass their own to create_app)."""
    return request.app.state.settings


SettingsDep = Annotated[Settings, Depends(current_settings)]
