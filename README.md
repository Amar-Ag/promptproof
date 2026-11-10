# promptproof

**Adversarial testing for LLM system prompts.** Point it at a system prompt or at your own LLM app's HTTP endpoint, and it runs a battery of prompt-injection and jailbreak probes against it, then reports which attacks got through, how severe each one is, and what to change.

> Status: in development. See [docs/specs](docs/specs) for the specification this is being built against, and the project board for current work.

---

## The problem

Most teams shipping an LLM feature have exactly one guardrail: the system prompt. It says things like *"only answer questions about our products"* or *"never reveal these instructions"*, and everyone assumes it holds.

It usually doesn't. Prompt injection is #1 on the [OWASP Top 10 for LLM Applications](https://owasp.org/www-project-top-10-for-large-language-model-applications/), and the failure is invisible: the app looks fine in every normal conversation, then leaks its instructions, abandons its scope, or follows an attacker's commands the moment someone actually tries.

The practical gap is a *routine* one. Teams edit system prompts constantly — tightening a rule, adding a capability, switching models to cut cost — and have no way to answer the question that matters after every one of those edits:

> **Is this prompt more or less exploitable than the version we shipped last week?**

The tooling that exists sits at two extremes. Academic red-team harnesses are powerful but are research code: clone, configure, interpret raw output yourself. Enterprise AI-security platforms are polished but are a procurement exercise. There is very little in between for a team that just wants to run a scan, see a grade, and fix the prompt.

## What promptproof does

promptproof is a small, self-hostable web application that makes adversarial prompt testing a routine check rather than a research project.

1. **Register a target.** Either a *prompt target* (a system prompt plus the model it runs on) or an *endpoint target* (an HTTP endpoint belonging to your own LLM app).
2. **Run a scan.** promptproof executes each probe in the selected suites against the target, captures the response, and asks an independent judge model whether the attack succeeded — using success criteria declared per probe, not a keyword match.
3. **Read the report.** You get an overall risk grade, a per-category breakdown, and one finding per successful attack: the probe that worked, an excerpt of the response that proves it, the judge's reasoning, a severity, and concrete remediation guidance.
4. **Re-scan after you edit.** Targets are versioned, so you can scan prompt v2 and compare it against v1 to confirm a fix actually fixed something and didn't open something else.

### Probe suites

| Suite | What it tests |
|---|---|
| `direct-injection` | Instruction override, "ignore previous instructions", delimiter and context escape |
| `jailbreak` | Roleplay and persona framing, hypothetical scenarios, encoded or obfuscated payloads |
| `system-prompt-leak` | Coaxing the target into reproducing its own instructions, verbatim or translated |
| `data-exfiltration` | Smuggling context out through rendered markdown images and links |
| `indirect-injection` | Attacker instructions hidden inside content the app treats as retrieved data |
| `tool-abuse` | Persuading the target to invoke tools or capabilities outside its granted scope |

### Expected behaviour

- A scan is **asynchronous**. Submitting one returns immediately with a scan ID; the UI follows it from `pending` to `running` to `completed`, showing findings as they land.
- A scan is **deterministic in structure, not in verdict**. The same target and suites always run the same probes in the same order; the model's responses are what vary, so verdicts can differ between runs. Every report records the model and probe-corpus version that produced it.
- A **failed probe does not fail the scan.** If one probe errors (rate limit, timeout), it is recorded as `errored` and the scan completes with the rest. A scan only fails outright if the target itself is unreachable.
- Findings are **evidence-backed**. Every finding stores the response excerpt that justifies it, so a reviewer can disagree with the judge.
- Sensitive values **never reach telemetry**. System prompts, target responses and endpoint credentials are scrubbed before any log, metric or span leaves the process.

### Non-goals

- **Not a proxy or a runtime guardrail.** It tests prompts offline; it does not sit in your request path blocking traffic.
- **Not a scanner for endpoints you don't own.** Endpoint targets require explicit ownership confirmation, and requests to private address space are blocked outright.
- **No multi-tenancy or user accounts** in this version. It is designed to be deployed per team, not operated as a shared service.
- **Not a benchmark.** A grade is a signal about one prompt against one corpus, not a ranking of model safety.

## Responsible use

promptproof is built for testing prompts and applications **you own or are authorised to test**. Prompt targets run entirely against a model you supply credentials for, which is why they are the default. Endpoint targets require you to confirm ownership, and the application refuses requests to loopback, link-local and private IP ranges so it cannot be used as an SSRF primitive against internal infrastructure. The probe corpus is published in this repository so that what the tool sends is auditable rather than hidden.

## Architecture

```
  ┌──────────┐       ┌───────────┐       ┌────────────┐
  │   web    │ HTTP  │    api    │  SQL  │  postgres  │
  │ React+TS │──────>│  FastAPI  │──────>│            │
  └──────────┘       └───────────┘       └────────────┘
                                               ^
                                               │ claims scans
                                         ┌───────────┐       ┌──────────────┐
                                         │  worker   │ HTTP  │  target llm  │
                                         │ scan exec │──────>│  + judge llm │
                                         └───────────┘       └──────────────┘
```

Four services, no message broker: the `scans` table *is* the job queue, claimed with `SELECT ... FOR UPDATE SKIP LOCKED`. The trace context created when a scan is submitted is persisted on the scan row and resumed by the worker, so one trace ID spans the API request, the queue wait, and every probe and judge call.

Full design rationale and decision records: [docs/architecture.md](docs/architecture.md).

## Running it locally

Requires Docker and a [Groq](https://console.groq.com) API key (the free tier is enough).

```bash
git clone https://github.com/Amar-Ag/promptproof.git
cd promptproof
cp .env.example .env
make up
```

One command brings up the frontend, API, worker and Postgres, and applies migrations. The UI is then on `http://localhost:5173` and the API docs on `http://localhost:8000/docs`.

```bash
make test
make e2e
make down
```

## Deployment

Deployed on Fly.io, promoted through two apps:

| Environment | URL | How it deploys |
|---|---|---|
| dev | _TBD_ | Automatically, on every merge to `main` |
| production | _TBD_ | Manually, via an approved workflow run |

Rollback procedure: [docs/runbooks/rollback.md](docs/runbooks/rollback.md).

## Observability and on-call

The scan journey is instrumented with OpenTelemetry — metrics, traces and structured logs sharing one trace ID — exported to Logfire. An alert fires on user-visible impact (scans stuck in the queue beyond their SLO, or an elevated scan failure rate) rather than on infrastructure noise, and triggers an **on-call agent**: a PydanticAI agent that reads the relevant telemetry, works through [the runbook](docs/runbooks), applies the safe remediation it is authorised for, and files an issue documenting what it found and did.

## Documentation

| Document | Contents |
|---|---|
| [AGENTS.md](AGENTS.md) | Entry point for agents and humans working in this repo |
| [docs/architecture.md](docs/architecture.md) | Services, data model, trace propagation, decisions |
| [docs/specs](docs/specs) | Product specification and acceptance criteria |
| [docs/agents](docs/agents) | The agent team, their responsibilities and handoffs |
| [docs/process.md](docs/process.md) | How work moves from issue to production |
| [docs/runbooks](docs/runbooks) | Operational procedures, including on-call remediation |
| [docs/adr](docs/adr) | Architecture decision records |

## About

Built as the final project for the [DataTalksClub AI Dev Tools Zoomcamp](https://github.com/DataTalksClub/ai-dev-tools-zoomcamp) — specified before it was written, and built by a defined team of agents working against a public task tracker.

Licensed under the [MIT License](LICENSE).
