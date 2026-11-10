# Specifications

Behaviour is specified here **before** it is implemented. Every issue on the board links to a
requirement in one of these documents, and every requirement carries acceptance criteria that
can be turned into a test without further clarification.

| Spec | Scope |
|---|---|
| [001 — Targets](001-targets.md) | Registering, versioning and validating what gets scanned |
| [002 — Scanning](002-scanning.md) | The probe corpus, queueing, execution, judging, grading |
| [003 — Reporting](003-reporting.md) | What the user sees: scan list, live progress, report, comparison |
| [004 — Operability](004-operability.md) | Running it: one-command stack, tests, CI/CD, telemetry, alerting, on-call |

## Conventions

- Requirements are identified `R-<spec>-<n>`, for example `R-002-3`. IDs are stable and are
  never reused; a withdrawn requirement is marked `Withdrawn` and kept.
- Each requirement states a **user story**, the **behaviour** including failure behaviour, and
  **acceptance criteria** as a checklist.
- Acceptance criteria are written so that a reader can tell whether they hold by observing the
  system, not by reading the implementation.
- Priority: `MUST` ships in v1. `SHOULD` ships if the phase has room. `LATER` is explicitly out
  of scope for v1 and recorded only so it stops being re-litigated.

## Glossary

| Term | Meaning |
|---|---|
| **Target** | The thing under test: a system prompt plus a model, or an HTTP endpoint of the user's own app. Immutable and versioned. |
| **Target family** | The lineage of versions of one logically-same target, used for comparison. |
| **Probe** | One adversarial input plus the criteria that decide whether it succeeded. |
| **Suite** | A named group of probes sharing an attack category, e.g. `direct-injection`. |
| **Scan** | One execution of a chosen set of suites against one target version. |
| **Finding** | The outcome of one probe in one scan: `exploited`, `resisted`, or `errored`. |
| **Judge** | An independent model that decides a probe's outcome from the target's response and the probe's success criteria. |
| **Grade** | A letter A–F summarising a scan, derived only from exploited findings. |
| **Exploited** | The target did what the attacker asked instead of what its own instructions said. |

## Out of scope for v1

Recorded so these do not resurface as scope creep mid-phase:

- User accounts, authentication, multi-tenancy. Deploy one instance per team.
- Scheduled or recurring scans.
- A CI integration or SDK for failing a build on regression.
- User-authored probes through the UI. The corpus is edited in the repository.
- Scanning anything other than a chat-completion-shaped interface.
- Any model provider other than Groq.
