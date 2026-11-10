# The only interface you need to run anything.
#
# If a command you want is missing, add a target here rather than documenting a
# raw invocation (AGENTS.md).

COMPOSE := docker compose
WEB     := $(COMPOSE) run --rm --no-deps web
# The test image: the same base the services run on, plus dev dependencies and
# the tests themselves. The runtime image carries neither, on purpose.
TESTS   := $(COMPOSE) --profile test run --rm tests
# Same image without waiting on Postgres, for things that need no database.
TESTS_X := $(COMPOSE) --profile test run --rm --no-deps tests

# Git Bash on Windows rewrites arguments that look like absolute POSIX paths
# into Windows paths, which turns `docker run -w /w` into `-w W:/` and fails.
# Unset elsewhere, this variable does nothing.
export MSYS_NO_PATHCONV := 1

.DEFAULT_GOAL := help
.PHONY: help up down reset logs ps shell psql test test-unit test-integration test-web \
        e2e lint fmt migrate migration lock web-lock

help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
	 | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

# ── Running ──────────────────────────────────────────────────────────────────

up: ## Build and start the full system, apply migrations, wait until ready
	$(COMPOSE) up --build --wait --wait-timeout 240
	@echo ""
	@echo "  UI       http://localhost:$${WEB_PORT:-5173}"
	@echo "  API docs http://localhost:$${API_PORT:-8000}/docs"
	@echo ""

down: ## Stop everything, keep the data
	$(COMPOSE) down

reset: ## Stop everything and destroy the database volume
	$(COMPOSE) down -v

logs: ## Tail all service logs
	$(COMPOSE) logs -f --tail=100

ps: ## Show service status
	$(COMPOSE) ps

shell: ## A shell in the api container
	$(COMPOSE) exec api bash

psql: ## A psql prompt against the running database
	$(COMPOSE) exec db psql -U promptproof -d promptproof

# ── Verifying ────────────────────────────────────────────────────────────────

test: test-unit test-integration test-web ## Unit + integration + frontend tests

test-unit: ## Unit tests only (no I/O, fast)
	$(TESTS_X) uv run --frozen pytest tests/unit -q

test-integration: ## Integration tests against a real Postgres
	$(COMPOSE) up -d --wait db
	$(TESTS) uv run --frozen pytest tests/integration -q

test-web: ## Frontend tests
	$(WEB) npm run test

e2e: ## Playwright end-to-end journey against a running stack
	$(WEB) npm run e2e

# ── Quality ──────────────────────────────────────────────────────────────────

lint: ## Ruff, mypy, eslint, tsc
	$(TESTS_X) uv run --frozen ruff check .
	$(TESTS_X) uv run --frozen ruff format --check .
	$(TESTS_X) uv run --frozen mypy packages apps
	$(WEB) npm run lint
	$(WEB) npm run typecheck

fmt: ## Format everything in place
	$(TESTS_X) uv run --frozen ruff format .
	$(TESTS_X) uv run --frozen ruff check --fix .

# ── Database ─────────────────────────────────────────────────────────────────

migrate: ## Apply migrations
	$(COMPOSE) run --rm api alembic upgrade head

migration: ## Generate a migration: make migration m="add widgets"
	@test -n "$(m)" || (echo 'usage: make migration m="what it does"' && exit 1)
	$(COMPOSE) run --rm api alembic revision --autogenerate -m "$(m)"

# ── Lockfiles ────────────────────────────────────────────────────────────────
# Generated inside containers, so neither uv nor Node has to be installed on the
# host — Docker is the only prerequisite.

lock: ## Regenerate uv.lock
	docker run --rm -v "$(CURDIR)":/w -w /w ghcr.io/astral-sh/uv:0.12.10-python3.12-bookworm-slim \
		uv lock

web-lock: ## Regenerate apps/web/package-lock.json
	docker run --rm -v "$(CURDIR)/apps/web":/w -w /w node:22-alpine \
		npm install --package-lock-only
