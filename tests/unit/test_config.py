import pytest
from pydantic import ValidationError

from app.config import Settings

pytestmark = pytest.mark.unit


def test_default_csv_limits_are_bounded_and_consistent() -> None:
    settings = Settings()

    assert settings.csv_max_field_chars == 1_048_576
    assert settings.csv_max_record_chars == 4_194_304
    assert settings.csv_error_raw_value_chars == 1_024
    assert settings.csv_error_raw_total_chars == 4_096


def test_csv_record_and_raw_total_limits_reject_inconsistent_settings() -> None:
    with pytest.raises(ValidationError):
        Settings(csv_max_field_chars=8_192, csv_max_record_chars=4_096)

    with pytest.raises(ValidationError):
        Settings(csv_error_raw_value_chars=1_024, csv_error_raw_total_chars=2_048)
