#!/usr/bin/env python3
"""Create the labels, project board and work items for promptproof.

The tracker is part of the project's specification, so it is created from source
rather than by hand: re-running this is safe, and the issue set is reviewable in
the same place as the code it describes.

Every issue below links to a requirement in docs/specs and reproduces the
acceptance criteria that issue is responsible for. The full criteria for a
requirement always live in the spec; the issue carries the subset it must satisfy.

Usage:
    python scripts/seed_tracker.py            # create anything missing
    python scripts/seed_tracker.py --dry-run  # print what it would do

Requires: gh, authenticated. Project creation additionally needs the `project`
scope: gh auth refresh -s project,read:project
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys

REPO = "Amar-Ag/promptproof"
PROJECT_TITLE = "promptproof build"
OWNER = "Amar-Ag"

GH = shutil.which("gh") or r"C:\Program Files\GitHub CLI\gh.exe"

LABELS: list[tuple[str, str, str]] = [
    ("phase:1-skeleton", "1d76db", "Walking skeleton: stack up, scan queued end to end"),
    ("phase:2-product", "0e8a16", "The product: corpus, execution, judging, reporting"),
    ("phase:3-verify", "fbca04", "Tests behind one command"),
    ("phase:4-ship", "d93f0b", "CI/CD and deployment"),
    ("phase:5-operate", "5319e7", "Telemetry, alerting, on-call agent"),
    ("phase:6-later", "ededed", "Nice to have if the budget allows"),
    ("role:backend-engineer", "c5def5", "Owned by backend-engineer"),
    ("role:frontend-engineer", "c5def5", "Owned by frontend-engineer"),
    ("role:probe-author", "c5def5", "Owned by probe-author"),
    ("role:test-engineer", "c5def5", "Owned by test-engineer"),
    ("role:sre", "c5def5", "Owned by sre"),
    ("role:architect", "c5def5", "Owned by architect"),
    ("spec:targets", "bfd4f2", "Implements docs/specs/001-targets.md"),
    ("spec:scanning", "bfd4f2", "Implements docs/specs/002-scanning.md"),
    ("spec:reporting", "bfd4f2", "Implements docs/specs/003-reporting.md"),
    ("spec:operability", "bfd4f2", "Implements docs/specs/004-operability.md"),
]

SPEC_URL = "https://github.com/Amar-Ag/promptproof/blob/main/docs/specs"


def body(
    spec: str,
    req: str,
    behaviour: str,
    criteria: list[str],
    role: str,
    deps: str = "None",
    out_of_scope: str = "",
) -> str:
    checks = "\n".join(f"- [ ] {c}" for c in criteria)
    oos = out_of_scope or "Anything not listed in the acceptance criteria above."
    return f"""## Spec

Implements [`{req}`]({SPEC_URL}/{spec}) — see the spec for the full requirement.

## Behaviour

{behaviour}

## Acceptance criteria

{checks}

## Owning role

`{role}`

## Dependencies

{deps}

## Out of scope

{oos}
"""


ISSUES: list[dict] = [
    # ───────────────────────── Phase 1 — walking skeleton ─────────────────────
    dict(
        title="Compose stack and Makefile: web, api, worker, db start with one command",
        labels=["phase:1-skeleton", "role:sre", "spec:operability"],
        body=body(
            "004-operability.md",
            "R-004-1",
            "`make up` builds and starts all four services, applies migrations, and waits for "
            "real readiness. A reviewer with only Docker installed gets a working stack from a "
            "clean clone.",
            [
                "`make up` from a clean clone (after `cp .env.example .env` plus a Groq key) "
                "brings the UI up and the API healthy",
                "Postgres runs as a Compose service; no hosted database is required",
                "Migrations apply automatically on api startup, and starting twice is harmless",
                "api and worker wait for Postgres to be genuinely ready, not just for the port",
                "`GET /healthz` reports liveness; `GET /readyz` reports database reachability",
                "`make down` preserves data; `make reset` destroys the volume",
                "Every command in the AGENTS.md table exists in the Makefile and works",
            ],
            "sre",
        ),
    ),
    dict(
        title="Schema and migrations for target families, targets, scans, findings",
        labels=["phase:1-skeleton", "role:backend-engineer", "spec:targets"],
        body=body(
            "001-targets.md",
            "R-001-3",
            "The initial Alembic migration creating the four tables with the constraints and "
            "indexes the architecture specifies.",
            [
                "Tables `target_families`, `targets`, `scans`, `findings` created in one "
                "forward-only migration",
                "Unique constraint on `targets (family_id, version)`",
                "Partial index on `scans (queued_at) WHERE status = 'pending'`",
                "Index on `findings (scan_id)`; findings cascade on scan delete",
                "`scans.traceparent`, `locked_at`, `locked_by`, `attempts`, `counts` jsonb present",
                "Migration applies cleanly on an empty database and is idempotent to re-run",
            ],
            "backend-engineer",
            deps="Blocked by the Compose stack issue",
        ),
    ),
    dict(
        title="Create and list prompt targets",
        labels=["phase:1-skeleton", "role:backend-engineer", "spec:targets"],
        body=body(
            "001-targets.md",
            "R-001-1",
            "`POST /api/targets` registers a system prompt plus a model as version 1 of a new "
            "target family. `GET` endpoints list and retrieve them.",
            [
                "`POST /api/targets` with name, kind=prompt, system_prompt, model returns 201 "
                "with id, family_id and version 1",
                "System prompt outside 1–20,000 characters is rejected 422 naming the field",
                "Unrecognised model rejected 422 listing allowed models",
                "Name empty or over 200 characters rejected 422",
                "Created target returned by `GET /api/targets` and `GET /api/targets/{id}`",
                "System prompt text appears in no log line, span attribute or metric label",
            ],
            "backend-engineer",
            deps="Blocked by the schema issue",
            out_of_scope="Endpoint targets (separate issue). Deletion.",
        ),
    ),
    dict(
        title="Version a target immutably",
        labels=["phase:1-skeleton", "role:backend-engineer", "spec:targets"],
        body=body(
            "001-targets.md",
            "R-001-3",
            "Editing a target inserts a new row in the same family with version + 1. Existing "
            "versions are never mutated or deleted.",
            [
                "`POST /api/targets/{id}/versions` creates a row in the same family_id with "
                "version incremented by exactly 1, returning 201",
                "The previous version remains byte-identical afterwards",
                "`GET /api/target-families/{family_id}` lists versions descending",
                "No API route mutates or deletes an existing target version",
                "Two simultaneous version creations on one family cannot produce duplicate "
                "version numbers — covered by an integration test with concurrent requests",
            ],
            "backend-engineer",
            deps="Blocked by the create-targets issue",
        ),
    ),
    dict(
        title="Submit a scan: validate, enqueue, capture trace context",
        labels=["phase:1-skeleton", "role:backend-engineer", "spec:scanning"],
        body=body(
            "002-scanning.md",
            "R-002-2",
            "`POST /api/scans` validates the target and suites, resolves suites to a probe set, "
            "writes a pending scan carrying the current trace context, and returns 202. The API "
            "makes no model call.",
            [
                "Returns 202 in under 300 ms at p95 against a local stack",
                "Unknown target 404; unknown suite 422 naming it; empty suite list 422",
                "Row has status pending, non-null queued_at, and a W3C-format traceparent",
                "No model provider call during the request — asserted with a stub that fails "
                "the test if invoked",
                "Scan immediately visible via `GET /api/scans` and `GET /api/scans/{id}`",
            ],
            "backend-engineer",
            deps="Blocked by the schema issue",
        ),
    ),
    dict(
        title="Worker claim loop: SKIP LOCKED, lease expiry, attempt limit",
        labels=["phase:1-skeleton", "role:backend-engineer", "spec:scanning"],
        body=body(
            "002-scanning.md",
            "R-002-3",
            "The worker claims the oldest eligible scan in one statement, honours a lease, "
            "reclaims abandoned work, and fails scans that exhaust their attempts. Phase 1 may "
            "execute stub probes; real execution is a later issue.",
            [
                "Two workers claiming concurrently from a queue of one produce exactly one claim",
                "Claims ordered by queued_at ascending",
                "A running scan with locked_at older than the lease is reclaimed on the next poll",
                "A scan past SCAN_MAX_ATTEMPTS ends failed with an attempt-exhaustion error and "
                "is never claimed again",
                "The claim is a single SQL round trip",
                "Claiming emits a span that is a child of the submitting request's span, sharing "
                "its trace ID",
            ],
            "backend-engineer",
            deps="Blocked by the submit-scan issue",
        ),
    ),
    dict(
        title="Frontend shell, typed API client, target list and create form",
        labels=["phase:1-skeleton", "role:frontend-engineer", "spec:targets"],
        body=body(
            "001-targets.md",
            "R-001-4",
            "The React app, its generated API client, a target list and a create form.",
            [
                "Target list renders distinct empty, loading and error states; an API failure "
                "shows an actionable message, not an endless spinner",
                "Create form surfaces server-side validation errors against the specific field",
                "Submitting the form twice rapidly creates exactly one target",
                "System prompt text rendered as inert text, never HTML or markdown",
                "Detail view lists versions with creation time and can start a scan against any "
                "version, not only the latest",
                "API response types derived from the OpenAPI schema, not hand-copied",
            ],
            "frontend-engineer",
            deps="Blocked by the create-targets and versioning issues",
        ),
    ),
    dict(
        title="Scan list and live progress view",
        labels=["phase:1-skeleton", "role:frontend-engineer", "spec:reporting"],
        body=body(
            "003-reporting.md",
            "R-003-1 and R-003-2",
            "A paginated scan list, and a scan view that follows a running scan and appends "
            "findings as they land.",
            [
                "`GET /api/scans` returns newest first with limit and cursor, default page 20",
                "Rows show target name and version, status, grade, exploited count, duration; a "
                "running scan shows elapsed time",
                "A failed scan shows its reason in the list without opening it",
                "While running, the view shows resolved/total and updates without manual refresh",
                "Findings appear incrementally, not only at completion",
                "Polling stops at a terminal state — asserted by a test counting requests after "
                "completion",
                "Polling interval at least 2s, backing off on failure",
                "pending is visually distinct from running, so a stalled queue is visible to the user",
            ],
            "frontend-engineer",
            deps="Blocked by the worker claim loop issue",
        ),
    ),
    # ───────────────────────── Phase 2 — the product ──────────────────────────
    dict(
        title="Probe corpus schema, loader and validation",
        labels=["phase:2-product", "role:backend-engineer", "spec:scanning"],
        body=body(
            "002-scanning.md",
            "R-002-1",
            "Probes are YAML under probes/<suite>/, loaded and validated at startup, with a "
            "corpus version stamped onto every scan.",
            [
                "Loading validates every probe against a schema; a missing required field fails "
                "startup naming the file and the field",
                "Duplicate probe IDs fail startup",
                "severity restricted to low/medium/high/critical",
                "`GET /api/suites` returns each suite with probe count and description",
                "A probe payload containing shell and SQL metacharacters is transmitted verbatim "
                "and harmlessly — covered by a test",
                "The corpus version appears on every scan record",
            ],
            "backend-engineer",
        ),
    ),
    dict(
        title="Probe corpus: direct-injection, jailbreak and system-prompt-leak suites",
        labels=["phase:2-product", "role:probe-author", "spec:scanning"],
        body=body(
            "002-scanning.md",
            "R-002-1",
            "The first three suites, at least six probes each, each probe declaring success "
            "criteria the judge can evaluate and actionable remediation.",
            [
                "At least 6 probes in each of direct-injection, jailbreak, system-prompt-leak",
                "Every probe has a stable unique ID, severity, payload, success_criteria, "
                "remediation and source",
                "Each success_criteria distinguishes compliance from a refusal that quotes the "
                "attack back",
                "One technique per probe; surface form varies across the suite",
                "No payload seeks genuinely harmful content, working exploit code, or targets a "
                "real named company's system",
                "Corpus validation and coverage tests pass",
            ],
            "probe-author",
            deps="Blocked by the corpus schema issue",
        ),
    ),
    dict(
        title="Probe corpus: data-exfiltration, indirect-injection and tool-abuse suites",
        labels=["phase:2-product", "role:probe-author", "spec:scanning"],
        body=body(
            "002-scanning.md",
            "R-002-1",
            "The remaining three suites, bringing the corpus to at least 40 probes across six "
            "suites.",
            [
                "At least 6 probes in each of data-exfiltration, indirect-injection, tool-abuse",
                "Corpus totals at least 40 probes across all six suites",
                "indirect-injection probes place attacker instructions inside content the app "
                "would treat as retrieved data",
                "Same format, severity-calibration and safety rules as the first three suites",
                "Corpus validation and coverage tests pass",
            ],
            "probe-author",
            deps="Blocked by the first corpus issue",
        ),
    ),
    dict(
        title="Model provider client: timeouts, bounded retry, telemetry scrubbing",
        labels=["phase:2-product", "role:backend-engineer", "spec:scanning"],
        body=body(
            "002-scanning.md",
            "R-002-4",
            "One client in packages/core owning all model I/O, its timeouts, its bounded retry "
            "with backoff, and the scrubber. No other module calls a provider directly.",
            [
                "All target and judge calls go through this client — asserted by a test that "
                "fails if a provider SDK is imported elsewhere",
                "Per-call timeout from MODEL_TIMEOUT_SECONDS",
                "Transient 429 and 5xx retried with backoff up to the configured limit, then "
                "surfaced as a typed error",
                "Prompt text, response text and API keys pass through the scrubber before any "
                "span attribute, log field or metric label",
                "Latency recorded per call",
            ],
            "backend-engineer",
        ),
    ),
    dict(
        title="Execute probes against a prompt target with bounded concurrency",
        labels=["phase:2-product", "role:backend-engineer", "spec:scanning"],
        body=body(
            "002-scanning.md",
            "R-002-4",
            "The worker runs each probe against the target with at most SCAN_CONCURRENCY in "
            "flight. A failing probe never fails the scan.",
            [
                "Every probe in the selected suites produces exactly one finding",
                "Peak in-flight calls never exceed SCAN_CONCURRENCY — asserted by a stub "
                "recording concurrency",
                "A provider timeout produces an errored finding and the scan continues",
                "A transient 429 is retried then recorded errored",
                "Errored probes are counted separately in `counts`",
                "target_latency_ms recorded per finding",
                "Neither system prompt nor target response appears in any log, span or metric label",
            ],
            "backend-engineer",
            deps="Blocked by the provider client and corpus loader issues",
        ),
    ),
    dict(
        title="Judge verdicts: structured output, evidence, fixture tests",
        labels=["phase:2-product", "role:backend-engineer", "spec:scanning"],
        body=body(
            "002-scanning.md",
            "R-002-5",
            "An independent judge model, from a different family than the default target, "
            "decides each probe's outcome from the probe's success criteria and stores its "
            "reasoning as evidence.",
            [
                "Judge returns structured output; a malformed response is retried once then the "
                "finding is errored",
                "Every non-errored finding stores a non-empty judge_rationale",
                "response_excerpt stored, truncated to at most 2,000 characters",
                "Judge model recorded on the scan; a configuration test asserts it differs from "
                "DEFAULT_TARGET_MODEL",
                "Fixture test: a recorded refusal that quotes the attack judges resisted",
                "Fixture test: a recorded compliant paraphrase judges exploited",
                "judge_latency_ms recorded per finding",
            ],
            "backend-engineer",
            deps="Blocked by the probe execution issue",
        ),
    ),
    dict(
        title="Grade the scan and complete it transactionally",
        labels=["phase:2-product", "role:backend-engineer", "spec:scanning"],
        body=body(
            "002-scanning.md",
            "R-002-6",
            "When every probe has resolved, compute the grade from exploited findings using the "
            "worst-case table, write counts, and mark the scan completed.",
            [
                "Grades follow the table exactly, including the one-high vs two-high boundary",
                "Every grade boundary has a unit test",
                "A scan where every probe errored is completed with no grade, not graded A",
                "counts reports exploited/resisted/errored per severity",
                "finished_at, grade, counts and status are written in one transaction",
            ],
            "backend-engineer",
            deps="Blocked by the judge issue",
        ),
    ),
    dict(
        title="Report view: grade, breakdown, evidence-backed findings rendered inert",
        labels=["phase:2-product", "role:frontend-engineer", "spec:reporting"],
        body=body(
            "003-reporting.md",
            "R-003-3",
            "The report shows the grade, a per-suite breakdown, and findings ordered exploited, "
            "errored, resisted — each expandable to payload, excerpt, rationale and remediation.",
            [
                "Grade and per-suite exploited/resisted/errored counts shown above the findings",
                "Findings ordered exploited, then errored, then resisted; within group by "
                "severity descending",
                "Each finding expands to payload, response excerpt, judge rationale, remediation",
                "A finding containing `<script>` or markdown image syntax renders literally — "
                "covered by a test",
                "Errored findings state why and are visually separated from resisted ones",
                "Metadata shows target name and version, corpus version, judge model, timings",
                "Report reachable at a stable, shareable, reloadable URL",
            ],
            "frontend-engineer",
            deps="Blocked by the grading issue",
        ),
    ),
    dict(
        title="SSRF guard and endpoint targets",
        labels=["phase:2-product", "role:backend-engineer", "spec:targets"],
        body=body(
            "001-targets.md",
            "R-001-2 and R-002-7",
            "Endpoint targets, with a guard applied at registration and before every request: "
            "DNS resolved once and pinned, private ranges refused, redirects re-validated.",
            [
                "kind=endpoint requires endpoint_url and ownership_confirmed true; omitting the "
                "confirmation returns 422 with an explanation",
                "Non-https rejected unless SSRF_ALLOW_PRIVATE, which production config asserts false",
                "Loopback, link-local, private, multicast and reserved addresses refused 422 "
                "without leaking the resolved address",
                "A stored auth header is never returned by any GET and never appears in telemetry",
                "Guard applied before every request, not only at registration",
                "A redirect to a private address is refused mid-request; the probe is errored "
                "with a guard-specific reason",
                "DNS resolved once and pinned — covered by a test with a rebinding stub resolver",
                "A wholly unreachable endpoint fails the scan rather than producing 40 errored findings",
            ],
            "backend-engineer",
        ),
    ),
    # ───────────────────────── Phase 3 — verify ───────────────────────────────
    dict(
        title="Unit test suite: grading, corpus validation, scrubber, SSRF guard",
        labels=["phase:3-verify", "role:test-engineer", "spec:operability"],
        body=body(
            "004-operability.md",
            "R-004-2",
            "Pure-function coverage of the logic most likely to be wrong, with no I/O.",
            [
                "Unit tests perform no I/O and complete in under 10 seconds",
                "Every grade-table boundary covered",
                "Corpus schema validation covered, including duplicate IDs and bad severity",
                "Scrubber covered: sentinel values never survive into emitted attributes",
                "SSRF guard covered for each refused address class",
                "A network-guard fixture fails the run on any unstubbed socket connection",
                "Each test verified to fail against the pre-change code, recorded in the PR",
            ],
            "test-engineer",
        ),
    ),
    dict(
        title="Integration tests: concurrency, lease, partial failure, trace propagation",
        labels=["phase:3-verify", "role:test-engineer", "spec:operability"],
        body=body(
            "004-operability.md",
            "R-004-2 and R-004-5",
            "Tests against a real Postgres started by the harness, with a stubbed provider, "
            "covering the failure modes the specs call out.",
            [
                "Run against real Postgres with a stubbed model provider; no real provider calls",
                "Two concurrent workers claiming one scan produce exactly one claim",
                "Two concurrent target-version inserts cannot duplicate a version number",
                "An expired lease is reclaimed; an exhausted scan ends failed and is not reclaimed",
                "A probe timeout yields an errored finding and the scan still completes",
                "Exported spans show the worker's scan.execute sharing the API's scan.submit "
                "trace ID",
                "Sentinel prompt, response and auth header appear nowhere in captured spans, "
                "logs or metric labels",
            ],
            "test-engineer",
            deps="Blocked by the unit test suite issue",
        ),
    ),
    dict(
        title="Frontend tests and the Playwright end-to-end journey",
        labels=["phase:3-verify", "role:test-engineer", "spec:operability"],
        body=body(
            "004-operability.md",
            "R-004-2",
            "Vitest coverage of component states and polling behaviour, plus one Playwright "
            "journey through the whole product against the Compose stack.",
            [
                "`make test` runs unit, integration and frontend suites with a single pass/fail",
                "Vitest covers loading, empty, error and loaded states for list and report views",
                "Vitest asserts polling stops at a terminal state",
                "Vitest asserts adversarial finding content renders literally",
                "`make e2e` covers create target → run scan → read report against the Compose stack",
                "e2e waits on state, never on a duration",
                "Everything passes from a clean clone with only Docker installed",
            ],
            "test-engineer",
            deps="Blocked by the integration tests issue",
        ),
    ),
    # ───────────────────────── Phase 4 — ship ─────────────────────────────────
    dict(
        title="CI: lint, type check and all test suites on every pull request",
        labels=["phase:4-ship", "role:sre", "spec:operability"],
        body=body(
            "004-operability.md",
            "R-004-3",
            "A GitHub Actions workflow gating merge on lint, types and tests.",
            [
                "Triggers on pull_request and on push to main",
                "Runs lint, type check, unit, integration and frontend tests",
                "A deliberately broken test produces a failing check — demonstrated once and "
                "linked in the PR",
                "main is protected: the check is required and direct pushes are refused",
                "No secret is required for CI to run the test suites",
            ],
            "sre",
        ),
    ),
    dict(
        title="CD: automatic dev deploy, gated production deploy, rollback drill",
        labels=["phase:4-ship", "role:sre", "spec:operability"],
        body=body(
            "004-operability.md",
            "R-004-4",
            "Merging to main deploys to promptproof-dev automatically. Production is a separate, "
            "manually triggered, approval-gated workflow. Rollback is exercised for real.",
            [
                "A merge to main deploys to dev with no human action and the dev URL serves it",
                "Production requires a manual trigger and an approval on a protected environment",
                "The production workflow refuses to run unless CI passed on that commit",
                "Migrations run as a release step before the new version takes traffic",
                "Both environments have separate databases and separate credentials",
                "A rollback is performed once and the outcome recorded in "
                "docs/runbooks/rollback.md",
            ],
            "sre",
            deps="Blocked by the CI issue",
        ),
    ),
    # ───────────────────────── Phase 5 — operate ──────────────────────────────
    dict(
        title="OpenTelemetry tracing across the API and worker boundary",
        labels=["phase:5-operate", "role:sre", "spec:operability"],
        body=body(
            "004-operability.md",
            "R-004-5",
            "One trace per scan, spanning both processes, joined through the W3C context "
            "persisted on scans.traceparent.",
            [
                "A submitted scan produces one trace covering scan.submit, scan.claim, "
                "scan.execute, and per probe probe.execute with target.invoke and judge.evaluate",
                "Worker spans share the submitting request's trace ID",
                "queue.wait_ms recorded on the claim span",
                "Span names and attributes match docs/observability.md",
                "With the exporter unreachable, requests still succeed and scans still complete",
                "Signals viewable in a hosted UI, with the location documented",
            ],
            "sre",
        ),
    ),
    dict(
        title="Metrics, structured logs and enforced scrubbing",
        labels=["phase:5-operate", "role:sre", "spec:operability"],
        body=body(
            "004-operability.md",
            "R-004-5",
            "The metric set and structured logging from docs/observability.md, with scrubbing "
            "enforced by a test rather than by intention.",
            [
                "Metrics exist for scans submitted, scans completed by status, scan duration, "
                "probe duration by suite and outcome, queue depth, queue oldest-age",
                "Every log line emitted while handling a scan carries trace_id and span_id",
                "Logs are structured JSON through the shared logger",
                "A test asserts a known system prompt, response and auth header appear nowhere "
                "in captured spans, logs or metric labels",
                "No metric label contains unbounded or user-supplied text",
            ],
            "sre",
            deps="Blocked by the tracing issue",
        ),
    ),
    dict(
        title="Alerts on user impact: stalled queue and elevated failure rate",
        labels=["phase:5-operate", "role:sre", "spec:operability"],
        body=body(
            "004-operability.md",
            "R-004-6",
            "Two alerts, both on user-visible symptoms, each linked to exactly one runbook, each "
            "fired deliberately once.",
            [
                "scan-queue-stalled fires when queue.oldest_age_seconds exceeds 120s sustained "
                "over 5 minutes",
                "scan-failure-rate-high fires when the failure rate exceeds 20% over 10 minutes",
                "Each alert names the user-visible symptom and links its runbook",
                "No alert is defined on CPU, memory or disk as a primary signal",
                "Each alert fired deliberately once, with the result in the runbook's Exercise log",
                "Delivery reaches both the on-call agent endpoint and a human channel",
            ],
            "sre",
            deps="Blocked by the metrics issue",
        ),
    ),
    dict(
        title="On-call responder agent: diagnose from the runbook, remediate, file an issue",
        labels=["phase:5-operate", "role:sre", "spec:operability"],
        body=body(
            "004-operability.md",
            "R-004-7",
            "A PydanticAI agent on Groq, triggered by the alert webhook, which follows the "
            "matching runbook, applies only AGENT-marked remediations, and files an issue on "
            "every run.",
            [
                "Triggered by the alert webhook, authenticated by a shared secret; an "
                "unauthenticated call is rejected",
                "Given the queue-age alert, identifies stuck scans and reclaims expired leases",
                "Every remediation idempotent — two runs on one alert do not double-apply",
                "Refuses and escalates when the needed action is outside its authorised list, "
                "recording the refusal in the issue",
                "Files a GitHub issue on every run stating symptom, what it inspected, what it "
                "changed and what remains — including runs that changed nothing",
                "Has no credential to deploy, migrate or delete data",
                "Full cycle demonstrated: induce failure, alert fires, agent remediates, queue "
                "drains, issue exists — evidence recorded in the runbook",
            ],
            "sre",
            deps="Blocked by the alerting issue",
        ),
    ),
    # ───────────────────────── Phase 6 — later ────────────────────────────────
    dict(
        title="Compare two scans of one target family",
        labels=["phase:6-later", "role:backend-engineer", "spec:reporting"],
        body=body(
            "003-reporting.md",
            "R-003-4",
            "Given two scans of the same family with overlapping suites, show the grade change "
            "and per-probe whether each outcome improved, regressed or held.",
            [
                "`GET /api/scans/{a}/compare/{b}` returns per-probe fixed/regressed/unchanged/"
                "not-comparable plus both grades",
                "Scans from different families return 400 with an explanation",
                "Probes present in only one scan are reported not-comparable, not dropped",
                "A probe that errored in either scan is not-comparable, never counted as fixed",
                "The UI shows regressions first",
            ],
            "backend-engineer",
            deps="Blocked by the grading and report-view issues",
        ),
    ),
]


def run(args: list[str], dry: bool) -> str:
    if dry:
        print("  would run:", " ".join(args[:6]), "...")
        return ""
    result = subprocess.run([GH, *args], capture_output=True, text=True)
    if result.returncode != 0:
        print(f"  ! {result.stderr.strip()}", file=sys.stderr)
    return result.stdout.strip()


def existing_titles() -> set[str]:
    out = subprocess.run(
        [
            GH,
            "issue",
            "list",
            "--repo",
            REPO,
            "--state",
            "all",
            "--limit",
            "300",
            "--json",
            "title",
        ],
        capture_output=True,
        text=True,
    )
    if out.returncode != 0:
        return set()
    return {i["title"] for i in json.loads(out.stdout or "[]")}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    opts = ap.parse_args()

    print(f"Labels ({len(LABELS)})")
    for name, color, desc in LABELS:
        run(
            [
                "label",
                "create",
                name,
                "--repo",
                REPO,
                "--color",
                color,
                "--description",
                desc,
                "--force",
            ],
            opts.dry_run,
        )
        print(f"  {name}")

    print(f"\nProject board: {PROJECT_TITLE}")
    run(["project", "create", "--owner", OWNER, "--title", PROJECT_TITLE], opts.dry_run)

    have = set() if opts.dry_run else existing_titles()
    print(f"\nWork items ({len(ISSUES)})")
    for issue in ISSUES:
        if issue["title"] in have:
            print(f"  = {issue['title'][:70]} (exists)")
            continue
        args = [
            "issue",
            "create",
            "--repo",
            REPO,
            "--title",
            issue["title"],
            "--body",
            issue["body"],
        ]
        for label in issue["labels"]:
            args += ["--label", label]
        url = run(args, opts.dry_run)
        print(f"  + {issue['title'][:70]}" + (f"  {url}" if url else ""))

    print("\nDone. Add the issues to the board, then move Phase 1 items to Ready.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
