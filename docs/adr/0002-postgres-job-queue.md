# ADR-0002 — Postgres `SKIP LOCKED` as the job queue, not Redis

- **Status:** Accepted
- **Date:** 2026-11-09
- **Deciders:** architect, human

## Context

With scan execution moved to a worker ([ADR-0001](0001-separate-worker-process.md)), the two
processes need a durable queue with at-least-once delivery, recovery of work abandoned by a
dead worker, and observable depth and age — queue age is the primary alerting signal.

Expected throughput is single-digit scans per minute, forever. This is a correctness and
observability problem, not a throughput problem.

## Decision

The `scans` table is the queue. Work is claimed with a single statement using
`SELECT ... FOR UPDATE SKIP LOCKED`, with a lease (`locked_at`, `locked_by`) and an attempt
counter for recovery.

## Consequences

**Good**

- One fewer service to run, deploy, secure and monitor; Compose stays at four containers.
- No dual-write problem: the queue row and the scan row are the same row, so the queue cannot
  disagree with the data about what state a scan is in.
- Queue depth and oldest-age are plain SQL, which the on-call agent can query directly without
  a broker-specific client.
- Enqueue is transactional with the insert — a scan is never enqueued but unpersisted, or
  persisted but unenqueued.
- Lease expiry gives free recovery of work from a crashed worker, and a realistic failure mode
  to build the on-call agent against.

**Bad**

- Polling, so claim latency is bounded by the poll interval rather than being instant.
- Claim load falls on the primary database. Irrelevant at this scale, a problem at a much
  larger one.
- `SKIP LOCKED` semantics are less familiar to a reader than `BRPOP`, so the claim statement
  needs its comment and its concurrency test.

## Alternatives rejected

- **Redis + arq/Celery** — a second persistence model and a second failure mode in exchange for
  throughput headroom this workload will never use, and it would re-introduce the dual-write
  problem between queue and database.
- **Fly-native or cloud queue** — ties local development to a hosted service and breaks the
  one-command Compose requirement.

## Revisit when

Sustained enqueue rate exceeds roughly 50/second, or claim contention becomes measurable in
`scan.claim` span duration.
