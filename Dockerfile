# Python image for both `api` and `worker`.
#
# Stages are targeted explicitly by compose and by the deploy workflows. They
# have to be: a stage added at the end of this file becomes the default build
# target, and a service with no `target` would silently build the wrong one.
#
#   runtime  api and worker under Compose
#   test     adds dev dependencies and the tests
#   serve    one container running both processes, for deployment
#
# One image, two commands: the services share all their code through
# packages/core, so building twice would only duplicate layers. Compose and Fly
# override the command per service.

FROM python:3.12-slim AS runtime

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

# ── Test stage ───────────────────────────────────────────────────────────────
# The runtime image deliberately carries neither dev dependencies nor tests.
# This stage adds both, so `make test` runs the suite in the same environment
# the services run in rather than against whatever the host happens to have.
FROM runtime AS test

USER root
COPY tests/ tests/
# Not in the runtime image, but it is still code that lint should cover.
COPY scripts/ scripts/
RUN uv sync --frozen  && chown -R promptproof:promptproof /app /opt/venv
USER promptproof

CMD ["uv", "run", "--frozen", "pytest", "-q"]

# ── Frontend build ───────────────────────────────────────────────────────────
# Only used by the `serve` stage below. Compose builds `runtime` and never
# touches this, so local builds do not pay for a Node toolchain.
FROM node:22-alpine AS web-build

WORKDIR /web
COPY apps/web/package.json apps/web/package-lock.json ./
RUN npm ci

COPY apps/web/ ./
# Empty base URL means the client issues same-origin relative requests, which
# is the whole point of the API serving the UI: one public URL, no CORS.
ENV VITE_API_BASE_URL=""
RUN npm run build

# ── Deployment image ─────────────────────────────────────────────────────────
# One container running both processes, for the free tier (ADR-0005). Compose
# does not use this stage; it runs api and worker as separate services.
FROM runtime AS serve

USER root
COPY --from=web-build /web/dist /app/static
COPY ops/docker/entrypoint-serve.sh /usr/local/bin/entrypoint-serve
RUN chmod +x /usr/local/bin/entrypoint-serve  && chown -R promptproof:promptproof /app/static
USER promptproof

# Read by the API to decide whether to mount the UI. Set here rather than left
# to the platform, because this stage is the reason it exists.
ENV FRONTEND_DIR=/app/static

CMD ["entrypoint-serve"]
