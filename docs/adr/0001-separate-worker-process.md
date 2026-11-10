# ADR-0001 — Scans execute in a separate worker process

- **Status:** Accepted
- **Date:** 2026-11-09
- **Deciders:** architect, human

## Context

A scan runs 40+ probes, each making a target call and a judge call. At realistic latencies that
is minutes of wall time. The HTTP request that starts a scan cannot wait for it.

Three options were on the table: run it in a FastAPI `BackgroundTask`, run it in an in-process
asyncio task pool, or run it in a dedicated process.

## Decision

Scans execute in a dedicated `worker` process. The API only validates, persists and enqueues.

## Consequences

**Good**

- API latency is independent of model latency, which keeps `POST /api/scans` under 300 ms.
- The worker can be restarted, scaled or crashed without affecting request serving.
- Scan work survives an API deploy — a rolling restart does not abandon in-flight scans,
  because the lease expires and another worker reclaims them.
- It forces trace context to cross a process boundary, which is the honest version of the
  observability problem rather than a single-process shortcut.

**Bad**

- One more container in Compose and one more process group on Fly.
- Trace propagation now requires deliberate work (see
  [architecture.md](../architecture.md#trace-propagation-across-the-queue)).
- Local debugging spans two log streams.

## Alternatives rejected

- **`BackgroundTask`** — dies with the request's worker process on deploy or crash, with no
  recovery and no visibility. Unacceptable for work measured in minutes.
- **In-process task pool** — model latency and retries would compete with request serving for
  the event loop, and a scan would still be lost on restart.
