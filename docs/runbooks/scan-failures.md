# Runbook — `scan-failure-rate-high`

> **Status:** commands unverified until Phase 5.

## Symptom

Scans are completing, but as failures or as reports full of errored probes rather than useful
findings. The alert fires when the failed share of scans exceeds 20% over 10 minutes. The user
sees an error page, or a report whose grade is missing.

## Blast radius

Users get no usable verdict. Target data is unaffected; failed scans are re-runnable.

## Diagnosis

**1. What is failing, and with what reason?**

```sql
SELECT error, count(*)
  FROM scans
 WHERE status = 'failed'
   AND finished_at > now() - interval '30 minutes'
 GROUP BY error
 ORDER BY 2 DESC;
```

**2. Is it the target, the judge, or us?**

Compare `promptproof.target.errors` and `promptproof.judge.errors` by `reason` over the same
window.

- Mostly `429` → **D1 (provider rate limited)**
- Mostly `timeout` → **D2 (provider degraded)**
- Mostly `parse` on the judge → **D3 (judge output malformed)**
- Mostly guard refusals → **D4 (endpoint target unreachable or unsafe)**
- A spike starting at a deploy → **D5 (bad release)**

**3. Is it one target or all of them?**

```sql
SELECT t.name, t.kind, count(*) AS failures
  FROM scans s JOIN targets t ON t.id = s.target_id
 WHERE s.status = 'failed'
   AND s.finished_at > now() - interval '30 minutes'
 GROUP BY 1, 2
 ORDER BY 3 DESC;
```

Concentrated on one endpoint target → **D4**, and it is that user's endpoint, not our system.

**4. Did this start at a deploy?**

```bash
fly releases --app promptproof-prod
```

Onset matching a release → **D5**.

## Remediations

### D1 — provider rate limited · `AGENT`

Reduce `SCAN_CONCURRENCY` to 2. Requeue scans that failed on a transient reason, within the
attempt limit — idempotent, since the predicate excludes anything already requeued:

```sql
UPDATE scans
   SET status = 'pending', locked_at = NULL, locked_by = NULL, error = NULL
 WHERE status = 'failed'
   AND finished_at > now() - interval '30 minutes'
   AND error ILIKE '%rate limit%'
   AND attempts < 3
RETURNING id;
```

### D2 — provider degraded · `AGENT`

Requeue as above for timeout reasons. If the failure rate does not recover within 15 minutes,
escalate: the provider is down and there is nothing to fix here.

### D3 — judge output malformed · `HUMAN`

Usually a judge model change or a prompt-format regression. Inspect a failing
`judge.evaluate` span's reason code, then fix the judge prompt or pin the previous model.

### D4 — endpoint target unreachable · `AGENT`, bounded

Not our outage. Do not requeue. Confirm it is confined to one target, then record it in the
issue so the owner can be told. Escalate only if multiple unrelated targets are affected,
which would point at our egress.

### D5 — bad release · `HUMAN`

Roll back via [rollback.md](rollback.md), then open an issue with the failing release ID.

## Agent boundaries

The on-call agent may apply **D1**, **D2**, and the diagnosis-only path of **D4**. It never
rolls back, changes the judge configuration, or edits a target. Every run files an issue
recording which branch matched, what it changed, the rows affected, and whether the failure
rate recovered.

## Exercise log

| Date | How induced | Outcome |
|---|---|---|
| _Phase 5_ | Point a stub provider at a 429-always response | _to be recorded_ |
