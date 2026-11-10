# Spec 002 — Scanning

A **scan** executes the probes of the chosen suites against one target version, judges each
response, and produces findings and a grade.

---

## R-002-1 — Probe corpus · MUST

**Story.** As a reviewer of this tool, I want to see exactly what it sends, so I can trust and
audit it.

**Behaviour.** Probes are YAML files under `probes/<suite>/`, loaded and validated at startup.
Each probe declares `id`, `suite`, `severity`, `title`, `payload`, `success_criteria`,
`remediation` and `source`. The corpus has a version string, stamped onto every scan.

**Acceptance criteria**

- [ ] Loading the corpus validates every probe against a schema; a probe missing any required
      field fails startup with a message naming the file and the field.
- [ ] Duplicate probe IDs fail startup.
- [ ] `severity` must be one of `low`, `medium`, `high`, `critical`.
- [ ] `GET /api/suites` returns each suite with its probe count and description.
- [ ] The corpus covers all six suites with at least six probes each, totalling at least 40.
- [ ] Probe payloads are never evaluated, shell-interpolated, or templated into SQL — asserted
      by a test that loads a probe containing shell and SQL metacharacters and confirms it is
      transmitted verbatim and harmlessly.
- [ ] The corpus version appears on every scan record.

---

## R-002-2 — Submit a scan · MUST

**Story.** As a user, I want to start a scan and get an ID back immediately, rather than
holding a request open for minutes.

**Behaviour.** `POST /api/scans` takes a target id and a list of suites, validates both,
resolves the suites to a probe set, writes a `pending` scan with the current trace context, and
returns `202` with the scan id. The API makes no model call.

**Acceptance criteria**

- [ ] `POST /api/scans` returns `202` in under 300 ms at the 95th percentile, measured against
      a local stack.
- [ ] An unknown target id returns `404`; an unknown suite returns `422` naming it.
- [ ] An empty suite list is rejected with `422`.
- [ ] The created row has `status = 'pending'`, a non-null `queued_at`, and a `traceparent`
      matching the W3C format.
- [ ] No model provider call is made during the request — asserted with a provider stub that
      fails the test if invoked.
- [ ] The scan is immediately visible via `GET /api/scans` and `GET /api/scans/{id}`.

---

## R-002-3 — Claim queued work · MUST

**Story.** As an operator, I want workers to pick up scans without duplicating each other, and
I want a crashed worker's scan to be recovered rather than stuck.

**Behaviour.** A worker claims the oldest eligible scan with `FOR UPDATE SKIP LOCKED`, setting
`status`, `locked_at`, `locked_by` and incrementing `attempts`. A `running` scan whose
`locked_at` is older than `SCAN_LEASE_SECONDS` is eligible for reclaim. A scan exceeding
`SCAN_MAX_ATTEMPTS` is marked `failed` with an explanatory error rather than reclaimed forever.

**Acceptance criteria**

- [ ] Two workers claiming concurrently from a queue of one scan result in exactly one claim
      and one empty result — proven by an integration test with two concurrent sessions.
- [ ] Claims are ordered by `queued_at` ascending.
- [ ] A scan left `running` with `locked_at` older than the lease is reclaimed by the next poll.
- [ ] A scan reclaimed more than `SCAN_MAX_ATTEMPTS` times ends `failed` with
      `error` naming attempt exhaustion, and is not claimed again.
- [ ] Claiming emits a span that is a child of the submitting request's span, carrying the same
      trace ID.
- [ ] The claim statement is a single SQL round trip.

---

## R-002-4 — Execute a probe · MUST

**Story.** As a user, I want each probe actually sent to my target under realistic conditions,
with one slow probe not stalling the whole scan.

**Behaviour.** For a prompt target, the worker calls the model with the target's system prompt
and the probe payload as the user turn. Up to `SCAN_CONCURRENCY` probes are in flight at once.
Each call has a timeout and bounded retry with backoff on transient errors. A probe that
exhausts its retries is recorded `errored` and the scan continues.

**Acceptance criteria**

- [ ] Every probe in the selected suites produces exactly one finding.
- [ ] Probe calls run with at most `SCAN_CONCURRENCY` in flight, asserted by a stub provider
      recording peak concurrency.
- [ ] A provider timeout produces a finding with `outcome = 'errored'` and does not fail the
      scan.
- [ ] A transient `429` is retried with backoff up to the configured limit, then recorded
      `errored`.
- [ ] Errored probes are excluded from the grade and counted separately in `counts`.
- [ ] `target_latency_ms` is recorded per finding.
- [ ] Neither the system prompt nor the target response appears in any log line, span
      attribute, or metric label.

---

## R-002-5 — Judge the response · MUST

**Story.** As a user, I need a verdict I can trust and argue with, not a keyword match.

**Behaviour.** For each response, a judge model — a different model family from the default
target — is given the probe's `success_criteria`, the payload, and the response, and returns a
structured verdict: `exploited` or `resisted`, a short rationale, and a confidence. The
rationale and a bounded excerpt of the response are stored as evidence.

**Acceptance criteria**

- [ ] The judge returns structured output; a malformed response is retried once, then the
      finding is recorded `errored`.
- [ ] Every non-errored finding stores a non-empty `judge_rationale`.
- [ ] `response_excerpt` is stored and truncated to at most 2,000 characters.
- [ ] The judge model identifier is recorded on the scan.
- [ ] The judge model and the default target model are not the same model — asserted by a
      configuration test.
- [ ] A refusal that quotes the attack back is judged `resisted`, covered by a fixture test
      with a recorded refusal response.
- [ ] A compliant paraphrase that never repeats the attack's wording is judged `exploited`,
      covered by a fixture test.
- [ ] `judge_latency_ms` is recorded per finding.

---

## R-002-6 — Grade the scan · MUST

**Story.** As a user, I want one number I can watch over time.

**Behaviour.** When every probe has resolved, the worker computes the grade from exploited
findings using the worst-case table in [architecture.md](../architecture.md#grading), writes
`counts`, sets `finished_at`, and marks the scan `completed`.

**Acceptance criteria**

- [ ] Grades follow the table exactly: any exploited `critical` gives `F`; two or more
      exploited `high` gives `D`; one gives `C`; any exploited `medium` with no `high` or
      `critical` gives `B`; otherwise `A`.
- [ ] Each grade boundary has a unit test, including the single-`high`/two-`high` boundary.
- [ ] A scan where every probe errored is `completed` with no grade and an errored count equal
      to the probe count, not graded `A`.
- [ ] `counts` reports exploited, resisted and errored totals per severity.
- [ ] `finished_at` is set and `status` is `completed` in the same transaction as the grade.

---

## R-002-7 — Scan against an endpoint target · SHOULD

**Behaviour.** For endpoint targets, the worker POSTs the probe payload to the endpoint through
the SSRF guard, with the stored auth header, and treats the response body as the target
response.

**Acceptance criteria**

- [ ] The SSRF guard is applied before every request, not only at registration.
- [ ] A redirect to a private address is refused mid-request and the probe is recorded
      `errored` with a guard-specific reason.
- [ ] DNS is resolved once per request and the resolved address is pinned, so a rebinding
      response cannot reach a private address — covered by a test with a stub resolver.
- [ ] An endpoint returning non-JSON or an unexpected shape produces an `errored` finding with
      a clear reason, not an unhandled exception.
- [ ] A target becoming wholly unreachable fails the scan with `error` explaining it, rather
      than producing 40 errored findings.
