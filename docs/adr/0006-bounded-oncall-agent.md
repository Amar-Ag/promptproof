# ADR-0006 — An on-call agent whose authority is its toolset

- **Status:** Accepted
- **Date:** 2026-11-12
- **Deciders:** sre, human

## Context

Two alert rules fire at nobody in particular. `scan-queue-stalled` and
`scan-failure-rate-high` both have runbooks, and both runbooks begin with the same handful of
mechanical steps: look at the queue, look at who claimed what, look at why things failed. Those
steps are deterministic, they are the same every time, and they are exactly what a human does
at 3am before doing anything interesting.

The interesting part — deciding *which* branch of the runbook applies — is judgement over a
handful of signals. That is a reasonable thing to automate with a model, and an unreasonable
thing to automate with an `if` chain, because the branches are distinguished by things like
"are the failures concentrated on one target, or spread across all of them".

The risk is obvious and it is not hypothetical. An agent that can reach the database at 3am,
acting on a judgement call, is one bad inference away from doing real damage. The usual
mitigation — telling the model what it must not do — is a request, not a boundary. A model
that has been instructed not to delete anything and has a general SQL tool can still delete
something; the instruction is just text, and this is a project whose entire subject matter is
text that persuades models to ignore their instructions.

## Decision

Give the agent a **closed set of six typed tools** and no general-purpose access. Its
authority is defined by `packages/core/src/promptproof_core/oncall/remediations.py` and nowhere
else.

| Tool | Kind |
|---|---|
| `inspect_queue` | read-only |
| `inspect_recent_failures` | read-only |
| `inspect_worker_activity` | read-only |
| `reclaim_expired_leases` | write, bounded, idempotent |
| `fail_exhausted_scans` | write, bounded, idempotent |
| `requeue_transient_failures` | write, bounded, idempotent |

There is no general query tool, no shell, no deploy, no migration, no delete, and no way to
change configuration. Each write is a single fixed `UPDATE` with a predicate that excludes rows
already in the target state, so running it twice changes nothing the second time.

The consequence is that **the agent's worst case is bounded by construction**. If the model is
confused, or injected, or simply wrong, the most it can do is reclaim a lease that did not need
reclaiming or requeue a scan that did not need requeuing — both of which the worker itself does
routinely. There is no prompt that makes a tool exist.

Everything outside the set is handled by one output field: `escalate`. The agent's move when it
cannot fix something is to say so, in writing, with what a human needs to do.

Every run files a GitHub issue, **including runs that changed nothing**. An on-call system that
is silent when healthy is indistinguishable from one that is broken.

## Alternatives considered

**Rules in code, no model.** Deterministic and auditable, and genuinely sufficient for the
stale-lease branch. It falls down on diagnosis: `scan-failure-rate-high` branches on whether
failures are concentrated or spread, transient or permanent, and whether onset matches a
deploy. Encoding that is a thicket of thresholds that would need tuning against incidents we
have not had yet. The split adopted here — model for diagnosis, fixed code for action — keeps
the determinism where the damage would be.

**A model with a general SQL tool.** Far more capable, and the capability is the problem. The
blast radius becomes "anything SQL can do", and the only thing standing between an incident and
a `DELETE` is the prompt. For this project that is an especially poor argument, since the
product exists to demonstrate that prompts do not hold.

**Prompt-only restriction.** Same toolset as a general agent, with instructions not to misuse
it. Rejected for the reason above: a restriction that lives in the context window is a
restriction an attacker can address.

**Full autonomy including restarts and rollbacks.** Would close the loop on the two branches
that currently escalate (`D1 no worker`, `D5 bad release`). Rejected because those are exactly
the actions whose failure mode is an outage, and because a human who is never woken stops
knowing how the system behaves.

## Consequences

**Good.** The security argument is structural and can be stated in one line: the agent cannot
do what it has no tool for. The remediations are ordinary tested functions, so they are covered
by the normal suite rather than by evaluating model behaviour. Idempotency means duplicate
alerts and retries are safe.

**Bad.** The agent cannot resolve the single most common serious cause — the worker being
absent — because starting a process is outside its authority. It escalates, which is correct
but is not resolution. Adding a capability means writing a tool and a test, deliberately: this
is friction by design, and it will feel like friction.

**Be honest about what this is not.** The agent holds no *credential* restriction. It runs
inside the API process and shares the database role Alembic uses for migrations, so at the
connection level it could do anything that role can. The control is the toolset: there is no
tool that expresses a migration, a delete or a deploy, and no general query tool through which
one could be improvised. That is a real boundary, but an application-level one. A restricted
database role would make the guarantee structural rather than architectural, and is the obvious
next step if this agent is ever given more reach.

**Watch for.** The agent runs synchronously inside the alert webhook, so a run holds the
delivery open for as long as it takes (see `ONCALL_TIMEOUT_SECONDS`). The human channel is
therefore notified *first*. If agent runs grow much slower, this should move to a background
task rather than having the timeout raised again.
