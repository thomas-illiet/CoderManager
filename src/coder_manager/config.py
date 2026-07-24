"""Application configuration."""

from functools import lru_cache

from pydantic import Field, SecretStr
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
    job_retry_interval_seconds: int = Field(default=60, ge=1)
    job_stale_after_seconds: int = Field(default=300, ge=1)
    template_sync_poll_interval_seconds: float = Field(default=2.0, ge=0.1)
    template_sync_timeout_seconds: int = Field(default=1800, ge=1)
    instance_domain: str = Field(
        default="code-studio",
        pattern=r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$",
    )
    crypto_key: SecretStr | None = None
    argocd_url: str | None = None
    argocd_token: SecretStr | None = None
    argocd_skip_ssl_verify: bool = False
    argocd_project: str | None = None
    argocd_application_prefix: str = "coder"
    argocd_repository_url: str | None = None
    argocd_repository_path: str | None = None
    argocd_target_revision: str | None = None
    argocd_development_destination_name: str | None = None
    argocd_staging_destination_name: str | None = None
    argocd_production_destination_name: str | None = None
    cyberark_development_app_id: str | None = None
    cyberark_development_cert_name: str | None = None
    cyberark_development_key_name: str | None = None
    cyberark_development_safe: str | None = None
    cyberark_staging_app_id: str | None = None
    cyberark_staging_cert_name: str | None = None
    cyberark_staging_key_name: str | None = None
    cyberark_staging_safe: str | None = None
    cyberark_production_app_id: str | None = None
    cyberark_production_cert_name: str | None = None
    cyberark_production_key_name: str | None = None
    cyberark_production_safe: str | None = None
    default_admins: str = ""


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings instance."""

    return Settings()
