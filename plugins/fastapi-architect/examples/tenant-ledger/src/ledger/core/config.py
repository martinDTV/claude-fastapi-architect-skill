from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration. Two database URLs on purpose: the application connects as a
    role that is neither owner nor BYPASSRLS, so row-level security applies to it; migrations
    connect as the owner."""

    model_config = SettingsConfigDict(env_prefix="LEDGER_", env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://ledger_app@localhost:5432/ledger"
    migrations_database_url: str = "postgresql+asyncpg://ledger_owner:ledger@localhost:5432/ledger"
    app_role: str = "ledger_app"
    echo_sql: bool = False


@lru_cache
def get_settings() -> Settings:
    return Settings()
