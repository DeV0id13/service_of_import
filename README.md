# Service of Import

Backend-сервис импорта CSV-отчётов с остатками. На текущем этапе доступны
потоковая регистрация оригиналов в MinIO, API чтения отчётов, PostgreSQL-схема,
health endpoints и FIFO worker для потоковой CSV-валидации.

Worker сохраняет валидные строки и ошибки ограниченными batch-ами, затем атомарно
применяет валидный snapshot к складам, товарам и остаткам. Обновление каталогов,
явных остатков, selective zeroing, счётчиков и статуса `completed` выполняется одной
транзакцией PostgreSQL.

## Требования

- Docker с Docker Compose v2;
- для запуска без Docker — Python 3.12.

## Запуск

```bash
cp .env.example .env
docker compose up --build -d
docker compose ps
```

API: <http://localhost:8000>

- `GET /health/live` — процесс API жив;
- `GET /health/ready` — bucket объектного хранилища доступен;
- `POST /api/v1/reports` — потоково сохранить оригинал и создать `pending`;
- `GET /api/v1/reports` — список отчётов;
- `GET /api/v1/reports/{id}` — детали отчёта;
- `GET /api/v1/reports/{id}/errors` — пагинированные ошибки отчёта;
- `GET /api/v1/reports/{id}/original` — потоково скачать оригинал;
- `GET /api/v1/warehouses` и `GET /api/v1/warehouses/{id}` — склады;
- `GET /api/v1/products` и `GET /api/v1/products/{id}` — товары;
- `GET /api/v1/stocks` — денормализованные остатки с фильтрами и пагинацией;
- Swagger UI: <http://localhost:8000/docs>;
- MinIO Console: <http://localhost:9001>.

Пример регистрации:

```bash
curl -F "file=@report.csv;type=text/csv" http://localhost:8000/api/v1/reports
```

Логи:

```bash
docker compose logs -f api worker
```

Остановка без удаления данных:

```bash
docker compose down
```

## Проверки

```bash
docker compose run --rm api ruff check .
docker compose run --rm api ruff format --check .
docker compose run --rm api mypy app tests
docker compose run --rm api pytest -q
docker compose config
docker compose build
```

## Alembic

```bash
docker compose run --rm api alembic upgrade head
docker compose run --rm api alembic current
docker compose run --rm api alembic check
```
