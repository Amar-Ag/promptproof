#!/usr/bin/env bash
# Single-container entrypoint for the free deployment tier (ADR-0005).
#
# Runs the migrations, then the worker and the API side by side as two separate
# processes. They are not merged into one: the worker keeps its own lifecycle,
# so trace context still crosses a real process boundary through
# `scans.traceparent` and the queue, lease and reclaim paths all still do work.
#
# Under Compose these are two containers. Here they share one, because no free
# tier offers an always-on background worker.
set -euo pipefail

: "${PORT:=8000}"

log() { echo "[entrypoint] $*"; }

# ── Migrate before serving ───────────────────────────────────────────────────
# A release step, not a background task: the new instance brings the schema
# forward before it accepts a single request. If this fails, nothing starts,
# which is the correct outcome — serving against the wrong schema is worse than
# not serving.
attempt=1
max_attempts=5
until alembic upgrade head; do
    if (( attempt >= max_attempts )); then
        log "migrations failed after ${max_attempts} attempts; refusing to serve"
        exit 1
    fi
    log "migration attempt ${attempt} failed, retrying in 5s"
    attempt=$(( attempt + 1 ))
    sleep 5
done
log "migrations applied"

# ── Run both processes ───────────────────────────────────────────────────────
# If either exits, the container exits. A worker that dies silently while the
# API keeps answering would leave scans queueing forever with the service
# looking healthy — the exact failure the stuck-queue runbook exists for, made
# invisible.
terminate() {
    log "shutting down"
    kill -TERM "${worker_pid}" "${api_pid}" 2>/dev/null || true
    wait || true
    exit 0
}
trap terminate SIGTERM SIGINT

promptproof-worker &
worker_pid=$!
log "worker started (pid ${worker_pid})"

uvicorn promptproof_api.main:app \
    --host 0.0.0.0 \
    --port "${PORT}" \
    --proxy-headers \
    --forwarded-allow-ips '*' &
api_pid=$!
log "api started (pid ${api_pid}) on port ${PORT}"

# Return as soon as either process exits, and carry its status out.
wait -n "${worker_pid}" "${api_pid}"
status=$?
log "a process exited with status ${status}; stopping the container"
kill -TERM "${worker_pid}" "${api_pid}" 2>/dev/null || true
wait || true
exit "${status}"
