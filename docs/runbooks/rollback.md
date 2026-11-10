# Runbook — Rolling back a release

> **Status:** commands unverified until Phase 4, when a rollback is performed for real and the
> Exercise log below is filled in.

No alert triggers this. It is used when a deploy made things worse: the failure-rate alert fires
immediately after a release, the UI breaks, or `/readyz` goes red.

**Rollback is a `HUMAN` action.** The on-call agent has no credentials to deploy or roll back.
If its diagnosis points at a bad release, it files an issue and escalates.

## Decide quickly

Roll back first and diagnose afterwards if either holds:

- `/readyz` is failing on the new release, or
- the scan failure rate is elevated and the onset matches the release time.

A forward fix is only the right call when the problem is understood *and* the fix is small
enough to ship inside 15 minutes.

## Procedure

**1. Identify the releases.**

```bash
fly releases --app promptproof-prod
```

Note the current version and the last known-good one.

**2. Check whether the bad release migrated the database.**

```bash
fly logs --app promptproof-prod | grep -i "alembic\|running upgrade"
```

This decides everything below. Migrations are forward-only
([AGENTS.md](../../AGENTS.md#hard-rules)), so a schema change cannot be undone by rolling back
code.

**3a. No migration in the bad release — roll back the image.**

```bash
fly deploy --app promptproof-prod --image <image-ref-of-last-good-release>
```

**3b. The bad release migrated — stop and think.**

Migrations are written to be backward-compatible with the previous release, so rolling back the
image alone is normally correct and safe. If the migration was *not* backward-compatible, that
is a defect in the migration: do not attempt a down-migration against production data. Scale
the worker to zero to stop further damage, then fix forward with a new migration.

```bash
fly scale count worker=0 --app promptproof-prod
```

**4. Verify.**

- `/healthz` and `/readyz` both green
- a test scan submits and reaches `completed`
- the failure rate falls back under 20% within 10 minutes
- the running release is the one you intended

**5. Record it.**

Open an issue with: the bad release ID, the symptom, the rolled-back-to release, whether a
migration was involved, and what will prevent a repeat. Add a line to the Exercise log below.

## Dev environment

The same commands with `--app promptproof-dev`. Dev may also be fixed forward freely, since no
one depends on it — if the two environments have diverged, redeploy dev from `main` rather than
rolling it back.

## Exercise log

| Date | Release | Reason | Migration involved | Outcome |
|---|---|---|---|---|
| _Phase 4_ | — | Deliberate drill | No | _to be recorded_ |
