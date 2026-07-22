from functools import lru_cache

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings supplied through environment variables."""

    model_config = SettingsConfigDict(
        env_prefix="",
        case_sensitive=False,
        extra="ignore",
    )

    app_name: str = "Service of Import"
    app_environment: str = "local"
    log_level: str = "INFO"

    database_url: str = (
        "postgresql+psycopg://import_service:import_service@postgres:5432/import_service"
    )

    s3_endpoint_url: str = "http://minio:9000"
    s3_access_key: str = "minioadmin"
    s3_secret_key: SecretStr = SecretStr("minioadmin")
    s3_bucket: str = "stock-reports"
    s3_region: str = "us-east-1"


@lru_cache
def get_settings() -> Settings:
    """Return one validated settings object per process."""

    return Settings()
