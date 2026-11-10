# Process

How work moves from an idea to production in this repository, and what each agent is expected
to do at each step. This document is binding: a pull request that skipped a step here should
be sent back regardless of code quality.

## Principles

1. **Specify before building.** No feature code is written until the behaviour it implements
   has acceptance criteria written down in [docs/specs](specs) and captured in an issue. If a
   specification turns out to be wrong, change the specification first, then the code.
2. **The tracker is the state of the project.** Not the chat history, not someone's memory.
   If work is happening, there is an issue in `In progress` for it.
3. **One agent, one responsibility.** Agents hold a defined role with defined ownership (see
   [docs/agents](agents)). An agent that needs work outside its ownership hands off rather
   than reaching across the boundary.
4. **Small, reviewable changes.** One issue, one branch, one pull request. If a PR touches
   more than one issue's acceptance criteria, it is too big.
5. **Humans decide, agents execute.** The human approves the specification, approves the
   production deploy, and approves anything irreversible. Everything between those points is
   delegated.

## Issue-first flow

```
  idea
   │
   ▼
  spec in docs/specs/   ──────── written by spec-analyst, approved by human
   │
   ▼
  issues on the board   ──────── one per acceptance-criteria cluster, labelled by area
   │
   ▼
  Ready ──▶ In progress ──▶ In review ──▶ Done
              │                 │
              │                 └── reviewer agent + CI must both pass
              └── implementing agent comments its plan on the issue before coding
```

### Board columns

| Column | Means |
|---|---|
| `Backlog` | Captured, not yet specified or not yet scheduled |
| `Ready` | Has acceptance criteria, is unblocked, any agent in the owning role can pick it up |
| `In progress` | Exactly one agent is working on it, and has commented its plan on the issue |
| `In review` | PR open, awaiting reviewer agent and CI |
| `Done` | Merged, acceptance criteria verified, deployed to dev |

### Definition of ready

An issue may enter `Ready` only when it states:

- the user-visible behaviour being added or changed,
- acceptance criteria as a checklist, each one independently verifiable,
- which spec section it implements,
- which role owns it,
- its dependencies, if any.

### Definition of done

An issue may be closed only when:

- every acceptance criterion is checked off, with the commit or test that satisfies it,
- `make test` passes locally and in CI,
- new behaviour has a test that would fail against the previous code,
- documentation affected by the change is updated **in the same PR**,
- no secret or user content was added to a log, span, or metric,
- the reviewer agent has approved,
- it is deployed to dev and the behaviour was observed working there.

## Branches, commits, pull requests

- Branch: `<type>/<issue>-<slug>` — for example `feat/14-scan-queue-claim`,
  `fix/31-stale-lock-reclaim`. Types: `feat`, `fix`, `chore`, `docs`, `test`, `ops`.
- Commits: conventional commits, present tense, scoped to the service —
  `feat(worker): claim queued scans with SKIP LOCKED`. Reference the issue in the body with
  `Refs #14`, and use `Closes #14` only on the commit that completes it.
- Pull requests must contain: a link to the issue, the acceptance criteria reproduced as a
  checklist with evidence against each, what was tested and how, and anything deliberately
  left out. A PR body that does not let a reviewer verify acceptance without reading the diff
  twice is incomplete.

## Handoffs

Agents coordinate through the tracker, not through implicit shared memory. A handoff is a
comment on the issue containing:

- what was completed and where (files, commits),
- what the receiving role needs to do,
- anything discovered that changes the spec or the architecture,
- explicitly, what was *not* done.

A handoff that cannot be understood without the originating conversation is a defective
handoff. The receiving agent is expected to say so rather than guess.

## Escalation

An agent stops and escalates to the human when it hits any of these, rather than deciding
alone:

- the specification is ambiguous in a way that changes user-visible behaviour,
- a locked technology choice in [AGENTS.md](../AGENTS.md) would need to change,
- a change would send user content or secrets to a third party,
- a change is irreversible: destroying data, rotating a credential, deploying to production,
- the work turns out to be substantially larger than the issue describes.

## Build phases

The project is built in phases. Each phase ends with something demonstrable; nothing is left
"to be wired up later".

| Phase | Delivers | Done when |
|---|---|---|
| 0 — Specify | Spec with acceptance criteria, agent team, process, architecture, public tracker populated | A reader can tell what will be built and how work will flow, without reading code |
| 1 — Walking skeleton | Compose stack: web, api, worker, postgres. Create a target, queue a scan, see its status change in the UI | `make up` then a scan transitions `pending` to `completed` with stub probes |
| 2 — The product | Probe corpus, target execution, judge verdicts, severity scoring, report UI, SSRF guard | A real scan against a real prompt produces evidence-backed findings |
| 3 — Verify | Unit, integration, frontend and e2e tests behind `make test` and `make e2e` | All green from a clean clone |
| 4 — Ship | CI on every PR, CD to dev on merge, gated manual production deploy, documented rollback | A merge reaches dev with no human action; production needs one approval |
| 5 — Operate | OTel traces, metrics, structured logs with a shared trace ID, scrubbing, user-impact alert, on-call agent | An induced failure fires an alert and the on-call agent remediates and files an issue |

## Human checkpoints

| Checkpoint | Who | Why |
|---|---|---|
| Specification approval | Human | Everything downstream is judged against it |
| Architecture change (ADR) | Human | Locked choices have consequences for several criteria |
| Production deploy | Human | Deliberately manual; this is also a graded requirement |
| Credential handling, data destruction | Human | Irreversible |
