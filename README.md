# Service of Import

Backend-сервис принимает полные CSV-снимки остатков товаров по складам. Оригинал
файла потоково сохраняется в MinIO, отдельный worker проверяет CSV и атомарно
синхронизирует данные в PostgreSQL. Сервис хранит текущее состояние остатков и не
рассчитывает его по движениям или операциям прихода/расхода.

## Архитектура

- FastAPI принимает оригинал, предоставляет статусы, ошибки, каталоги и остатки.
- API и отдельный worker запускаются из одного Docker image.
- PostgreSQL хранит данные и служит простой FIFO-очередью отчётов.
- MinIO хранит зарегистрированные оригиналы независимо от результата валидации.
- Worker читает CSV потоково и пишет проверенные строки в staging ограниченными
  batch-ами.
- Session-level PostgreSQL advisory lock сериализует worker-процессы.
- Склады, товары и остатки не меняются до успешной проверки всего файла.
- Upsert каталогов, явные остатки, selective zeroing, счётчики и `completed`
  фиксируются одной apply-транзакцией PostgreSQL.

### Snapshot-семантика

Отчёт является полным снимком только для складов, которые в нём представлены.
Например, если отчёт содержит `MSK-1 / A-001` и `MSK-1 / A-002`, ранее существующая
пара `MSK-1 / A-003` станет нулевой. Остатки `SPB-1` останутся без изменений, если
строк `SPB-1` в отчёте нет. Нулевые остатки остаются строками в БД.

## Требования

- Linux как целевое окружение;
- Docker Engine и Docker Compose v2;
- `curl`, `cmp`, POSIX shell и `make` — для примеров и удобных команд;
- достаточно дискового места для PostgreSQL, MinIO и временного spool-файла
  FastAPI `UploadFile` при больших multipart uploads.

Порты по умолчанию:

| Компонент | Порт |
|---|---:|
| FastAPI | `8000` |
| PostgreSQL | `5432` |
| MinIO S3 API | `9000` |
| MinIO Console | `9001` |

## Конфигурация

Создайте локальную конфигурацию из безопасного примера:

```bash
cp .env.example .env
```

Основные переменные:

| Переменная | Назначение |
|---|---|
| `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_PORT` | Инициализация и публикация PostgreSQL |
| `DATABASE_URL` | Основное SQLAlchemy-подключение API и worker |
| `MINIO_ROOT_USER`, `MINIO_ROOT_PASSWORD` | Локальная учётная запись MinIO |
| `MINIO_PORT`, `MINIO_CONSOLE_PORT` | Публикуемые порты MinIO |
| `S3_ENDPOINT_URL`, `S3_ACCESS_KEY`, `S3_SECRET_KEY`, `S3_REGION` | S3-клиент приложения |
| `S3_BUCKET` | Bucket оригинальных отчётов; `minio-init` создаёт его при запуске |
| `VALIDATION_BATCH_SIZE` | Максимальный размер batch staging/errors, по умолчанию `500` |
| `WORKER_POLL_INTERVAL_SECONDS` | Пауза между polling-циклами, по умолчанию `2` секунды |
| `WORKER_ADVISORY_LOCK_KEY` | Фиксированный ключ глобального worker-lock |
| `TEST_DATABASE_URL` | Только integration-тесты; БД должна оканчиваться на `_test` и пересоздаётся тестами |

Максимальный размер загрузки зафиксирован в приложении как `1_073_741_824` байта
(1 GiB) и не настраивается через окружение. `.env.example` содержит только локальные
демонстрационные значения; реальные секреты в репозиторий добавлять нельзя.

## Запуск

```bash
docker compose up --build -d
docker compose ps
docker compose run --rm api alembic upgrade head
docker compose run --rm api alembic current
docker compose logs -f api worker
```

Текущая Alembic head: `0002_add_report_checksum`.

Остановка без удаления volumes:

```bash
docker compose down
```

Эквивалентные короткие команды: `make up`, `make migrate`, `make logs`, `make down`.

## Health endpoints

- `GET http://localhost:8000/health/live` подтверждает, что API-процесс отвечает.
- `GET http://localhost:8000/health/ready` проверяет доступность настроенного MinIO
  bucket. Текущая readiness-проверка отдельно не опрашивает PostgreSQL.

## API

Базовый URL: `http://localhost:8000`. Swagger UI доступен по
<http://localhost:8000/docs>, OpenAPI JSON — по <http://localhost:8000/openapi.json>.

| Метод | Путь | Назначение |
|---|---|---|
| `POST` | `/api/v1/reports` | Загрузить multipart-поле `file`, ответ `202 Accepted` |
| `GET` | `/api/v1/reports` | Список отчётов; `status`, `limit`, `offset` |
| `GET` | `/api/v1/reports/{report_id}` | Детали, статус и счётчики |
| `GET` | `/api/v1/reports/{report_id}/errors` | Ошибки отчёта; `limit`, `offset` |
| `GET` | `/api/v1/reports/{report_id}/original` | Потоково скачать исходные байты |
| `GET` | `/api/v1/warehouses` | Склады; `code`, `query`, `limit`, `offset` |
| `GET` | `/api/v1/warehouses/{warehouse_id}` | Один склад |
| `GET` | `/api/v1/products` | Товары; `sku`, `query`, `limit`, `offset` |
| `GET` | `/api/v1/products/{product_id}` | Один товар |
| `GET` | `/api/v1/stocks` | Остатки; `warehouse_id`, `warehouse_code`, `product_id`, `sku`, `include_zero`, `limit`, `offset` |
| `GET` | `/health/live` | Liveness |
| `GET` | `/health/ready` | MinIO bucket readiness |

Списки возвращаются в формате:

```json
{"items": [], "limit": 50, "offset": 0, "total": 0}
```

Безопасный формат API-ошибки:

```json
{"error": {"code": "report_not_found", "message": "Report not found"}}
```

## Статусы отчётов

| Статус | Значение |
|---|---|
| `pending` | Оригинал зарегистрирован и ожидает worker |
| `processing` | Worker валидирует или атомарно применяет отчёт |
| `completed` | Снимок полностью применён |
| `failed` | Сохранена validation/processing/apply error |

`processing` обычно является кратким промежуточным состоянием. Оригинал доступен
для скачивания при любом зарегистрированном статусе, включая `failed`.

## CSV-примеры

Каталог [`examples/csv`](examples/csv) содержит небольшие valid, invalid, UTF-8 BOM,
quoted и snapshot-примеры. Большие файлы намеренно не хранятся в Git.

## Полный curl-сценарий

Следующие вспомогательные функции используют Python внутри уже запущенного API-
контейнера, поэтому локальный `jq` или Python не требуются:

```bash
API_URL=http://localhost:8000

json_field() {
  docker compose exec -T api python -c \
    'import json, sys; print(json.load(sys.stdin)[sys.argv[1]])' "$1"
}

upload_report() {
  file=$1
  response_file=$2
  code=$(curl -sS -o "$response_file" -w '%{http_code}' \
    -F "file=@${file};type=text/csv" "$API_URL/api/v1/reports")
  test "$code" = 202
  json_field id < "$response_file"
}

wait_report() {
  report_id=$1
  expected=$2
  deadline=$(( $(date +%s) + 90 ))
  while test "$(date +%s)" -lt "$deadline"; do
    code=$(curl -sS -o /tmp/report-status.json -w '%{http_code}' \
      "$API_URL/api/v1/reports/$report_id")
    test "$code" = 200 || return 1
    status=$(json_field status < /tmp/report-status.json)
    test "$status" = "$expected" && return 0
    test "$status" = completed -o "$status" = failed && return 1
    sleep 1
  done
  return 1
}
```

### Valid report

Загрузите файл, сохраните JSON-ответ и извлеките новый ID:

```bash
VALID_RESPONSE=/tmp/valid-report.json
VALID_ID=$(upload_report examples/csv/valid_basic.csv "$VALID_RESPONSE")
printf 'report_id=%s\n' "$VALID_ID"
wait_report "$VALID_ID" completed
curl -fsS "$API_URL/api/v1/reports/$VALID_ID"
```

Проверьте созданные каталоги и остатки:

```bash
curl -fsS "$API_URL/api/v1/warehouses"
curl -fsS "$API_URL/api/v1/products"
curl -fsS "$API_URL/api/v1/stocks?warehouse_code=DEMO-BASIC"
```

Скачайте и побайтно сравните оригинал:

```bash
curl -fsS -o /tmp/valid-basic-downloaded.csv \
  "$API_URL/api/v1/reports/$VALID_ID/original"
cmp examples/csv/valid_basic.csv /tmp/valid-basic-downloaded.csv
```

### Invalid report

Upload endpoint принимает оригинал, а worker позднее переводит отчёт в `failed`:

```bash
INVALID_RESPONSE=/tmp/invalid-report.json
INVALID_ID=$(upload_report examples/csv/invalid_negative_quantity.csv "$INVALID_RESPONSE")
printf 'report_id=%s\n' "$INVALID_ID"
wait_report "$INVALID_ID" failed
curl -fsS "$API_URL/api/v1/reports/$INVALID_ID/errors"
curl -fsS -o /tmp/invalid-downloaded.csv \
  "$API_URL/api/v1/reports/$INVALID_ID/original"
cmp examples/csv/invalid_negative_quantity.csv /tmp/invalid-downloaded.csv
```

### Snapshot selective zeroing

Сначала примените исходный снимок двух складов, затем снимок только `DEMO-MSK`:

```bash
SNAPSHOT_INITIAL_ID=$(upload_report \
  examples/csv/valid_snapshot_initial.csv /tmp/snapshot-initial.json)
wait_report "$SNAPSHOT_INITIAL_ID" completed

SNAPSHOT_UPDATE_ID=$(upload_report \
  examples/csv/valid_snapshot_update.csv /tmp/snapshot-update.json)
wait_report "$SNAPSHOT_UPDATE_ID" completed

curl -fsS "$API_URL/api/v1/reports/$SNAPSHOT_UPDATE_ID"
curl -fsS "$API_URL/api/v1/stocks?warehouse_code=DEMO-MSK"
curl -fsS "$API_URL/api/v1/stocks?warehouse_code=DEMO-SPB"
```

У update-отчёта ожидаются `stocks_created=0`, `stocks_updated=2`,
`stocks_zeroed=1`. `DEMO-MSK / SNAP-003` станет `0`, а обе пары `DEMO-SPB`
останутся `5` и `9`.

Автоматизированный valid + invalid сценарий без фиксированных report ID:

```bash
sh scripts/smoke.sh
```

Скрипт имеет ограниченный timeout, проверяет HTTP-коды, остатки, ошибки и `cmp` для
оригиналов обоих отчётов. Он не удаляет отчёты или чужие данные.

## Тесты и качество

Все команды выполняются в application image с реальными PostgreSQL и MinIO из
Compose:

```bash
docker compose run --rm api pytest -q
docker compose run --rm api pytest -q -m unit
docker compose run --rm api pytest -q -m integration
docker compose run --rm api ruff check .
docker compose run --rm api ruff format --check .
docker compose run --rm api mypy app tests
docker compose run --rm api alembic current
docker compose run --rm api alembic check
```

Короткие эквиваленты: `make test`, `make test-unit`, `make test-integration`,
`make lint`, `make format-check`, `make typecheck`, `make check`.

Integration-тесты пересоздают только БД из `TEST_DATABASE_URL`; её имя обязано
оканчиваться на `_test`. Не указывайте основную БД приложения.

## Ключевые бизнес-правила

- CSV должен быть UTF-8 или UTF-8 BOM с разделителем `,`.
- Обязательны `warehouse_code`, `warehouse_name`, `sku`, `product_name`, `quantity`.
- `quantity` — целое число от `0` до максимума PostgreSQL `BIGINT`.
- Повтор `(warehouse_code, sku)` внутри отчёта запрещён.
- Разные названия одного `warehouse_code` или `sku` сами по себе допустимы.
- При upsert побеждает название из логической строки с максимальным `line_number`.
- При любой ошибке отчёт не применяется частично.
- Отсутствующие пары обнуляются только у складов текущего снимка.
- Явные и неявные нули сохраняются строками с `quantity = 0`.

## Ограничения

- Нет авторизации и фронтенда.
- Нет write API для ручного управления складами, товарами и остатками.
- Глобальный FIFO/advisory lock намеренно сериализует все отчёты.
- `UploadFile` может использовать временный диск до начала блочного S3 upload.
- S3 и PostgreSQL не имеют общей транзакции; после неуспешной регистрации
  выполняется best-effort compensating delete незарегистрированного объекта.
- Максимальный входной файл — 1 GiB.
- Полный 1-GiB fixture не хранится в репозитории; большие потоки генерируются тестами.

## Возможные production improvements

Без изменения текущего тестового решения в production потребовались бы:

- аутентификация и авторизация;
- метрики, distributed tracing и alerting;
- lifecycle/retention-политика для S3-объектов;
- резервное копирование PostgreSQL и MinIO;
- rate limiting и upload quotas;
- более масштабируемая стратегия параллельной обработки;
- внешний task queue, только если это оправдано реальной нагрузкой.
