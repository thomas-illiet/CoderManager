"""Application configuration."""

from functools import lru_cache

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings loaded from environment variables or a local .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="CODER_MANAGER_",
        extra="ignore",
    )

    app_name: str = "Coder Manager"
    database_url: str = (
        "postgresql+asyncpg://coder_manager:coder_manager@localhost:5432/coder_manager"
    )
    celery_broker_url: str = "redis://localhost:6379/0"
    celery_result_backend: str = "redis://localhost:6379/1"
    global_whitelist: bool = False
    crypto_key: SecretStr | None = None
    argocd_url: str | None = None
    argocd_token: SecretStr | None = None
    argocd_skip_ssl_verify: bool = False
    argocd_project: str | None = None
    argocd_application_prefix: str = "coder"
    argocd_repository_url: str | None = None
    argocd_repository_path: str | None = None
    argocd_target_revision: str | None = None
    argocd_destination_server: str | None = None
    default_admins: str = ""


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings instance."""

    return Settings()
