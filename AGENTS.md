# AGENTS.md

Entry point for any agent — or human — doing work in this repository. Read this first, then
the specific document your task points you at. Do not infer conventions from the code when a
document states them; the documents are the source of truth and the code is expected to match.

## What this project is

**promptproof** runs adversarial prompt-injection and jailbreak probes against an LLM system
prompt or an LLM application endpoint, and reports which attacks succeeded, how severe each
one is, and what to change. The user-facing description lives in [README.md](README.md); the
behaviour you are allowed to change lives in [docs/specs](docs/specs).

It is also a course submission for the DataTalksClub AI Dev Tools Zoomcamp, which means the
process artifacts — the spec, this file, the task tracker, the runbooks — are deliverables in
their own right, not scaffolding. Keep them current as you change code.

## Modular documentation

| Read this | When |
|---|---|
| [docs/specs](docs/specs) | Before writing any feature code. Specs carry the acceptance criteria your work is judged against. |
| [docs/architecture.md](docs/architecture.md) | Before changing service boundaries, the data model, or how trace context moves between processes. |
| [docs/agents](docs/agents) | To know which role you are holding, what you own, and who you hand off to. |
| [docs/process.md](docs/process.md) | Before opening a branch or a pull request. Defines issue-first flow and definition of done. |
| [docs/runbooks](docs/runbooks) | When operating or recovering the system, including as the on-call agent. |
| [docs/adr](docs/adr) | To understand why a decision was made before you reverse it. Add a new record rather than editing history. |
| [docs/observability.md](docs/observability.md) | Before adding a span, metric, or log line. Defines the naming scheme and the scrubbing rules. |

## Repository layout

```
promptproof/
├── apps/
│   ├── api/          FastAPI service: HTTP API, persistence, migrations
│   ├── worker/       Scan executor: claims queued scans, runs probes, judges results
│   └── web/          React + TypeScript frontend (Vite)
├── packages/
│   └── core/         Shared Python domain code: models, probe loading, scoring, telemetry
│       └── ...oncall/  The on-call responder: its tools, its report, its issues
├── probes/           The attack corpus, as versioned YAML. Reviewed like code.
├── docs/             Specs, architecture, agent team, process, runbooks, ADRs
├── ops/
│   └── docker/       Container entrypoints
├── tests/
│   ├── unit/         No I/O, no containers, milliseconds
│   ├── integration/  Real Postgres, stubbed model provider
│   └── e2e/          Playwright, full stack via Compose
├── compose.yaml      The whole system, one command
└── Makefile          The only interface you need to run anything
```

## Commands

Use the Makefile. Do not invent alternative invocations in docs, CI, or your own notes —
if something is missing, add a target.

| Command | Does |
|---|---|
| `make up` | Build and start the full system including Postgres, apply migrations |
| `make down` | Stop everything, keep the volume |
| `make reset` | Stop everything and destroy the database volume |
| `make logs` | Tail all service logs |
| `make test` | Unit + integration + frontend tests. Must pass before any PR. |
| `make test-unit` | Unit tests only. Fast loop while developing. |
| `make e2e` | Playwright end-to-end journey against a running stack |
| `make lint` | Ruff, mypy, eslint, tsc |
| `make fmt` | Format everything in place |
| `make migrate` | Apply Alembic migrations |
| `make migration m="..."` | Generate a new migration |

## Hard rules

These are non-negotiable. A change that breaks one of them should be rejected in review
regardless of how well it works.

1. **Issue first.** Every change traces to an issue with acceptance criteria. Reference it in
   the branch name and the PR body. See [docs/process.md](docs/process.md).
2. **No secrets or user content in telemetry.** System prompts, target responses, endpoint
   credentials and API keys must never appear in a log, span attribute, metric label, or
   exception message. Route everything through the scrubber in `packages/core/telemetry`.
3. **No probe payload executes locally.** Probe content is data sent to a model. It is never
   evaluated, templated into a shell command, or interpolated into SQL.
4. **Endpoint targets are guarded.** Any outbound request to a user-supplied URL goes through
   the SSRF guard. Loopback, link-local, private and multicast ranges are refused, redirects
   are re-validated, and DNS is resolved once and pinned.
5. **Migrations are forward-only.** Never edit an applied migration; add a new one.
6. **Tests accompany the change.** A behaviour change without a test that would have caught
   the old behaviour is incomplete.
7. **The trace ID survives the queue.** Work that crosses the API/worker boundary must
   propagate trace context through the scan row. Breaking this breaks a graded requirement.
8. **Keep `make up` to one command.** If a change needs a manual step, automate it or put it
   in the entrypoint.

## Technology, and what not to change casually

| Layer | Choice | Locked because |
|---|---|---|
| API | FastAPI + SQLAlchemy 2 + Alembic | Async-native, and the OTel instrumentation story is mature |
| Queue | Postgres `FOR UPDATE SKIP LOCKED` | A deliberate decision to avoid Redis. See [ADR-0002](docs/adr/0002-postgres-job-queue.md). |
| DB | Postgres 16 | Runs in Compose, matches Fly managed Postgres |
| Frontend | React 18 + TypeScript + Vite | Separate container, which is what makes the frontend/backend split demonstrable |
| Models | Groq | Free tier, fast; target and judge use different model families on purpose |
| Telemetry | OpenTelemetry SDK, exported to Logfire | Vendor-neutral instrumentation, hosted UI |
| Tests | pytest, Vitest, Playwright | — |
| Deploy | Render free tier, two services, with Neon Postgres | No free tier offers an always-on worker, so the worker is co-located. See [ADR-0005](docs/adr/0005-free-tier-deployment.md). |

Changing a locked row needs an ADR in [docs/adr](docs/adr) before the code change.

## Current state

Phase 2 — the product. The walking skeleton is complete: the four services come up with one
command, targets are registered and versioned, scans are queued and claimed off the Postgres
queue, and the report renders.

Probes now really run: the target is called, an independent judge decides each outcome
against the probe's declared criteria, and the scan is graded. Still open in this phase are
the probe corpus beyond its six seed probes (#10, #11) and the SSRF guard with endpoint
targets (#17).

The build order and what each phase delivers is in
[docs/process.md](docs/process.md#build-phases).
