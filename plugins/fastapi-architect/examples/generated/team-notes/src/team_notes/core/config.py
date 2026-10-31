"""Settings: environment variables prefixed TEAM_NOTES_ (and an optional .env file); no secret has a default."""

from functools import lru_cache
from typing import Annotated

from fastapi import Depends, Request
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="TEAM_NOTES_", env_file=".env", extra="ignore")

    app_name: str = "team-notes"
    environment: str = "local"
    # OIDC provider of the contract; the accepted algorithms are fixed in core/security.py, not configurable
    oidc_issuer: str = "https://login.example.com/"
    oidc_audience: str = "team-notes-api"
    oidc_jwks_url: str = "https://login.example.com/.well-known/jwks.json"
    oidc_jwks_json: str = ""  # static JWKS document for tests only (refused unless environment == "test")
    oidc_jwks_cache_s: int = 300
    oidc_leeway_s: int = 30


@lru_cache
def get_settings() -> Settings:
    """Settings read from the environment, used when the application is created without explicit settings."""
    return Settings()


def current_settings(request: Request) -> Settings:
    """The settings the running application was created with (tests pass their own to create_app)."""
    return request.app.state.settings


SettingsDep = Annotated[Settings, Depends(current_settings)]
