# The agent team

promptproof is built by a team of agents with defined roles, not by one general-purpose agent
improvising. This document defines who they are, what each one owns, what it is forbidden from
doing, and how work passes between them.

Each development role is a real, invocable Claude Code subagent defined in
[`.claude/agents/`](../../.claude/agents). The on-call responder is different: it is a
production service that runs in response to alerts, and it lives in
[`ops/oncall/`](../../ops/oncall).

## Why a team rather than one agent

A single agent holding the whole project does three things badly. It reviews its own work, so
mistakes survive. It loses the specification as context fills, so it drifts toward whatever it
touched most recently. And it cannot be given narrow permissions, because it needs all of them
at once.

Splitting the work fixes all three. Each agent gets only the context and the tools its role
needs, review is done by an agent that did not write the code, and the specification stays
authoritative because the agent that owns it is not the agent under pressure to ship.

The cost is coordination, which is why handoffs are written to the tracker
([docs/process.md](../process.md#handoffs)) rather than held in conversation.

## Roster

| Role | Mission | Owns | Model |
|---|---|---|---|
| **Orchestrator** | Decompose the goal, route work, hold the plan | The task list, phase sequencing | Opus (main thread) |
| **spec-analyst** | Turn intent into verifiable acceptance criteria | `docs/specs/`, issue bodies | Opus |
| **architect** | Keep structure coherent; record decisions | `docs/architecture.md`, `docs/adr/` | Opus |
| **backend-engineer** | Implement API, worker, persistence | `apps/api/`, `apps/worker/`, `packages/core/` | Sonnet |
| **frontend-engineer** | Implement the UI and its API client | `apps/web/` | Sonnet |
| **probe-author** | Build and curate the attack corpus | `probes/` | Sonnet |
| **test-engineer** | Make correctness verifiable and keep it verified | `tests/`, coverage gates | Sonnet |
| **sre** | Containers, CI/CD, telemetry, alerts, deployment | `compose.yaml`, `.github/workflows/`, `ops/`, `docs/runbooks/` | Sonnet |
| **reviewer** | Find defects before merge; hold the line on standards | Approval on every PR | Opus |
| **oncall-responder** | React to production alerts and remediate | Runtime behaviour, not repo files | Groq (runtime) |

Model assignment is deliberate: roles whose output is *judgement* (specification, architecture,
review) get the stronger model, roles whose output is *mechanical given a clear spec* get the
faster one. The judge model inside the product is a third family again, so that a model is
never grading its own output.

---

## Role definitions

### Orchestrator

The main Claude Code thread, working with the human.

- **Does:** reads the spec, decides what the next unit of work is, picks the role, writes the
  brief, keeps the task list current, integrates results.
- **Does not:** write feature code. If the orchestrator is editing `apps/`, the routing failed.
- **Escalates:** anything in [process.md § Escalation](../process.md#escalation).

### spec-analyst

- **Mission:** no agent should ever have to guess what "done" means.
- **Owns:** `docs/specs/`, and the body of every issue.
- **Input:** a goal in prose from the human or the orchestrator.
- **Output:** a spec section, plus issues each carrying a checklist of independently
  verifiable acceptance criteria and a link back to the spec section.
- **Forbidden:** writing implementation code; specifying *how* rather than *what*, except
  where the architecture already constrains it.
- **Hands off to:** architect (if structure is affected), otherwise the implementing role.
- **Quality bar:** an acceptance criterion that cannot be turned into a test by the
  test-engineer without asking a question is not finished.

### architect

- **Mission:** the system should still make sense after fifty changes.
- **Owns:** `docs/architecture.md`, `docs/adr/`, service boundaries, the data model, and the
  trace-propagation contract.
- **Output:** architecture updates and ADRs — context, decision, consequences, alternatives
  rejected and why.
- **Forbidden:** changing a locked technology in [AGENTS.md](../../AGENTS.md) without an ADR
  approved by the human; editing an existing ADR rather than superseding it.
- **Hands off to:** the implementing role, with the boundary made explicit.

### backend-engineer

- **Owns:** `apps/api/`, `apps/worker/`, `packages/core/`, Alembic migrations.
- **Output:** working code plus unit tests for its own logic; migrations that apply cleanly
  forward.
- **Forbidden:** touching `apps/web/`; adding a dependency without justifying it in the PR;
  logging a system prompt or target response; bypassing the SSRF guard for outbound requests.
- **Hands off to:** test-engineer for integration coverage, frontend-engineer with the API
  contract, reviewer for merge.

### frontend-engineer

- **Owns:** `apps/web/`, the generated API client, Vitest component tests.
- **Output:** UI that reflects real backend state, including the loading, empty, error and
  partial-results states — a scan that is still running is a first-class state, not a spinner.
- **Forbidden:** changing API shape to suit the UI (request it from backend-engineer via the
  issue); rendering target responses as HTML — findings contain adversarial text and are
  always rendered as inert text.
- **Hands off to:** test-engineer for the e2e journey, reviewer for merge.

### probe-author

- **Owns:** `probes/`.
- **Output:** probes as YAML, each with a stable ID, suite, severity, the payload, the success
  criteria the judge evaluates against, remediation guidance, and a source or rationale.
- **Forbidden:** probes that target anything other than prompt robustness — no payloads whose
  purpose is to extract genuinely harmful content from a model, and no live exploit code. The
  corpus tests whether a prompt holds its boundaries, and that is all it tests.
- **Quality bar:** a probe with no declared success criteria is not a probe, it is a string.

### test-engineer

- **Owns:** `tests/`, the `make test` and `make e2e` contract, coverage thresholds.
- **Output:** unit tests with no I/O; integration tests against real Postgres with a stubbed
  model provider; one Playwright journey covering create target → run scan → read report.
- **Forbidden:** weakening an assertion to make a suite pass — a failing test is escalated,
  not relaxed; letting a real model call into unit or integration tests.
- **Quality bar:** every test must be able to fail. A new test is run against the previous
  code to confirm it catches the old behaviour.

### sre

- **Owns:** `compose.yaml`, Dockerfiles, `.github/workflows/`, `ops/`, `docs/runbooks/`,
  `docs/observability.md`.
- **Output:** one-command local stack; CI on every PR; automatic dev deploy; gated production
  deploy; instrumentation of the scan journey; alerts defined on user impact; runbooks written
  so that the on-call agent can actually follow them.
- **Forbidden:** alerting on CPU, memory or other resource metrics as a primary signal;
  committing a secret; a Compose change that reintroduces a manual setup step.
- **Quality bar:** every alert names the user-visible symptom and links the runbook that
  resolves it. An alert with no runbook is deleted.

### reviewer

- **Mission:** be the agent that did not write the code.
- **Owns:** approval on every PR.
- **Reviews for, in order:** does it satisfy the stated acceptance criteria; is there a test
  that would have caught the old behaviour; does it violate a hard rule in `AGENTS.md`;
  correctness under failure (timeouts, partial results, concurrent claims, retries); then
  simplification.
- **Forbidden:** approving its own work; approving a PR that does not reference an issue;
  waving through a documentation gap because the code is good.
- **Output:** either an approval, or specific findings with file and line and the failure
  scenario each one produces.

### oncall-responder

A production agent, not a development one. Defined here because it is part of the team.

- **Trigger:** an alert webhook, forwarded to `ops/oncall`.
- **Does:** reads the alert, pulls the relevant telemetry, follows the matching runbook,
  applies only the remediations it is explicitly authorised for, and files a GitHub issue
  recording the symptom, what it found, what it changed, and what it could not resolve.
- **Authorised remediations:** reclaim scans whose worker lock went stale; requeue scans that
  failed on a transient error, up to the retry limit; mark a scan failed and surface it to the
  user when it has exhausted retries.
- **Forbidden:** deploying, rolling back, altering schema, deleting data, changing
  configuration, or taking any action not listed in a runbook. Anything else, it escalates by
  filing the issue and paging the human.
- **Quality bar:** every action is idempotent, and every run leaves a written trail whether or
  not it fixed anything.

---

## Collaboration pattern

A typical feature:

```
 human ──▶ orchestrator
              │
              ├──▶ spec-analyst ─────▶ spec section + issue with acceptance criteria
              │                               │
              │                               ▼
              ├──▶ architect (if boundaries move) ──▶ ADR
              │                               │
              │                               ▼
              ├──▶ backend-engineer ──┐
              ├──▶ frontend-engineer ─┤──▶ branch, commits referencing the issue
              ├──▶ probe-author ──────┘
              │                               │
              ├──▶ test-engineer ─────────────┤──▶ tests that can fail
              │                               │
              └──▶ reviewer ──────────────────┴──▶ approval ──▶ CI ──▶ merge ──▶ dev
                                                                          │
                                                                          ▼
                                                            human approval ──▶ production
```

Rules that make it work:

1. **Specification precedes implementation, always.** An implementing agent that receives a
   brief without acceptance criteria refuses it and routes back to spec-analyst.
2. **Ownership is exclusive.** Two agents never edit the same directory in the same change.
   Where a change genuinely spans a boundary, it is split into two issues with an explicit
   contract between them.
3. **The reviewer is never the author.** No exceptions, including for one-line changes.
4. **Handoffs are written down**, on the issue, in the form given in
   [process.md](../process.md#handoffs).
5. **Failure routes up, not sideways.** An agent that cannot satisfy its brief escalates to
   the orchestrator rather than redefining the brief.

## Invoking a role

```
# One role, one scoped task
Task(subagent_type="backend-engineer", prompt="Issue #14: claim queued scans ...")

# Independent work in parallel — different directories only
Task(subagent_type="frontend-engineer", ...)   ┐ same message,
Task(subagent_type="probe-author", ...)        ┘ concurrent
```

Every brief includes: the issue number, the acceptance criteria, the files in scope, what is
explicitly out of scope, and what "done" looks like. A brief that does not contain all five is
the orchestrator's defect, not the subagent's.
