# Runbooks

Operational procedures. These are written to be followed by the **on-call agent** as well as by
a human, which sets the house style:

1. **One runbook per alert.** Every alert in [docs/observability.md](../observability.md#alerts)
   links to exactly one runbook. An alert with no runbook gets deleted, not muted.
2. **Symptom first.** A runbook opens with the user-visible symptom, so whoever opens it can
   confirm they are in the right place before doing anything.
3. **Diagnosis before action.** Numbered checks, each with the exact command and what its output
   means. No step says "investigate".
4. **Remediations are explicitly labelled.** Each one is marked `AGENT` (the on-call agent may
   apply it unattended) or `HUMAN` (requires a person). An agent that cannot find an `AGENT`
   remediation for what it has diagnosed escalates; it never improvises.
5. **Every remediation is idempotent.** Running it twice must be harmless, because retries and
   duplicate alerts happen.
6. **Outcomes get recorded.** When a runbook is exercised — including deliberately, as a drill —
   the date and what happened goes in its Exercise log.

| Runbook | Alert |
|---|---|
| [stuck-queue.md](stuck-queue.md) | `scan-queue-stalled` |
| [scan-failures.md](scan-failures.md) | `scan-failure-rate-high` |
| [rollback.md](rollback.md) | No alert — used after a bad deploy |

> **Status:** written in Phase 0 against the specified design. Commands are verified, and
> Exercise logs filled in, during Phase 5. Any step still unverified is marked as such.
