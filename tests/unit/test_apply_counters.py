import pytest

from app.services.apply_report import classify_stock_change

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("previous", "explicit", "warehouse_in_report", "expected"),
    [
        (None, 5, True, "created"),
        (5, 7, True, "updated"),
        (5, 5, True, None),
        (None, 0, True, "created"),
        (5, 0, True, "updated"),
        (5, None, True, "zeroed"),
        (0, None, True, None),
        (5, None, False, None),
    ],
)
def test_stock_counter_classification_is_mutually_exclusive(
    previous: int | None,
    explicit: int | None,
    warehouse_in_report: bool,
    expected: str | None,
) -> None:
    assert (
        classify_stock_change(
            previous_quantity=previous,
            explicit_quantity=explicit,
            warehouse_in_report=warehouse_in_report,
        )
        == expected
    )
