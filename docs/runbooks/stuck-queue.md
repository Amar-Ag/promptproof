# Runbook — `scan-queue-stalled`

> **Status:** commands unverified until Phase 5. Do not treat the SQL as tested yet.

## Symptom

A user submitted a scan and it is sitting in `pending`, or stuck in `running` without findings
appearing. The alert fires when `promptproof.queue.oldest_age_seconds` exceeds 120 seconds for
5 minutes — meaning someone has been watching a scan that is not progressing.

## Blast radius

Scans do not complete. Nothing is lost: queued scans are durable rows and will run once a
worker is available. Existing reports are unaffected, and the API and UI stay up.

## Diagnosis

Run these in order. Stop at the first one that explains the symptom.

**1. Is any worker alive?**

```bash
fly status --app promptproof-prod
```

No running `worker` process → cause is **D1 (no worker)**.

**2. How deep and how old is the queue?**

```sql
SELECT status,
       count(*)                                        AS scans,
       max(extract(epoch from now() - queued_at))::int AS oldest_age_s
  FROM scans
 WHERE status IN ('pending', 'running')
 GROUP BY status;
```

`pending` growing with a live worker → **D3 (worker not claiming)**.

**3. Are there scans holding an expired lease?**

```sql
SELECT id, locked_by, attempts,
       extract(epoch from now() - locked_at)::int AS lease_age_s
  FROM scans
 WHERE status = 'running'
   AND locked_at < now() - interval '5 minutes'
 ORDER BY locked_at;
```

Rows returned → cause is **D2 (stale lease)**. This is the common case: a worker died
mid-scan.

**4. Is the worker claiming but failing to progress?**

Open the trace for the oldest stuck scan and compare `queue.wait_ms` on `scan.claim` against
the `probe.execute` spans; then check `promptproof.target.errors` and
`promptproof.judge.errors` by reason. Sustained `429` → **D4 (provider rate limited)**.

**5. Is the database refusing connections?**

```bash
fly logs --app promptproof-prod | grep -i "could not connect\|too many clients"
```

Hits → **D5 (database saturated)**.

## Remediations

### D2 — stale lease · `AGENT`

Reclaim scans whose lease expired, within the attempt limit. Idempotent: the predicate
excludes anything already reset or exhausted.

```sql
UPDATE scans
   SET status = 'pending', locked_at = NULL, locked_by = NULL
 WHERE status = 'running'
   AND locked_at < now() - interval '5 minutes'
   AND attempts < 3
RETURNING id;
```

Then mark genuinely exhausted scans failed, so the user sees an honest error instead of waiting
forever:

```sql
UPDATE scans
   SET status = 'failed',
       finished_at = now(),
       error = 'Scan abandoned after repeated worker failures'
 WHERE status = 'running'
   AND locked_at < now() - interval '5 minutes'
   AND attempts >= 3
RETURNING id;
```

Confirm `queue.oldest_age_seconds` falls within two minutes. If it does not, escalate.

### D4 — provider rate limited · `AGENT`

Lower `SCAN_CONCURRENCY` to 2 and let the backlog drain. Verify the error rate falls; the
original value is restored by a human once the provider quota recovers.

### D1 — no worker · `HUMAN`

Restart the worker process group. If it crash-loops, read the logs, then roll back via
[rollback.md](rollback.md).

### D3 — worker not claiming · `HUMAN`

Worker is up but claiming nothing: usually a migration mismatch or a corpus validation failure
at startup. Check `/readyz` on the API and the worker's startup logs.

### D5 — database saturated · `HUMAN`

Check connection count against the plan limit. Likely a pool misconfiguration in a recent
deploy; consider rolling back.

## Agent boundaries

The on-call agent may apply **D2** and **D4** and nothing else. For anything else it files an
issue with its diagnosis and escalates. It has no credentials to deploy, restart machines,
migrate, or delete data.

Every run files an issue recording: the alert, which diagnostic branch matched, the SQL it ran,
the row IDs affected, and whether queue age recovered.

## Exercise log

| Date | How induced | Outcome |
|---|---|---|
| 2026-11-12 | Worker stopped, three scans queued | **Fired and resolved correctly.** Details below. |

### 2026-11-12 — first drill

The worker was stopped and three scans submitted, so the queue genuinely stalled rather than
being simulated.

The alert fired **435 seconds** after the first scan was queued — the 120-second threshold
plus the 300-second sustain window, which is what those numbers are for. It was delivered to
both configured targets:

```
/oncall  signed=true   scan-queue-stalled  value=427.794  waiting=3
/human   signed=false  scan-queue-stalled  value=427.794  waiting=3
```

Then the worker was restarted. The queue drained and the alert **resolved** on the next
evaluation. Exactly two webhook deliveries were recorded in total — one alert to two targets,
not one every fifteen seconds. That is the firing-edge behaviour working: an alert that
repeats while the problem persists is an alert that gets muted.

**What the drill changed.** Planning it exposed that the evaluator was running inside the
worker, which cannot work: this rule exists to report that the worker has stopped picking work
up, and an evaluator inside the worker is watching for its own death. It would have passed
every test that did not stop the worker, and failed silently the first time it mattered. The
evaluator now runs in the API, which outlives the worker.

| Signal | Confirmed |
|---|---|
| Fires on a sustained breach, not a single sample | yes |
| Does not repeat while firing | yes, 2 deliveries total |
| Resolves when the condition clears | yes |
| Delivered to the on-call endpoint, signed | yes |
| Delivered to a human channel | yes |
| Payload carries no scan content | yes |
