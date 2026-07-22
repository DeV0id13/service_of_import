import csv
import io
import re
from collections.abc import Buffer, Callable, Iterator
from dataclasses import dataclass

REQUIRED_COLUMNS = (
    "warehouse_code",
    "warehouse_name",
    "sku",
    "product_name",
    "quantity",
)
POSTGRES_BIGINT_MAX = 9_223_372_036_854_775_807
INTEGER_PATTERN = re.compile(r"^[+-]?[0-9]+$")
csv.field_size_limit(2_147_483_647)


@dataclass(frozen=True, slots=True)
class ValidatedRow:
    line_number: int
    warehouse_code: str
    warehouse_name: str
    sku: str
    product_name: str
    quantity: int


@dataclass(frozen=True, slots=True)
class ValidationIssue:
    line_number: int | None
    field_name: str | None
    code: str
    message: str
    raw_data: dict[str, object] | None = None


@dataclass(frozen=True, slots=True)
class CsvValidationSummary:
    row_count: int
    reached_eof: bool


class ChunkIteratorIO(io.RawIOBase):
    """Expose bounded byte chunks as a RawIOBase without joining the stream."""

    def __init__(
        self,
        chunks: Iterator[bytes],
        before_next_chunk: Callable[[], None] | None = None,
    ) -> None:
        super().__init__()
        self._chunks = chunks
        self._before_next_chunk = before_next_chunk
        self._current = memoryview(b"")
        self._offset = 0

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: Buffer) -> int:
        target = memoryview(buffer).cast("B")
        written = 0
        while written < len(target):
            if self._offset >= len(self._current):
                if self._before_next_chunk is not None:
                    self._before_next_chunk()
                try:
                    chunk = next(self._chunks)
                except StopIteration:
                    break
                if not chunk:
                    continue
                self._current = memoryview(chunk)
                self._offset = 0

            available = min(len(target) - written, len(self._current) - self._offset)
            target[written : written + available] = self._current[
                self._offset : self._offset + available
            ]
            written += available
            self._offset += available
        return written

    def close(self) -> None:
        close_chunks = getattr(self._chunks, "close", None)
        if callable(close_chunks):
            close_chunks()
        super().close()


def validate_csv_stream(
    chunks: Iterator[bytes],
    on_valid_row: Callable[[ValidatedRow], None],
    on_issues: Callable[[list[ValidationIssue]], None],
    *,
    before_next_chunk: Callable[[], None] | None = None,
) -> CsvValidationSummary:
    """Validate logical CSV records while retaining at most one input chunk.

    Logical records use header=1 and first data record=2. If csv.reader fails while
    constructing a record, the error is assigned to the next logical record number;
    physical line information is not stable for quoted multiline fields.
    """

    raw_stream = ChunkIteratorIO(chunks, before_next_chunk)
    buffered = io.BufferedReader(raw_stream)
    text_stream = io.TextIOWrapper(
        buffered,
        encoding="utf-8-sig",
        errors="surrogateescape",
        newline="",
    )
    reader = csv.reader(text_stream, delimiter=",", strict=True)
    logical_line_number = 1
    row_count = 0

    try:
        try:
            header = next(reader)
        except StopIteration:
            on_issues(
                [
                    ValidationIssue(
                        line_number=None,
                        field_name=None,
                        code="missing_header",
                        message="CSV header is missing",
                    )
                ]
            )
            return CsvValidationSummary(row_count=0, reached_eof=True)
        except csv.Error:
            on_issues([_malformed_csv_issue(1)])
            return CsvValidationSummary(row_count=0, reached_eof=False)

        if _contains_surrogate(header):
            on_issues([_invalid_utf8_issue(None)])
            return CsvValidationSummary(row_count=0, reached_eof=False)

        header_issues = _validate_header(header)
        if header_issues:
            on_issues(header_issues)
            return CsvValidationSummary(row_count=0, reached_eof=False)

        column_indexes = {name: header.index(name) for name in REQUIRED_COLUMNS}

        while True:
            try:
                row = next(reader)
            except StopIteration:
                if row_count == 0:
                    on_issues(
                        [
                            ValidationIssue(
                                line_number=None,
                                field_name=None,
                                code="no_data_rows",
                                message="CSV contains no data rows",
                            )
                        ]
                    )
                return CsvValidationSummary(row_count=row_count, reached_eof=True)
            except csv.Error:
                on_issues([_malformed_csv_issue(logical_line_number + 1)])
                return CsvValidationSummary(row_count=row_count, reached_eof=False)

            logical_line_number += 1
            row_count += 1
            if _contains_surrogate(row):
                on_issues([_invalid_utf8_issue(logical_line_number)])
                continue
            issues, validated_row = _validate_row(
                row,
                header,
                column_indexes,
                logical_line_number,
            )
            if issues:
                on_issues(issues)
            elif validated_row is not None:
                on_valid_row(validated_row)
    finally:
        text_stream.close()


def _validate_header(header: list[str]) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    raw_data: dict[str, object] = {"header": header}
    for column in REQUIRED_COLUMNS:
        occurrences = header.count(column)
        if occurrences == 0:
            issues.append(
                ValidationIssue(
                    line_number=None,
                    field_name=column,
                    code="missing_required_column",
                    message=f"Required column '{column}' is missing",
                    raw_data=raw_data,
                )
            )
        elif occurrences > 1:
            issues.append(
                ValidationIssue(
                    line_number=None,
                    field_name=column,
                    code="duplicate_required_column",
                    message=f"Required column '{column}' appears more than once",
                    raw_data=raw_data,
                )
            )
    return issues


def _validate_row(
    row: list[str],
    header: list[str],
    column_indexes: dict[str, int],
    line_number: int,
) -> tuple[list[ValidationIssue], ValidatedRow | None]:
    if len(row) != len(header):
        return (
            [
                ValidationIssue(
                    line_number=line_number,
                    field_name=None,
                    code="invalid_column_count",
                    message="CSV row has a different number of fields than the header",
                    raw_data={"values": row},
                )
            ],
            None,
        )

    raw_values = {name: row[index] for name, index in column_indexes.items()}
    values = {name: value.strip() for name, value in raw_values.items()}
    raw_data: dict[str, object] = dict(raw_values)
    issues: list[ValidationIssue] = []

    for field_name in ("warehouse_code", "warehouse_name", "sku", "product_name"):
        if not values[field_name]:
            issues.append(
                ValidationIssue(
                    line_number=line_number,
                    field_name=field_name,
                    code="required",
                    message=f"Field '{field_name}' must not be empty",
                    raw_data=raw_data,
                )
            )

    quantity_text = values["quantity"]
    quantity: int | None = None
    if not INTEGER_PATTERN.fullmatch(quantity_text):
        issues.append(
            ValidationIssue(
                line_number=line_number,
                field_name="quantity",
                code="invalid_quantity",
                message="Quantity must be a decimal integer",
                raw_data=raw_data,
            )
        )
    else:
        quantity = int(quantity_text)
        if quantity < 0:
            issues.append(
                ValidationIssue(
                    line_number=line_number,
                    field_name="quantity",
                    code="quantity_negative",
                    message="Quantity must be greater than or equal to zero",
                    raw_data=raw_data,
                )
            )
        elif quantity > POSTGRES_BIGINT_MAX:
            issues.append(
                ValidationIssue(
                    line_number=line_number,
                    field_name="quantity",
                    code="quantity_too_large",
                    message="Quantity exceeds the PostgreSQL BIGINT range",
                    raw_data=raw_data,
                )
            )

    if issues or quantity is None:
        return issues, None

    return (
        [],
        ValidatedRow(
            line_number=line_number,
            warehouse_code=values["warehouse_code"],
            warehouse_name=values["warehouse_name"],
            sku=values["sku"],
            product_name=values["product_name"],
            quantity=quantity,
        ),
    )


def _invalid_utf8_issue(line_number: int | None) -> ValidationIssue:
    return ValidationIssue(
        line_number=line_number,
        field_name=None,
        code="invalid_utf8",
        message="CSV must be encoded as UTF-8",
    )


def _contains_surrogate(values: list[str]) -> bool:
    return any(0xDC80 <= ord(character) <= 0xDCFF for value in values for character in value)


def _malformed_csv_issue(line_number: int) -> ValidationIssue:
    return ValidationIssue(
        line_number=line_number,
        field_name=None,
        code="malformed_csv",
        message="CSV syntax is malformed",
    )
