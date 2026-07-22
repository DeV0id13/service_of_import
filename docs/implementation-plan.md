# План реализации

## 1. Подход

Реализация должна быть небольшой и прозрачной для проверяющего. Один Python-проект собирается в один Docker image, из которого запускаются FastAPI API и отдельный worker. PostgreSQL служит БД и очередью, MinIO хранит оригиналы.

На текущем этапе создаётся только план. Код, миграции и Docker-конфигурация не реализуются.

## 2. Предполагаемая структура проекта

```text
.
├── AGENTS.md
├── README.md
├── pyproject.toml
├── alembic.ini
├── Dockerfile
├── compose.yaml
├── .env.example
├── alembic/
│   ├── env.py
│   └── versions/
│       └── 0001_initial_schema.py
├── app/
│   ├── __init__.py
│   ├── main.py                 # FastAPI entrypoint
│   ├── worker.py               # worker entrypoint и polling loop
│   ├── config.py
│   ├── logging.py
│   ├── db.py
│   ├── models.py
│   ├── schemas.py
│   ├── api/
│   │   ├── reports.py
│   │   └── inventory.py
│   └── services/
│       ├── storage.py          # MinIO/S3 streaming
│       ├── validation.py       # CSV -> staging/errors
│       └── apply_report.py     # финальная транзакция
├── tests/
│   ├── conftest.py
│   ├── unit/
│   ├── integration/
│   ├── e2e/
│   └── fixtures/
│       └── csv/
├── examples/
│   └── csv/
└── docs/
    ├── test-assignment.docx
    ├── architecture.md
    └── implementation-plan.md
```

Структура разделяет только реальные ответственности. Отдельный repository/DDD/DI-слой для каждой таблицы не нужен.

## 3. Зависимости

### Runtime

- Python 3.12;
- FastAPI и Uvicorn;
- SQLAlchemy 2.x;
- Alembic;
- `psycopg[binary]`;
- boto3;
- Pydantic Settings;
- `python-multipart`.

### Development

- pytest;
- httpx для API-тестов;
- Ruff;
- mypy.

Не добавлять Celery, Redis, pandas, Testcontainers, отдельный logging framework и другие библиотеки без конкретной необходимости.

## 4. Этапы реализации

### Этап 0. Архитектура — текущий этап

Результат:

- требования и спорные случаи зафиксированы;
- определены модель, API, lifecycle, validation, apply и concurrency;
- создан `AGENTS.md`;
- код приложения отсутствует.

### Этап 1. Каркас и локальная инфраструктура

Работы:

- создать `pyproject.toml`, пакет `app`, настройки Ruff/mypy/pytest;
- добавить Pydantic Settings и `.env.example`;
- создать Dockerfile;
- создать Compose-сервисы `postgres`, `minio`, `minio-init`, `api`, `worker`;
- добавить минимальный FastAPI app и idle worker entrypoint без обработки отчётов;
- настроить базовое stdlib logging.

Проверки:

- image собирается;
- PostgreSQL и MinIO стартуют с healthchecks;
- API и worker запускаются отдельными командами;
- Ruff, format check, mypy и имеющиеся тесты проходят.

Критерий завершения: окружение поднимается без ручной настройки, но бизнес-логика ещё отсутствует.

### Этап 2. Модель данных и Alembic

Работы:

- добавить модели `reports`, `report_errors`, `report_staging_rows`, `warehouses`, `products`, `stock_balances`;
- создать `0001_initial_schema`;
- настроить SQLAlchemy session factory и явные transaction boundaries;
- добавить индексы и CHECK/UNIQUE/FK из архитектуры.

Проверки:

- `alembic upgrade head` на чистой БД;
- `alembic current` и `alembic check`;
- integration-тест ограничений: уникальные коды/пары, неотрицательное quantity, допустимые статусы;
- повторный запуск миграции не меняет схему.

Критерий завершения: рабочая схема создаётся только Alembic, без `create_all()`.

### Этап 3. Загрузка и API отчётов

Работы:

- реализовать S3-адаптер для потоковой загрузки и скачивания;
- `POST /api/v1/reports` принимает `UploadFile`, читает фиксированными блоками и проверяет фактический размер;
- после MinIO upload создаётся `pending`;
- при превышении лимита загрузка прекращается, отчёт не создаётся;
- реализовать список, детали и скачивание оригинала;
- API не валидирует CSV-содержимое.

Проверки:

- сохранённый объект побайтно равен входному файлу;
- код никогда не вызывает `read()` без размера;
- границы размера `limit` и `limit + 1`;
- пустой или неверный CSV всё равно сохраняется и регистрируется;
- оригинал скачивается для любого текущего статуса;
- ошибка MinIO/DB не создаёт ложный `pending`.

Критерий завершения: файл и метаданные надёжно приняты, но предметные данные не меняются.

### Этап 4. FIFO worker, validation и staging

Работы:

- реализовать polling loop worker;
- получать session-level advisory lock на выделенном соединении;
- выбирать отчёты по `created_at ASC, id ASC`;
- восстанавливать прерванный `processing` с начала после очистки его staging/errors;
- читать CSV из MinIO потоково;
- проверять header, значения и quantity;
- сохранять корректные строки и ошибки ограниченными batch-ами;
- после EOF находить SQL-запросом дубли пары `warehouse_code + sku`;
- не считать разные названия одного `warehouse_code` или `sku` validation error;
- при любой validation error переводить отчёт в `failed` без предметных изменений.

Проверки:

- UTF-8 и UTF-8 BOM;
- отсутствующая обязательная колонка;
- пустые поля;
- отрицательное, дробное и слишком большое quantity;
- duplicate pair, в том числе в разных batch;
- разные названия одного склада/товара не приводят к `failed`;
- ошибка в последней строке большого генерируемого потока;
- ошибки содержат line number, field, code/message и raw data;
- два worker: только один получает lock и обрабатывает FIFO;
- failed-report не создаёт даже новые склады/товары и сохраняет оригинал.

Критерий завершения: worker полностью классифицирует отчёт как валидный или невалидный, но валидный отчёт пока не применяется.

### Этап 5. Атомарное применение

Работы:

- реализовать одну явную apply-транзакцию;
- повторно проверить статус отчёта и отсутствие ошибок;
- set-based SQL-запросами сформировать по одному складу и товару на код, выбирая название из строки с максимальным `line_number`;
- upsert складов, товаров и явных остатков;
- обнулить отсутствующие пары только представленных складов;
- вычислить счётчики created/updated/zeroed;
- обновить `completed` в той же транзакции;
- при исключении rollback и отдельной транзакцией сохранить processing error + `failed`;
- после терминального результата очистить staging.

Проверки:

- создание новых складов, товаров и остатков;
- обновление названий и quantities;
- для повторяющегося `warehouse_code`/`sku` с разными названиями применяется название из последней логической CSV-строки;
- selective zeroing: MSK синхронизируется, SPB не меняется;
- явный и неявный ноль;
- точные mutually-exclusive counters;
- неизменившиеся пары не считаются updated;
- исключение после каждого основного SQL-шага откатывает склады, товары, названия и остатки;
- `completed` никогда не виден без соответствующих предметных изменений;
- `failed` и ошибка сохраняются после rollback.

Критерий завершения: выполнены all-or-nothing и snapshot-семантика исходного задания.

### Этап 6. Read API, документация и финальная проверка

Работы:

- завершить `/reports/{id}/errors`;
- добавить `/warehouses`, `/products`, `/stocks` с фильтрами и пагинацией;
- добавить небольшие примеры valid/invalid/snapshot CSV;
- написать README с запуском, миграциями, тестами, quality checks и curl-сценарием;
- проверить Swagger/OpenAPI;
- добавить базовые логи upload/processing/completed/failed.

Проверки:

- статусы и ошибки отчётов доступны через API;
- оригинал скачивается для `pending`, `processing`, `completed`, `failed`;
- фильтры остатков работают отдельно и вместе;
- нулевые остатки возвращаются API;
- полный end-to-end happy path;
- полный end-to-end invalid path с неизменной предметной БД;
- запуск на чистых Docker volumes по README;
- pytest, Ruff, format check и mypy.

Критерий завершения: проверяющий воспроизводит основные сценарии без ручной настройки.

## 5. План миграций

Для нового тестового проекта достаточно одной initial migration `0001_initial_schema`.

Порядок создания:

1. `reports`;
2. `warehouses` и `products`;
3. `stock_balances`;
4. `report_staging_rows`;
5. `report_errors`;
6. индексы очереди, выдачи ошибок и фильтра остатков.

Решения:

- статусы — `VARCHAR + CHECK`, а не PostgreSQL ENUM;
- идентификаторы — `BIGINT IDENTITY`;
- остаток — composite primary key `(warehouse_id, product_id)`;
- staging/errors ссылаются на report через FK;
- pair в staging не UNIQUE, поскольку дубли являются validation errors;
- рабочая схема не создаётся через SQLAlchemy `create_all()`.

Проверка миграции:

- upgrade чистой БД до head;
- проверка всех constraints и indexes;
- `alembic check` против моделей.

Искусственно разбивать ещё не выпущенную greenfield-схему на несколько миграций не нужно. После появления данных изменения оформляются новыми миграциями, а `0001` не переписывается.

## 6. Стратегия тестирования

### Unit

- CSV header и row validation;
- quantity parsing;
- status transitions;
- snapshot/counter rules на малых наборах;
- ограниченное чтение stream;
- безопасный object key и имя скачивания.

### Integration: PostgreSQL + MinIO

- Alembic migration;
- точные байты upload/download;
- staging batches и SQL duplicate check;
- valid apply create/update/zero;
- validation failure без предметных изменений;
- apply rollback;
- FIFO/advisory lock с двумя соединениями;
- report errors и API-фильтры.

Для integration-тестов используются отдельная test database и отдельный MinIO bucket из Docker Compose. Testcontainers как отдельная зависимость не нужен.

### End-to-end

1. Загрузить valid CSV и дождаться `completed`.
2. Проверить каталоги, остатки и счётчики.
3. Скачать и сравнить оригинал.
4. Загрузить invalid CSV и дождаться `failed`.
5. Проверить ошибки, неизменность остатков и доступность оригинала.
6. Загрузить снимок одного склада и проверить selective zeroing.

### Проверка больших файлов

- Не хранить большой fixture в Git.
- Генерировать поток с большим числом строк.
- Проверять, что размер batch/chunk ограничен и не зависит от числа строк.
- Обязательно проверять ошибку в конце большого потока.
- Полный 1-ГиБ прогон можно выполнить вручную перед сдачей в окружении с достаточным диском.

## 7. Ключевые риски

| Риск | Мера |
|---|---|
| S3 upload успешен, INSERT отчёта нет | Уникальный key и best-effort удаление только незарегистрированного объекта |
| Большой multipart занимает временный диск API | Документировать и выделить достаточно места; не читать файл целиком в RAM |
| Большой staging нагружает PostgreSQL | Ограниченные batches, только нужные индексы, cleanup после обработки |
| Два worker одновременно | Один session advisory lock на время отчёта |
| Ошибка в середине apply | Одна транзакция и отдельная failure-транзакция после rollback |
| Неверное обнуление другого склада | Отдельный integration-тест selective zeroing |
| Несколько названий одного кода | Set-based выбор значения из строки с максимальным `line_number` и отдельный integration-тест |

## 8. Готовность будущей реализации

- Проект поднимается через Docker Compose.
- Миграция создаёт схему на чистой БД.
- CSV нигде не загружается целиком в память.
- Оригинал сохраняется независимо от бизнес-валидации.
- Worker обрабатывает отчёты FIFO под advisory lock.
- Предметные изменения выполняются только после полной валидации.
- Финальное применение атомарно.
- Failed-report хранит ошибки и не оставляет частичных изменений.
- Snapshot обнуляет только склады текущего отчёта.
- Все endpoints и команды описаны в README/OpenAPI.
- Все тесты, Ruff и mypy проходят.
