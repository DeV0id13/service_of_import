from functools import lru_cache

from pydantic import Field, SecretStr
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

    worker_poll_interval_seconds: float = Field(default=2.0, gt=0)
    worker_advisory_lock_key: int = 7_314_602_941
    validation_batch_size: int = Field(default=500, ge=1, le=10_000)


@lru_cache
def get_settings() -> Settings:
    """Return one validated settings object per process."""

    return Settings()
