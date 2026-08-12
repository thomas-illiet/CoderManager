"""Application configuration."""

from functools import lru_cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, SecretStr, field_validator
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
    database_schema: str | None = None
    celery_broker_url: str = "redis://localhost:6379/0"
    celery_result_backend: str = "redis://localhost:6379/1"
    metrics_host: str = "0.0.0.0"  # noqa: S104
    metrics_port: int = Field(default=9808, ge=1, le=65535)
    scheduler_timezone: str = "Europe/Paris"
    job_retry_interval_seconds: int = Field(default=60, ge=1)
    job_stale_after_seconds: int = Field(default=300, ge=1)
    template_sync_poll_interval_seconds: float = Field(default=2.0, ge=0.1)
    template_sync_timeout_seconds: int = Field(default=1800, ge=1)
    workspace_build_poll_interval_seconds: float = Field(default=2.0, ge=0.1)
    workspace_build_timeout_seconds: int = Field(default=1800, ge=1)
    workspace_stop_poll_interval_seconds: float = Field(default=2.0, ge=0.1)
    workspace_stop_timeout_seconds: int = Field(default=1800, ge=1)
    workspace_delete_poll_interval_seconds: float = Field(default=2.0, ge=0.1)
    workspace_delete_timeout_seconds: int = Field(default=1800, ge=1)
    instance_domain: str = Field(
        default="code-studio",
        pattern=r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$",
    )
    crypto_key: SecretStr | None = None
    argocd_url: str | None = None
    argocd_development_token: SecretStr | None = None
    argocd_staging_token: SecretStr | None = None
    argocd_production_token: SecretStr | None = None
    argocd_skip_ssl_verify: bool = False
    argocd_development_application_prefix: str | None = None
    argocd_staging_application_prefix: str | None = None
    argocd_production_application_prefix: str | None = None
    argocd_region: str | None = None
    argocd_repository_url: str | None = None
    argocd_repository_path: str | None = None
    argocd_target_revision: str | None = None
    argocd_development_project_name: str | None = None
    argocd_staging_project_name: str | None = None
    argocd_production_project_name: str | None = None
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

    def require_database_schema(self) -> str:
        """Return the configured database schema or fail for a database consumer."""

        if self.database_schema is None or not self.database_schema.strip():
            msg = "CODER_MANAGER_DATABASE_SCHEMA is required"
            raise ValueError(msg)
        return self.database_schema

    @field_validator("scheduler_timezone")
    @classmethod
    def validate_scheduler_timezone(cls, value: str) -> str:
        """Require a timezone understood by Celery and the Python runtime."""

        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as error:
            msg = "scheduler_timezone must be a valid IANA timezone"
            raise ValueError(msg) from error
        return value


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings instance."""

    return Settings()
