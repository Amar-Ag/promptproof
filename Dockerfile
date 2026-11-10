# Python image for both `api` and `worker`.
#
# One image, two commands: the services share all their code through
# packages/core, so building twice would only duplicate layers. Compose and Fly
# override the command per service.

FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

# curl is used by the container healthcheck, not by the application.
RUN apt-get update \
 && apt-get install --no-install-recommends -y curl \
 && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.12.10 /uv /usr/local/bin/uv

WORKDIR /app

# ── Dependency layer ─────────────────────────────────────────────────────────
# Only the manifests, so a code change does not re-resolve dependencies.
COPY pyproject.toml uv.lock ./
COPY packages/core/pyproject.toml packages/core/
COPY apps/api/pyproject.toml apps/api/
COPY apps/worker/pyproject.toml apps/worker/

RUN uv sync --frozen --no-install-workspace --no-dev

# ── Application layer ────────────────────────────────────────────────────────
COPY packages/ packages/
COPY apps/api/ apps/api/
COPY apps/worker/ apps/worker/
COPY migrations/ migrations/
COPY probes/ probes/
COPY alembic.ini ./
COPY ops/docker/entrypoint-api.sh /usr/local/bin/entrypoint-api

RUN uv sync --frozen --no-dev \
 && chmod +x /usr/local/bin/entrypoint-api

# Run as a non-root user. Nothing in this image needs to write to the filesystem.
RUN useradd --create-home --uid 10001 promptproof \
 && chown -R promptproof:promptproof /app
USER promptproof

EXPOSE 8000

# The API is the default; compose overrides this for the worker.
CMD ["entrypoint-api"]
