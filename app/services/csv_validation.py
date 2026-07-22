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
DEFAULT_MAX_FIELD_CHARS = 1_048_576
DEFAULT_MAX_RECORD_CHARS = 4_194_304
DEFAULT_ERROR_RAW_VALUE_CHARS = 1_024
DEFAULT_ERROR_RAW_TOTAL_CHARS = 4_096
_TEXT_READ_CHARS = 64 * 1024
_TRUNCATION_MARKER = "...[truncated]"
_CSV_SPECIAL_CHARACTER = re.compile('[",\r\n]')
csv.field_size_limit(DEFAULT_MAX_FIELD_CHARS)


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


@dataclass(frozen=True, slots=True)
class LogicalCsvRecord:
    text: str
    char_count: int
    truncated: bool


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
    max_field_chars: int = DEFAULT_MAX_FIELD_CHARS,
    max_record_chars: int = DEFAULT_MAX_RECORD_CHARS,
    error_raw_value_chars: int = DEFAULT_ERROR_RAW_VALUE_CHARS,
    error_raw_total_chars: int = DEFAULT_ERROR_RAW_TOTAL_CHARS,
) -> CsvValidationSummary:
    """Validate logical CSV records while retaining at most one input chunk.

    Logical records use header=1 and first data record=2. If csv.reader fails while
    constructing a record, the error is assigned to the next logical record number;
    physical line information is not stable for quoted multiline fields.
    """

    if max_field_chars <= 0 or max_record_chars < max_field_chars:
        raise ValueError("CSV record limit must be positive and at least the field limit")
    if error_raw_value_chars <= 0 or error_raw_total_chars <= 0:
        raise ValueError("CSV error raw-data limits must be positive")

    csv.field_size_limit(max_field_chars)
    raw_stream = ChunkIteratorIO(chunks, before_next_chunk)
    buffered = io.BufferedReader(raw_stream)
    text_stream = io.TextIOWrapper(
        buffered,
        encoding="utf-8-sig",
        errors="surrogateescape",
        newline="",
    )
    records = _iter_logical_records(
        text_stream,
        max_record_chars=max_record_chars,
        preview_chars=error_raw_value_chars,
    )
    logical_line_number = 1
    row_count = 0

    try:
        try:
            header_record = next(records)
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

        if header_record.truncated:
            on_issues(
                [
                    _record_too_large_issue(
                        None,
                        header_record,
                        max_record_chars,
                        error_raw_value_chars,
                        error_raw_total_chars,
                    )
                ]
            )
            return CsvValidationSummary(row_count=0, reached_eof=False)

        header, header_parse_issue = _parse_record(
            header_record,
            None,
            max_field_chars,
            error_raw_value_chars,
            error_raw_total_chars,
        )
        if header_parse_issue is not None:
            on_issues([header_parse_issue])
            return CsvValidationSummary(row_count=0, reached_eof=False)
        assert header is not None

        if _contains_surrogate(header):
            on_issues([_invalid_utf8_issue(None)])
            return CsvValidationSummary(row_count=0, reached_eof=False)

        header_issues = _validate_header(
            header,
            error_raw_value_chars,
            error_raw_total_chars,
        )
        if header_issues:
            on_issues(header_issues)
            return CsvValidationSummary(row_count=0, reached_eof=False)

        column_indexes = {name: header.index(name) for name in REQUIRED_COLUMNS}

        for record in records:
            logical_line_number += 1
            row_count += 1
            if record.truncated:
                on_issues(
                    [
                        _record_too_large_issue(
                            logical_line_number,
                            record,
                            max_record_chars,
                            error_raw_value_chars,
                            error_raw_total_chars,
                        )
                    ]
                )
                continue

            row, row_parse_issue = _parse_record(
                record,
                logical_line_number,
                max_field_chars,
                error_raw_value_chars,
                error_raw_total_chars,
            )
            if row_parse_issue is not None:
                on_issues([row_parse_issue])
                if row_parse_issue.code == "csv_field_too_large":
                    continue
                return CsvValidationSummary(row_count=row_count, reached_eof=False)
            assert row is not None

            if _contains_surrogate(row):
                on_issues([_invalid_utf8_issue(logical_line_number)])
                continue
            issues, validated_row = _validate_row(
                row,
                header,
                column_indexes,
                logical_line_number,
                error_raw_value_chars,
                error_raw_total_chars,
            )
            if issues:
                on_issues(issues)
            elif validated_row is not None:
                on_valid_row(validated_row)

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
    finally:
        text_stream.close()


def _validate_header(
    header: list[str],
    error_raw_value_chars: int,
    error_raw_total_chars: int,
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    raw_data = _bounded_raw_data(
        {"header": header},
        error_raw_value_chars,
        error_raw_total_chars,
    )
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
    error_raw_value_chars: int,
    error_raw_total_chars: int,
) -> tuple[list[ValidationIssue], ValidatedRow | None]:
    if len(row) != len(header):
        return (
            [
                ValidationIssue(
                    line_number=line_number,
                    field_name=None,
                    code="invalid_column_count",
                    message="CSV row has a different number of fields than the header",
                    raw_data=_bounded_raw_data(
                        {"values": row},
                        error_raw_value_chars,
                        error_raw_total_chars,
                    ),
                )
            ],
            None,
        )

    raw_values = {name: row[index] for name, index in column_indexes.items()}
    values = {name: value.strip() for name, value in raw_values.items()}
    raw_data = _bounded_raw_data(
        dict(raw_values),
        error_raw_value_chars,
        error_raw_total_chars,
    )
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


def _iter_logical_records(
    text_stream: io.TextIOWrapper,
    *,
    max_record_chars: int,
    preview_chars: int,
) -> Iterator[LogicalCsvRecord]:
    """Split CSV records with bounded storage while respecting quoted newlines."""

    parts: list[str] = []
    stored_chars = 0
    record_chars = 0
    truncated = False
    in_quotes = False
    quote_pending = False
    at_field_start = True
    skip_lf_after_cr = False

    def append_piece(piece: str) -> None:
        nonlocal parts, stored_chars, truncated
        if not piece or truncated:
            return
        remaining = max_record_chars - stored_chars
        if len(piece) <= remaining:
            parts.append(piece)
            stored_chars += len(piece)
            return
        if remaining > 0:
            parts.append(piece[:remaining])
        preview = "".join(parts)[:preview_chars]
        parts = [preview]
        stored_chars = len(preview)
        truncated = True

    while True:
        chunk = text_stream.read(_TEXT_READ_CHARS)
        if not chunk:
            break

        segment_start = 0
        counted_start = 0
        for match in _CSV_SPECIAL_CHARACTER.finditer(chunk):
            index = match.start()
            character = match.group()
            if skip_lf_after_cr:
                if index == segment_start and character == "\n":
                    skip_lf_after_cr = False
                    segment_start = index + 1
                    counted_start = index + 1
                    continue
                skip_lf_after_cr = False

            normal_chars = index - counted_start
            if normal_chars:
                record_chars += normal_chars
                if in_quotes and quote_pending:
                    in_quotes = False
                    quote_pending = False
                if not in_quotes:
                    at_field_start = False

            record_chars += 1
            is_boundary = False
            reprocess = True
            while reprocess:
                reprocess = False
                if in_quotes:
                    if quote_pending:
                        if character == '"':
                            quote_pending = False
                        else:
                            in_quotes = False
                            quote_pending = False
                            reprocess = True
                    elif character == '"':
                        quote_pending = True
                elif character in {"\r", "\n"}:
                    is_boundary = True
                elif character == ",":
                    at_field_start = True
                elif character == '"' and at_field_start:
                    in_quotes = True
                    at_field_start = False
                else:
                    at_field_start = False

            if is_boundary:
                append_piece(chunk[segment_start : index + 1])
                record = LogicalCsvRecord(
                    text="".join(parts),
                    char_count=record_chars,
                    truncated=truncated,
                )
                parts = []
                stored_chars = 0
                record_chars = 0
                truncated = False
                in_quotes = False
                quote_pending = False
                at_field_start = True
                skip_lf_after_cr = character == "\r"
                segment_start = index + 1
                yield record
            counted_start = index + 1

        trailing_chars = len(chunk) - counted_start
        if trailing_chars:
            skip_lf_after_cr = False
            record_chars += trailing_chars
            if in_quotes and quote_pending:
                in_quotes = False
                quote_pending = False
            if not in_quotes:
                at_field_start = False
        append_piece(chunk[segment_start:])

    if record_chars:
        yield LogicalCsvRecord(
            text="".join(parts),
            char_count=record_chars,
            truncated=truncated,
        )


def _parse_record(
    record: LogicalCsvRecord,
    line_number: int | None,
    max_field_chars: int,
    error_raw_value_chars: int,
    error_raw_total_chars: int,
) -> tuple[list[str] | None, ValidationIssue | None]:
    record_stream = io.StringIO(record.text, newline="")
    reader = csv.reader(record_stream, delimiter=",", strict=True)
    try:
        try:
            row = next(reader)
        except StopIteration:
            row = []
        except csv.Error as exc:
            if "field larger than field limit" in str(exc):
                raw_data = _bounded_raw_data(
                    {"record_preview": record.text},
                    error_raw_value_chars,
                    error_raw_total_chars,
                )
                raw_data["max_field_chars"] = max_field_chars
                raw_data["_truncated"] = True
                return (
                    None,
                    ValidationIssue(
                        line_number=line_number,
                        field_name=None,
                        code="csv_field_too_large",
                        message=f"CSV field exceeds {max_field_chars} characters",
                        raw_data=raw_data,
                    ),
                )
            malformed_line = line_number if line_number is not None else 1
            return None, _malformed_csv_issue(malformed_line)

        try:
            next(reader)
        except StopIteration:
            return row, None
        except csv.Error:
            return None, _malformed_csv_issue(line_number if line_number is not None else 1)
        return None, _malformed_csv_issue(line_number if line_number is not None else 1)
    finally:
        record_stream.close()


def _record_too_large_issue(
    line_number: int | None,
    record: LogicalCsvRecord,
    max_record_chars: int,
    error_raw_value_chars: int,
    error_raw_total_chars: int,
) -> ValidationIssue:
    raw_data = _bounded_raw_data(
        {"record_preview": record.text},
        error_raw_value_chars,
        error_raw_total_chars,
    )
    raw_data.update(
        {
            "record_chars": record.char_count,
            "max_record_chars": max_record_chars,
            "_truncated": True,
        }
    )
    return ValidationIssue(
        line_number=line_number,
        field_name=None,
        code="csv_record_too_large",
        message=f"CSV logical record exceeds {max_record_chars} characters",
        raw_data=raw_data,
    )


def _bounded_raw_data(
    raw_data: dict[str, object],
    value_limit: int,
    total_limit: int,
) -> dict[str, object]:
    result: dict[str, object] = {}
    remaining = total_limit
    truncated = False

    for key, value in raw_data.items():
        if isinstance(value, str):
            bounded, used, was_truncated = _bounded_raw_text(value, value_limit, remaining)
            result[key] = bounded
            remaining -= used
            truncated = truncated or was_truncated
        elif isinstance(value, list):
            bounded_values: list[object] = []
            for item in value:
                if not isinstance(item, str):
                    bounded_values.append(item)
                    continue
                if remaining == 0:
                    truncated = True
                    break
                bounded, used, was_truncated = _bounded_raw_text(item, value_limit, remaining)
                bounded_values.append(bounded)
                remaining -= used
                truncated = truncated or was_truncated
            if len(bounded_values) != len(value):
                truncated = True
            result[key] = bounded_values
        else:
            result[key] = value

    if truncated:
        result["_truncated"] = True
    return result


def _bounded_raw_text(value: str, value_limit: int, remaining: int) -> tuple[str, int, bool]:
    allowed = min(value_limit, remaining)
    if len(value) <= allowed:
        return value, len(value), False
    if allowed <= 0:
        return "", 0, bool(value)
    if allowed > len(_TRUNCATION_MARKER):
        bounded = value[: allowed - len(_TRUNCATION_MARKER)] + _TRUNCATION_MARKER
    else:
        bounded = value[:allowed]
    return bounded, len(bounded), True
