"""Settings: environment variables prefixed INVENTORY_SERVICE_ (and an optional .env file); no secret has a default."""

from functools import lru_cache
from typing import Annotated

from fastapi import Depends, Request
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="INVENTORY_SERVICE_", env_file=".env", extra="ignore")

    app_name: str = "inventory-service"
    environment: str = "local"
    # no default secret: set INVENTORY_SERVICE_API_KEY (the dependency refuses an empty key)
    api_key: SecretStr = SecretStr("")


@lru_cache
def get_settings() -> Settings:
    """Settings read from the environment, used when the application is created without explicit settings."""
    return Settings()


def current_settings(request: Request) -> Settings:
    """The settings the running application was created with (tests pass their own to create_app)."""
    return request.app.state.settings


SettingsDep = Annotated[Settings, Depends(current_settings)]
