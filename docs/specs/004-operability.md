# Spec 004 — Operability

Running, verifying, shipping and operating the system. These requirements are specified with
the same rigour as the product features, because for this project they *are* features.

---

## R-004-1 — One-command local stack · MUST

**Story.** As a reviewer with Docker and nothing else installed, I want to clone this and have
it running without reading a wiki.

**Behaviour.** `make up` builds and starts `web`, `api`, `worker` and `db`, applies migrations,
and waits until the API is healthy. No manual step, no separate migration command, no
hand-created database.

**Acceptance criteria**

- [ ] From a clean clone with only `cp .env.example .env` and a Groq key added, `make up`
      brings the stack to a state where the UI loads and a scan can be submitted.
- [ ] Postgres runs as a Compose service — no external or hosted database is required.
- [ ] Migrations are applied automatically on `api` startup, and starting twice is harmless.
- [ ] `api` and `worker` wait for Postgres to be genuinely ready, not merely for the port to
      open.
- [ ] `GET /healthz` reports liveness and `GET /readyz` reports database reachability and
      corpus validity.
- [ ] `make down` stops everything and preserves data; `make reset` destroys the volume.
- [ ] The documented commands in `AGENTS.md` all exist in the Makefile and all work.

---

## R-004-2 — Verification behind one command · MUST

**Story.** As a reviewer, I want to run the tests without learning the project's layout.

**Behaviour.** `make test` runs unit, integration and frontend tests and exits non-zero on any
failure. `make e2e` runs the Playwright journey against a running stack. No test calls a real
model provider or the public internet.

**Acceptance criteria**

- [ ] `make test` runs all three suites and reports a single pass/fail.
- [ ] Unit tests perform no I/O and complete in under 10 seconds.
- [ ] Integration tests run against a real Postgres started by the test harness, with a stubbed
      model provider.
- [ ] No test makes an outbound network request — asserted by a fixture that fails on any
      unstubbed socket connection.
- [ ] `make e2e` covers the full journey: create a target, run a scan, read the report.
- [ ] Tests pass from a clean clone on a machine with only Docker installed.
- [ ] Each new test is demonstrated to fail against the pre-change code, recorded in the PR.

---

## R-004-3 — CI on every pull request · MUST

**Behaviour.** A GitHub Actions workflow runs lint, type checks, unit, integration and frontend
tests on every pull request and on pushes to `main`. A red build blocks merge.

**Acceptance criteria**

- [ ] The workflow triggers on `pull_request` and on `push` to `main`.
- [ ] It runs lint, type check, unit, integration and frontend tests.
- [ ] A deliberately broken test produces a failing check — demonstrated once and linked in the
      PR that adds the workflow.
- [ ] `main` is protected: the check is required and the branch cannot be pushed to directly.
- [ ] No secret is required for CI to run the test suites.

---

## R-004-4 — Continuous delivery with a gated production step · MUST

**Behaviour.** Merging to `main` deploys automatically to `promptproof-dev`. Production is a
separate, manually triggered workflow requiring an environment approval. Rollback is documented
and exercised at least once.

**Acceptance criteria**

- [ ] A merge to `main` deploys to dev with no human action, and the dev URL serves the new
      build.
- [ ] Deploying to production requires a manual trigger and an approval on a protected
      environment.
- [ ] The production workflow refuses to run unless CI passed on that commit.
- [ ] Migrations run as a release step before the new version takes traffic.
- [ ] [docs/runbooks/rollback.md](../runbooks/rollback.md) gives exact commands, and a rollback
      has been performed once with the outcome recorded in the runbook.
- [ ] Both environments have separate databases and separate credentials.

---

## R-004-5 — Telemetry on the scan journey · MUST

**Behaviour.** The scan journey is instrumented with OpenTelemetry: traces spanning API and
worker, metrics, and structured logs — all correlated by one trace ID. Secrets and user content
are scrubbed before export. Signals are collected and viewable in a UI.

**Acceptance criteria**

- [ ] A submitted scan produces one trace covering `scan.submit`, `scan.claim`,
      `scan.execute`, and per probe `probe.execute` with `target.invoke` and `judge.evaluate`
      beneath it.
- [ ] The worker's spans share the trace ID of the submitting API request, carried through
      `scans.traceparent` — asserted by an integration test reading exported spans.
- [ ] Every log line emitted while handling a scan carries that trace ID and span ID.
- [ ] Metrics exist for scans submitted, scans completed by status, scan duration, probe
      duration by suite and outcome, queue depth, and queue oldest-age.
- [ ] A test asserts that a known system prompt, a known target response and a known auth
      header do not appear anywhere in captured spans, logs or metric labels.
- [ ] Signals are viewable in a hosted UI, and [docs/observability.md](../observability.md)
      records where and how to read them.
- [ ] Telemetry failure never fails a request: with the exporter unreachable, scans still
      complete.

---

## R-004-6 — Alert on user impact · MUST

**Behaviour.** At least one alert fires on a user-visible symptom, not on resource usage. The
primary signal is queue age: scans waiting longer than the SLO means users are staring at a
pending scan.

**Acceptance criteria**

- [ ] An alert fires when the oldest pending scan has waited longer than 120 seconds,
      sustained over 5 minutes.
- [ ] A second alert fires when the scan failure rate exceeds 20% over 10 minutes.
- [ ] Each alert names the user-visible symptom and links the runbook that resolves it.
- [ ] No alert is defined on CPU, memory or disk as a primary signal.
- [ ] Each alert has been fired deliberately once, by stopping the worker with scans queued,
      and the result is recorded in the runbook.
- [ ] Alert delivery reaches the on-call agent endpoint and a human channel.

---

## R-004-7 — On-call agent · MUST

**Behaviour.** An alert triggers an agent that reads the alert, pulls the relevant telemetry and
queue state, follows the matching runbook, applies only the remediations it is authorised for,
and files a GitHub issue recording symptom, diagnosis, action and anything unresolved.

**Authorised remediations.** Reclaim scans with an expired lease; requeue scans that failed on a
transient error, within the retry limit; mark genuinely exhausted scans failed so the user sees
an honest error. Nothing else.

**Acceptance criteria**

- [ ] The agent is triggered by the alert webhook, with the request authenticated by a shared
      secret; an unauthenticated call is rejected.
- [ ] Given the queue-age alert, it identifies stuck scans and reclaims expired leases.
- [ ] Every remediation is idempotent — running the agent twice on the same alert does not
      double-apply anything.
- [ ] It refuses, and escalates instead, when the required action is outside its authorised
      list; the refusal is recorded in the issue.
- [ ] Every run files a GitHub issue stating symptom, what it inspected, what it changed, and
      what remains — including runs where it changed nothing.
- [ ] The full cycle is demonstrated end to end: induce the failure, alert fires, agent
      remediates, queue drains, issue exists. Recorded with evidence in the runbook.
- [ ] The agent never has credentials to deploy, migrate, or delete data.
