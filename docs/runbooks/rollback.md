# Runbook — Rolling back a release

No alert triggers this. It is used when a deploy made things worse: the
failure-rate alert fires right after a release, the UI breaks, or `/readyz` goes red.

**Rollback is a `HUMAN` action.** The on-call agent has no credentials to deploy or roll back.
If its diagnosis points at a bad release, it files an issue and escalates.

## Decide quickly

Roll back first and diagnose afterwards if either holds:

- `/readyz` is failing on the new release, or
- the scan failure rate is elevated and the onset matches the release time.

A forward fix is only right when the problem is understood *and* the fix is small enough to
ship inside fifteen minutes. On a free instance a deploy takes several minutes, so "just push
a fix" is slower than it sounds.

## Procedure

### 1. Find the last good deploy

Dashboard → the service → **Events**. Each deploy shows its commit and status. Note the id of
the last one that was `live` and healthy.

Or by API:

```bash
curl -sS -H "Authorization: Bearer $RENDER_API_KEY" \
  "https://api.render.com/v1/services/$SERVICE_ID/deploys?limit=10" \
  | jq -r '.[] | "\(.deploy.id)  \(.deploy.status)  \(.deploy.commit.id[0:7])  \(.deploy.finishedAt)"'
```

### 2. Check whether the bad release migrated the database

This decides everything below.

```bash
curl -sS -H "Authorization: Bearer $RENDER_API_KEY" \
  "https://api.render.com/v1/services/$SERVICE_ID/logs?limit=200" \
  | grep -i "running upgrade"
```

Migrations are forward-only ([AGENTS.md](../../AGENTS.md#hard-rules)), so rolling the code back
does not undo a schema change.

### 3a. No migration — roll back the image

```bash
curl -sS -X POST \
  -H "Authorization: Bearer $RENDER_API_KEY" \
  -H "Content-Type: application/json" \
  -d "{\"deployId\": \"$LAST_GOOD_DEPLOY_ID\"}" \
  "https://api.render.com/v1/services/$SERVICE_ID/rollback"
```

Or **Rollback to this deploy** on the chosen deploy in the dashboard.

### 3b. The bad release migrated — stop and think

Migrations are written to be backward-compatible with the previous release, so rolling the
image back is normally still correct. If the migration was *not* backward-compatible, that is a
defect in the migration: **do not run a down-migration against real data.** Fix forward with a
new migration instead.

To stop further damage while you decide, suspend the service:

```bash
curl -sS -X POST -H "Authorization: Bearer $RENDER_API_KEY" \
  "https://api.render.com/v1/services/$SERVICE_ID/suspend"
```

Suspending stops the worker too, so in-flight scans stop progressing. They are not lost — the
lease expires and they are reclaimed when the service returns.

### 4. Verify

- `/healthz` and `/readyz` both green
- a test scan submits and reaches `completed`
- the failure rate falls back under 20% within ten minutes
- the running deploy is the one you intended

On a free instance the first request after a rollback wakes the container, so allow about a
minute before concluding it failed.

### 5. Record it

Open an issue with the bad deploy id, the symptom, what you rolled back to, whether a migration
was involved, and what will prevent a repeat. Add a line to the Exercise log below.

## Neon: rolling back data, not code

A bad *migration* is a different problem from a bad *release*, and Neon branches help:

```bash
neon branches list --project-id rough-heart-88913963
```

A branch can be restored to a point in time within its history retention. That is a data
recovery tool, not a deploy tool — reach for it only when a migration corrupted or destroyed
data, and never as a substitute for rolling the code back.

## Development environment

The same commands with the dev service id. Dev may also be fixed forward freely, since nobody
depends on it: if the environments have diverged, redeploy dev from `main` rather than rolling
it back.

## Exercise log

| Date | Deploy | Reason | Migration involved | Outcome |
|---|---|---|---|---|
| _pending_ | — | Deliberate drill | No | _to be recorded on the first deploy_ |
