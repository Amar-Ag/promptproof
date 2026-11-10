#!/usr/bin/env bash
# API entrypoint: migrate, then serve.
#
# Migrations run here rather than in application code, so the schema is current
# before the first request is served and `make up` stays a single command
# (R-004-1). Alembic is idempotent, so starting twice is harmless.
#
# Compose already gates startup on the database's own healthcheck, so this retry
# exists only to cover the gap between "accepting connections" and "ready to
# serve" — and to make the failure legible if the database never arrives.
set -euo pipefail

attempt=1
max_attempts=12

until alembic upgrade head; do
    if (( attempt >= max_attempts )); then
        echo "[entrypoint] migrations failed after ${max_attempts} attempts" >&2
        exit 1
    fi
    echo "[entrypoint] migration attempt ${attempt} failed, retrying in 5s"
    attempt=$(( attempt + 1 ))
    sleep 5
done

echo "[entrypoint] migrations applied; starting api"
exec uvicorn promptproof_api.main:app \
    --host 0.0.0.0 \
    --port "${PORT:-8000}" \
    --proxy-headers
