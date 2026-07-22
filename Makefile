COMPOSE ?= docker compose

.PHONY: up down logs migrate test test-unit test-integration lint format-check typecheck check

up:
	$(COMPOSE) up --build -d

down:
	$(COMPOSE) down

logs:
	$(COMPOSE) logs -f api worker

migrate:
	$(COMPOSE) run --rm api alembic upgrade head

test:
	$(COMPOSE) run --rm api pytest -q

test-unit:
	$(COMPOSE) run --rm api pytest -q -m unit

test-integration:
	$(COMPOSE) run --rm api pytest -q -m integration

lint:
	$(COMPOSE) run --rm api ruff check .

format-check:
	$(COMPOSE) run --rm api ruff format --check .

typecheck:
	$(COMPOSE) run --rm api mypy app tests

check: lint format-check typecheck test
