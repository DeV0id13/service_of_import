from collections.abc import Iterator
from dataclasses import dataclass

import pytest

from app.services.csv_validation import (
    ValidatedRow,
    ValidationIssue,
    validate_csv_stream,
)

pytestmark = pytest.mark.unit

HEADER = "warehouse_code,warehouse_name,sku,product_name,quantity\n"


@dataclass(slots=True)
class ValidationResult:
    rows: list[ValidatedRow]
    issues: list[ValidationIssue]
    row_count: int
    reached_eof: bool


def validate(content: bytes, *, chunk_size: int = 7) -> ValidationResult:
    rows: list[ValidatedRow] = []
    issues: list[ValidationIssue] = []
    chunks = (content[start : start + chunk_size] for start in range(0, len(content), chunk_size))
    summary = validate_csv_stream(chunks, rows.append, issues.extend)
    return ValidationResult(
        rows=rows,
        issues=issues,
        row_count=summary.row_count,
        reached_eof=summary.reached_eof,
    )


def test_valid_utf8_and_zero_quantity() -> None:
    result = validate((HEADER + " WH-1 , Main warehouse , SKU-1 , Product , 0 \n").encode())
    assert result.issues == []
    assert result.rows == [
        ValidatedRow(
            line_number=2,
            warehouse_code="WH-1",
            warehouse_name="Main warehouse",
            sku="SKU-1",
            product_name="Product",
            quantity=0,
        )
    ]
    assert result.row_count == 1
    assert result.reached_eof


def test_utf8_bom_is_accepted() -> None:
    result = validate(b"\xef\xbb\xbf" + (HEADER + "WH,W,SKU,P,1\n").encode())
    assert result.issues == []
    assert result.rows[0].warehouse_code == "WH"


def test_missing_required_header_column() -> None:
    result = validate(b"warehouse_code,warehouse_name,sku,quantity\nWH,W,S,1\n")
    assert [issue.code for issue in result.issues] == ["missing_required_column"]
    assert result.issues[0].field_name == "product_name"


def test_duplicate_required_header_column() -> None:
    content = b"warehouse_code,warehouse_name,sku,product_name,quantity,sku\n" b"WH,W,S,P,1,S\n"
    result = validate(content)
    assert [issue.code for issue in result.issues] == ["duplicate_required_column"]
    assert result.issues[0].field_name == "sku"


def test_additional_columns_are_ignored() -> None:
    content = (
        b"warehouse_code,warehouse_name,sku,product_name,quantity,note\n" b"WH,W,S,P,1,ignored\n"
    )
    result = validate(content)
    assert result.issues == []
    assert result.rows[0].quantity == 1


def test_quoted_comma_and_multiline_fields() -> None:
    content = (HEADER + 'WH,"Warehouse, North",SKU,"Product\nmultiline",2\n').encode()
    result = validate(content, chunk_size=3)
    assert result.issues == []
    assert result.rows[0].warehouse_name == "Warehouse, North"
    assert result.rows[0].product_name == "Product\nmultiline"
    assert result.rows[0].line_number == 2


@pytest.mark.parametrize(
    ("row", "expected_field", "expected_code"),
    [
        (",Warehouse,SKU,Product,1\n", "warehouse_code", "required"),
        ("WH,,SKU,Product,1\n", "warehouse_name", "required"),
        ("WH,Warehouse,,Product,1\n", "sku", "required"),
        ("WH,Warehouse,SKU,,1\n", "product_name", "required"),
        ("WH,Warehouse,SKU,Product,-1\n", "quantity", "quantity_negative"),
        ("WH,Warehouse,SKU,Product,1.5\n", "quantity", "invalid_quantity"),
        ("WH,Warehouse,SKU,Product,many\n", "quantity", "invalid_quantity"),
        (
            f"WH,Warehouse,SKU,Product,{2**63}\n",
            "quantity",
            "quantity_too_large",
        ),
    ],
)
def test_invalid_field_values(row: str, expected_field: str, expected_code: str) -> None:
    result = validate((HEADER + row).encode())
    assert result.rows == []
    assert any(
        issue.field_name == expected_field and issue.code == expected_code
        for issue in result.issues
    )
    assert result.issues[0].line_number == 2
    assert result.issues[0].raw_data is not None


def test_malformed_csv_reports_next_logical_record() -> None:
    result = validate((HEADER + 'WH,"unterminated,SKU,Product,1').encode())
    assert result.rows == []
    assert result.issues[0].code == "malformed_csv"
    assert result.issues[0].line_number == 2


def test_invalid_utf8_is_validation_error() -> None:
    result = validate(HEADER.encode() + b"WH,W,SKU,\xff,1\n")
    assert result.rows == []
    assert result.issues[0].code == "invalid_utf8"
    assert result.issues[0].line_number == 2


def test_empty_file_is_invalid() -> None:
    result = validate(b"")
    assert result.issues[0].code == "missing_header"
    assert result.row_count == 0


def test_header_only_file_is_invalid() -> None:
    result = validate(HEADER.encode())
    assert result.issues[0].code == "no_data_rows"
    assert result.row_count == 0
    assert result.reached_eof


def test_wrong_column_count_is_invalid() -> None:
    result = validate((HEADER + "WH,W,SKU,Product\n").encode())
    assert result.issues[0].code == "invalid_column_count"
    assert result.issues[0].raw_data == {"values": ["WH", "W", "SKU", "Product"]}


def test_byte_iterator_is_consumed_incrementally() -> None:
    requested_chunks = 0

    def chunks() -> Iterator[bytes]:
        nonlocal requested_chunks
        for byte in (HEADER + "WH,W,SKU,P,1\n").encode():
            requested_chunks += 1
            yield bytes([byte])

    rows: list[ValidatedRow] = []
    issues: list[ValidationIssue] = []
    summary = validate_csv_stream(chunks(), rows.append, issues.extend)

    assert summary.row_count == 1
    assert issues == []
    assert requested_chunks == len((HEADER + "WH,W,SKU,P,1\n").encode())
