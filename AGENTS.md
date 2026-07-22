# Repository guidance

## Current phase

The repository is in the planning phase. Until the user explicitly requests implementation, do not add application code, business logic, migrations, Docker configuration, dependencies, or generated fixtures.

The approved files for this phase are:

- `docs/architecture.md`;
- `docs/implementation-plan.md`;
- `AGENTS.md`.

Use `docs/test-assignment.docx` as the source assignment and `docs/architecture.md` as the technical contract.

## Target commands

These commands describe the intended interface after implementation. They are not expected to work during the documentation-only phase.

### Start and stop

```bash
cp .env.example .env
docker compose up --build -d
docker compose ps
docker compose logs -f api worker
docker compose down
```

### Migrations

```bash
docker compose run --rm api alembic upgrade head
docker compose run --rm api alembic current
docker compose run --rm api alembic check
```

### Tests and quality checks

```bash
docker compose run --rm api pytest -q
docker compose run --rm api ruff check .
docker compose run --rm api ruff format --check .
docker compose run --rm api mypy app tests
```

Do not run `docker compose down -v` or delete persistent data without an explicit user request.

## Architecture constraints

- Use Python 3.12, FastAPI, SQLAlchemy 2.x, Alembic, PostgreSQL, MinIO/boto3, Pydantic Settings, pytest, Ruff, mypy, Docker, and Docker Compose.
- Run API and worker as separate processes from one application image.
- Use PostgreSQL as both the state store and the simple report queue. Do not add Redis, Celery, Kafka, or another broker.
- API must save the original file to MinIO before creating a `pending` report.
- A registered original file must remain downloadable for both successful and failed reports.
- Worker must read CSV from MinIO as a stream and write validated rows to a staging table in bounded batches.
- Never create or change warehouses, products, or stock balances before the whole CSV has passed validation.
- Apply warehouse/product changes, explicit stock updates, implicit zeroing, counters, and `completed` in one PostgreSQL transaction.
- After an apply error, roll back first, then save `failed` and the error in a separate transaction.
- Keep zero balances as rows with `quantity = 0`.
- Treat the CSV as a full snapshot only for warehouses present in that report. Do not change other warehouses.
- Process reports strictly by `created_at ASC, id ASC`.
- Coordinate workers with one session-level PostgreSQL advisory lock held on a dedicated connection while a report is processed.
- Do not use `SKIP LOCKED` to bypass an earlier report.
- Use Alembic for the schema; do not use `metadata.create_all()` for the application database.
- Store timezone-aware timestamps and log report ID, processing stage, and result.

## CSV and memory rules

- Maximum accepted file size is `1_073_741_824` bytes unless the architecture is explicitly changed.
- Never load the whole CSV into memory. Do not use unbounded `.read()`, `read_bytes()`, `list(reader)`, pandas, or an in-memory set containing every file key.
- Read files in fixed-size chunks and write staging/errors in bounded batches.
- Validate the actual number of bytes read, not only `Content-Length`.
- Accept UTF-8 and UTF-8 BOM with comma delimiter.
- Use staging/SQL to detect duplicate `(warehouse_code, sku)` pairs across the complete file.

## Test discipline

- Run all existing tests and all three quality checks after every implementation stage.
- After schema changes, also run migration upgrade and `alembic check`.
- Test that streaming code never requests an unbounded read.
- Test a large sequence of valid rows followed by an error at the end.
- Test that a failed report leaves warehouses, products, and stocks unchanged while its original remains downloadable.
- Test selective zeroing: only warehouses present in the report are synchronized.
- Test FIFO processing with two worker processes/connections and a real PostgreSQL advisory lock.
- Test rollback by raising an error during the final apply transaction.
- Do not weaken tests or add gigabyte fixtures to the repository.

## Dependency policy

- Do not add a dependency unless it is needed for a current requirement and the standard library or existing stack is insufficient.
- Do not add infrastructure or abstraction layers “for later”.
- Prefer readable, explicit transaction code over generic repositories or a single opaque SQL statement.
- A stage is complete only when its tests, Ruff, formatting check, and mypy pass.
