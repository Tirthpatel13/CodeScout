.DEFAULT_GOAL := help
BACKEND := backend
PY := $(BACKEND)/.venv/bin/python
PIP := $(BACKEND)/.venv/bin/pip

.PHONY: help
help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}'

# --- setup -----------------------------------------------------------------
.PHONY: venv
venv: ## Create the backend virtualenv and install dependencies
	cd $(BACKEND) && python3 -m venv .venv && .venv/bin/pip install -U pip && .venv/bin/pip install -e ".[dev]"

.PHONY: up
up: ## Start Postgres, Redis, the API and the worker
	docker compose up -d --build

.PHONY: down
down: ## Stop everything
	docker compose down

.PHONY: logs
logs: ## Tail the API and worker logs
	docker compose logs -f api worker

# --- database --------------------------------------------------------------
.PHONY: migrate
migrate: ## Apply migrations
	cd $(BACKEND) && .venv/bin/alembic upgrade head

.PHONY: migration
migration: ## Create a migration: make migration m="add foo"
	cd $(BACKEND) && .venv/bin/alembic revision --autogenerate -m "$(m)"

.PHONY: reset-db
reset-db: ## Drop and recreate the dev database
	docker compose exec -T db psql -U postgres -c "DROP DATABASE IF EXISTS codescout" -c "CREATE DATABASE codescout"
	$(MAKE) migrate

# --- quality ---------------------------------------------------------------
.PHONY: test
test: ## Run the test suite
	cd $(BACKEND) && .venv/bin/python -m pytest -q

.PHONY: cov
cov: ## Run tests with coverage
	cd $(BACKEND) && .venv/bin/python -m pytest --cov=app --cov-report=term-missing

.PHONY: lint
lint: ## Lint and type-check
	cd $(BACKEND) && .venv/bin/ruff check app tests && .venv/bin/ruff format --check app tests

.PHONY: fmt
fmt: ## Auto-format
	cd $(BACKEND) && .venv/bin/ruff check --fix app tests && .venv/bin/ruff format app tests

.PHONY: typecheck
typecheck: ## mypy
	cd $(BACKEND) && .venv/bin/mypy app

# --- evals -----------------------------------------------------------------
.PHONY: eval
eval: ## Run the full eval suite
	cd $(BACKEND) && .venv/bin/python -m app.evals.runner --cases evals/cases.yaml

.PHONY: eval-smoke
eval-smoke: ## Run the 15-case CI subset with the regression gate
	cd $(BACKEND) && .venv/bin/python -m app.evals.runner --cases evals/cases.yaml --smoke --fail-on-regression

.PHONY: eval-baseline
eval-baseline: ## Record the current scores as the baseline
	cd $(BACKEND) && .venv/bin/python -m app.evals.runner --cases evals/cases.yaml --write-baseline

# --- running locally -------------------------------------------------------
.PHONY: api
api: ## Run the API with reload
	cd $(BACKEND) && .venv/bin/uvicorn app.main:app --reload --port 8000

.PHONY: worker
worker: ## Run the ingestion worker
	cd $(BACKEND) && .venv/bin/arq app.worker.WorkerSettings
