# ADR-0005 — Deploy on a free tier, co-locating the worker

- **Status:** Proposed — supersedes the deployment row of the locked table in [AGENTS.md](../../AGENTS.md)
- **Date:** 2026-11-11
- **Deciders:** sre, human

## Context

[AGENTS.md](../../AGENTS.md) locks deployment to Fly.io with two apps. That choice assumed a
free allowance that no longer exists: Fly requires a payment method before it will provision
anything, and this is a bootcamp submission the owner does not want to pay to host.

Surveying the free tiers in November 2026 turns up one constraint that decides the design:

> **No mainstream platform offers a free always-on background worker.**

Render states background workers have no free instance type. Koyeb has required a card since
February 2026. Railway's free plan is credit-limited and pauses when the credit runs out.
Render's free PostgreSQL additionally *expires 30 days after creation*, which would take the
demo down partway through a cohort with no warning.

[ADR-0001](0001-separate-worker-process.md) put scan execution in its own process for reasons
that have not gone away: model latency must not compete with request serving, and work must
survive a deploy. The question is whether those reasons justify paying, or whether the
separation can be preserved in a cheaper shape.

## Decision

Deploy on free infrastructure, and **co-locate the worker with the API in one container**
while keeping it a separate process.

| Concern | Choice |
|---|---|
| Application | Render free web service, built from this repository's Dockerfile |
| Database | Neon free Postgres — one project per environment |
| Frontend | Built at image build time and served by the API as static files |
| Fallback | Hugging Face Spaces, if Render asks for a card at signup |

A small supervising entrypoint starts `uvicorn` and `promptproof-worker` side by side. They
remain two processes with two lifecycles, talking through the Postgres queue exactly as they
do under Compose.

Neon rather than Render's own database specifically because Render's free tier expires in 30
days. Neon's free project does not.

## Consequences

**Good**

- It costs nothing, and needs no card.
- **The trace-propagation contract is untouched.** Context still crosses a process boundary
  through `scans.traceparent`, so the thing the observability criterion turns on is still
  genuinely exercised rather than quietly simplified away.
- The queue, the lease and the reclaim path all still matter, because the worker is still a
  separate process that can die on its own.
- Compose is unchanged, so local development keeps the four-service topology and nothing about
  the architecture has to be re-explained.
- One public URL per environment and no CORS, since the API serves the frontend.

**Bad — and these are real**

- **A free web service sleeps after 15 minutes of inactivity.** The worker sleeps with it. A
  scan submitted to a sleeping service wakes it, which costs roughly a minute before the first
  probe runs. Scans queued and then left alone do not progress until something wakes the
  service.
- **Model latency now shares a container with request serving.** This is the exact coupling
  ADR-0001 removed. At one concurrent user it does not matter; under load it would.
- **A deploy restarts both processes together.** In-flight scans are abandoned and recovered by
  lease expiry rather than continuing, so a deploy during a scan costs a retry.
- Two platforms to configure instead of one, and the database lives outside the application
  host.
- Render free instances are documented as not for production use. This is a demo, which is
  what they are for.

## Alternatives rejected

- **Pay for Fly.** Rejected by the owner. It remains the right answer the moment this is
  anything but a demo, and nothing here forecloses it: the image and the process split are
  already what Fly would deploy.
- **Worker as a scheduled GitHub Actions job.** Free and genuinely always-available, but a
  cron floor of five minutes makes a scan feel broken, and it would move execution outside the
  application entirely.
- **Run scans in the request.** Would delete the queue, the lease and the cross-process trace,
  which is most of what this project is demonstrating. Cheaper to host and much worse to show.
- **Oracle Cloud always-free VM.** Genuinely free and would run Compose unchanged, but requires
  a card for identity verification and capacity for new accounts is unreliable.

## Revisit when

Anyone other than the owner depends on an instance staying warm, or a second concurrent user
appears. At that point the split is already in place — it only has to move back into its own
service.
