"""Settings: environment variables prefixed TENANT_PROJECTS_ (and an optional .env file); no secret has a default."""

from functools import lru_cache
from typing import Annotated
from uuid import UUID

from fastapi import Depends, Request
from pydantic import BaseModel, ConfigDict, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class ApiKeyGrant(BaseModel):
    """One API key: the tenant it belongs to, the subject it acts as (default: a digest of the key), its roles."""

    model_config = ConfigDict(extra="forbid")

    tenant_id: UUID
    subject: str | None = None
    roles: list[str] = []


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="TENANT_PROJECTS_", env_file=".env", extra="ignore")

    app_name: str = "tenant-projects"
    environment: str = "local"
    # JSON object in TENANT_PROJECTS_API_KEYS: key -> grant (tenant_id, subject, roles); a bare tenant id is accepted
    api_keys: dict[str, ApiKeyGrant] = {}
    # TENANT_PROJECTS_DATABASE_URL is the application role's URL (row privileges only, no BYPASSRLS);
    # migrations run with TENANT_PROJECTS_MIGRATIONS_DATABASE_URL (the owner)
    database_url: str = "postgresql+asyncpg://localhost:5432/tenant_projects"

    @field_validator("api_keys", mode="before")
    @classmethod
    def _grants(cls, value: object) -> object:
        """A bare tenant id per key (the registry format before grants) is read as a grant without subject or roles."""
        if isinstance(value, dict):
            return {key: ({"tenant_id": grant} if isinstance(grant, str) else grant) for key, grant in value.items()}
        return value


@lru_cache
def get_settings() -> Settings:
    """Settings read from the environment, used when the application is created without explicit settings."""
    return Settings()


def current_settings(request: Request) -> Settings:
    """The settings the running application was created with (tests pass their own to create_app)."""
    return request.app.state.settings


SettingsDep = Annotated[Settings, Depends(current_settings)]
