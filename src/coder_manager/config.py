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
    global_whitelist: bool = False
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
    argocd_destination_name: str | None = None
    cyberark_emea_development_app_id: str | None = None
    cyberark_emea_development_cert_name: str | None = None
    cyberark_emea_development_key_name: str | None = None
    cyberark_emea_development_safe: str | None = None
    cyberark_emea_staging_app_id: str | None = None
    cyberark_emea_staging_cert_name: str | None = None
    cyberark_emea_staging_key_name: str | None = None
    cyberark_emea_staging_safe: str | None = None
    cyberark_emea_production_app_id: str | None = None
    cyberark_emea_production_cert_name: str | None = None
    cyberark_emea_production_key_name: str | None = None
    cyberark_emea_production_safe: str | None = None
    cyberark_apac_development_app_id: str | None = None
    cyberark_apac_development_cert_name: str | None = None
    cyberark_apac_development_key_name: str | None = None
    cyberark_apac_development_safe: str | None = None
    cyberark_apac_staging_app_id: str | None = None
    cyberark_apac_staging_cert_name: str | None = None
    cyberark_apac_staging_key_name: str | None = None
    cyberark_apac_staging_safe: str | None = None
    cyberark_apac_production_app_id: str | None = None
    cyberark_apac_production_cert_name: str | None = None
    cyberark_apac_production_key_name: str | None = None
    cyberark_apac_production_safe: str | None = None
    cyberark_amer_development_app_id: str | None = None
    cyberark_amer_development_cert_name: str | None = None
    cyberark_amer_development_key_name: str | None = None
    cyberark_amer_development_safe: str | None = None
    cyberark_amer_staging_app_id: str | None = None
    cyberark_amer_staging_cert_name: str | None = None
    cyberark_amer_staging_key_name: str | None = None
    cyberark_amer_staging_safe: str | None = None
    cyberark_amer_production_app_id: str | None = None
    cyberark_amer_production_cert_name: str | None = None
    cyberark_amer_production_key_name: str | None = None
    cyberark_amer_production_safe: str | None = None
    default_admins: str = ""


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings instance."""

    return Settings()
