from functools import lru_cache
from typing import Self

from pydantic import Field, SecretStr, model_validator
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

    csv_max_field_chars: int = Field(default=1_048_576, ge=1_024, le=4_194_304)
    csv_max_record_chars: int = Field(default=4_194_304, ge=4_096, le=16_777_216)
    csv_error_raw_value_chars: int = Field(default=1_024, ge=64, le=16_384)
    csv_error_raw_total_chars: int = Field(default=4_096, ge=256, le=65_536)

    @model_validator(mode="after")
    def validate_csv_limits(self) -> Self:
        if self.csv_max_record_chars < self.csv_max_field_chars:
            raise ValueError("CSV_MAX_RECORD_CHARS must be at least CSV_MAX_FIELD_CHARS")
        if self.csv_error_raw_total_chars < 4 * self.csv_error_raw_value_chars:
            raise ValueError(
                "CSV_ERROR_RAW_TOTAL_CHARS must be at least four times " "CSV_ERROR_RAW_VALUE_CHARS"
            )
        return self


@lru_cache
def get_settings() -> Settings:
    """Return one validated settings object per process."""

    return Settings()
