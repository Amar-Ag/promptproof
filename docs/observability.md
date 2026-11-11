# Observability

What we emit, what it is called, what must never appear in it, and how to read it. Read this
before adding a span, a metric, or a log line.

## Principle

The question telemetry has to answer is *"is a user waiting longer than they should, and
where is the time going?"* Everything below exists to answer that. Signals that do not help
answer it are not added.

## Stack

OpenTelemetry SDK in both `api` and `worker`, exporting OTLP to **Logfire**, which provides the
UI for traces, metrics and logs. The instrumentation is vendor-neutral: pointing
`OTEL_EXPORTER_OTLP_ENDPOINT` at a local collector works without code changes.

Telemetry is best-effort. The exporter being unreachable must never fail a request or a scan.

## Traces

One trace per scan, spanning two processes.

| Span | Process | Attributes |
|---|---|---|
| `scan.submit` | api | `scan.id`, `target.id`, `target.version`, `target.kind`, `suite.count`, `probe.count` |
| `scan.claim` | worker | `scan.id`, `worker.id`, `scan.attempt`, `queue.wait_ms` |
| `scan.execute` | worker | `scan.id`, `probe.count`, `scan.concurrency` |
| `probe.execute` | worker | `probe.id`, `probe.suite`, `probe.severity`, `finding.outcome` |
| `target.invoke` | worker | `target.kind`, `model.id`, `response.length`, `retry.count` |
| `judge.evaluate` | worker | `judge.model`, `verdict`, `confidence`, `retry.count` |

The link between the two processes is the W3C trace context persisted on
`scans.traceparent` at submit time and extracted at claim time. `queue.wait_ms` makes the
queue wait legible as a measured quantity rather than an unexplained gap.

## Metrics

| Metric | Type | Labels |
|---|---|---|
| `promptproof.scans.submitted` | counter | `target.kind` |
| `promptproof.scans.completed` | counter | `status`, `grade` |
| `promptproof.scan.duration` | histogram (ms) | `status` |
| `promptproof.probe.duration` | histogram (ms) | `suite`, `outcome` |
| `promptproof.target.errors` | counter | `reason` |
| `promptproof.judge.errors` | counter | `reason` |
| `promptproof.queue.depth` | observable gauge | `status` |
| `promptproof.queue.oldest_age_seconds` | observable gauge | — |
| `promptproof.scan.reclaims` | counter | `reason` |

`queue.oldest_age_seconds` is the primary alerting signal: it is the closest available proxy
for "how long has a real user been waiting".

## Logs

Structured JSON, one event per line, emitted through the shared logger in
`packages/core/telemetry`. Every line carries `trace_id`, `span_id`, `service`, `env`, and
`scan_id` where one is in scope, so logs join to traces and metrics on one identifier.

Levels: `error` for a failed scan or an exhausted retry; `warning` for a reclaim or a retried
call; `info` for lifecycle transitions; `debug` is off outside development.

## Scrubbing: what must never be emitted

Hard rule, enforced by the scrubber and by a test.

**Never in any span attribute, log field, metric label, or exception message:**

- system prompt text, in whole or in part
- target responses, in whole or in part
- probe payloads (safe in the repository, noise and confusion in telemetry)
- judge rationale text
- endpoint auth headers, API keys, connection strings
- endpoint URLs beyond scheme and host

**Emit instead:** identifiers, lengths, counts, latencies, outcomes, model names, truncated
reason codes. `response.length` tells you what you need; `response.text` is a leak.

Evidence that a reviewer needs — excerpts and rationales — lives in the database, behind the
API, never in telemetry.

## Service level objective

95% of scans complete within 180 seconds of submission, measured over 7 days, with the
default suite selection. Alerting is derived from this, not from resource usage.

## Alerts

| Alert | Fires when | Means | Runbook |
|---|---|---|---|
| `scan-queue-stalled` | `queue.oldest_age_seconds > 120` sustained 5 min | Users are watching a scan that is not progressing | [stuck-queue.md](runbooks/stuck-queue.md) |
| `scan-failure-rate-high` | failed/total > 20% over 10 min | Scans are completing with errors instead of reports | [scan-failures.md](runbooks/scan-failures.md) |

Both deliver to a human channel and to the on-call agent endpoint. Neither is defined on CPU,
memory, or disk. An alert without a runbook is deleted rather than muted.

## Where the signals go

Traces are exported over OTLP to **Logfire**, at
`https://logfire-api.pydantic.dev/v1/traces`, authenticated by `LOGFIRE_TOKEN`.
Open the project at <https://logfire.pydantic.dev> and filter by service:

| Service name | Emits |
|---|---|
| `promptproof-api` | `scan.submit` |
| `promptproof-worker` | `scan.claim`, `scan.execute`, `probe.execute`, `target.invoke`, `judge.evaluate` |

Both appear under the `promptproof` namespace, with
`deployment.environment.name` set to `local`, `dev` or `production` — so one
project holds every environment and they are told apart by attribute rather
than by having three projects.

A scan's trace can be found from its row: `scans.traceparent` holds the W3C
context, and the middle segment is the trace ID.

```sql
SELECT id, split_part(traceparent, '-', 2) AS trace_id FROM scans ORDER BY queued_at DESC LIMIT 5;
```

Nothing here is required for the application to run. With `LOGFIRE_TOKEN`
unset, spans are still created and context still crosses the queue — they
simply go nowhere, which is what the test suite runs against.

## Reading the signals

| Question | Where |
|---|---|
| Why was this scan slow? | Open its trace; compare `queue.wait_ms` against the `probe.execute` spans |
| Is the queue backing up? | `promptproof.queue.depth` and `queue.oldest_age_seconds` |
| Is the judge or the target failing? | `promptproof.judge.errors` vs `promptproof.target.errors` by reason |
| What did the on-call agent do? | The issue it filed, plus its run's trace |
