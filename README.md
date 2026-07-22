# Service of Import

Каркас backend-сервиса импорта CSV-отчётов с остатками. На текущем этапе доступны
FastAPI health endpoints, пустой worker-процесс, PostgreSQL и MinIO в Docker Compose,
а также инфраструктура Alembic без миграций.

Предметные модели, загрузка CSV, S3-адаптер и бизнес-логика worker ещё не реализованы.

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
- `GET /health/ready` — каркас запущен с валидной конфигурацией;
- Swagger UI: <http://localhost:8000/docs>;
- MinIO Console: <http://localhost:9001>.

На первом этапе доступность PostgreSQL и MinIO контролируется healthcheck-ами Compose.
Проверки зависимостей из `/health/ready` будут добавлены вместе с соответствующими
адаптерами на следующих этапах.

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

Инфраструктура Alembic создана, но revisions и предметная схема отсутствуют.

```bash
docker compose run --rm api alembic current
```

Initial migration появится только на следующем этапе реализации.
