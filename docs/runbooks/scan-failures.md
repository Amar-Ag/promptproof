# Runbook — `scan-failure-rate-high`

> **Status:** exercised. See the drill log at the foot of this page.

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

- Mostly `429` → **D1 (provider rate limited)** or **D6 (allowance spent)**; the two are
  distinguished below and the fix differs
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

### First, tell the two 429s apart

Both a momentary rate limit and a spent daily allowance arrive as HTTP 429, and the useful
response is opposite in each case. Groq says which in its error message, and the code acts on
it (`_names_a_daily_limit` in `provider.py`):

| Limit | Named as | Classified | Behaviour |
|---|---|---|---|
| Tokens or requests per minute | `(TPM)`, `(RPM)`, "per minute" | `ProviderRateLimited`, transient | retried with backoff, honouring `Retry-After` |
| Tokens or requests per day | `(TPD)`, `(RPD)`, "per day" | `ProviderQuotaExhausted`, **not** transient | not retried; stops the scan |

A body that cannot be read is treated as the per-minute case: retrying costs a wait, whereas
refusing a scan that could have run costs the user their scan.

### D1 — provider rate limited (per minute) · `AGENT`

**Normally there is nothing to do.** `TokenBudget` in `provider.py` paces against the
allowance the provider reports on every response — `x-ratelimit-remaining-tokens` and
`x-ratelimit-reset-tokens` — so calls wait before being refused rather than after. A handful
of `429`s in the logs with no errored findings is that mechanism working.

**Scans are slower than they used to be, on purpose.** Pacing trades wall-clock time for
results that exist. On a free tier a full-corpus scan now spends most of its time waiting for
the per-minute window to roll over — a single `provider.pacing` line reading
`remaining_tokens=375 waiting_seconds=57` is the mechanism choosing a minute of waiting over a
handful of probes that would have come back undetermined. If someone reports scans as "slow",
check for those lines before treating it as a fault.

If errored findings *are* appearing, the burst is outrunning the pacing. Lower
`SCAN_CONCURRENCY` (default 2, chosen against a measured 8,000 tokens per minute per model;
the reasoning is in `settings.py`). Scans that already failed can be re-run by the user; there
is no requeue action — see [#45](https://github.com/Amar-Ag/promptproof/issues/45) for why a
transient reason never reaches a scan row today.

### D6 — daily allowance spent · `HUMAN`

The scan stopped on its own and says so:

```sql
SELECT id, status, grade, error
  FROM scans
 WHERE error ILIKE '%allowance%'
   AND finished_at > now() - interval '1 day';
```

- `status = 'failed'` with that error: nothing was graded, and the cause is the account rather
  than the target. Nothing to fix; the allowance resets.
- `status = 'completed'` with that error: partial. The findings it has are genuine and worth
  reading; the gap is how far the day's allowance got.

The responder cannot fix either, and deliberately has no action for it: the only remedies are
waiting, raising the tier, or pointing `JUDGE_MODEL` at a model with its own allowance. Note
that the judge is the expensive call — about 410 tokens against 140 for the target — so it is
the allowance that runs out first.

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

The on-call agent may diagnose any of these and has **no remediation for this alert at all**:
a scan only fails when it is abandoned, its target is gone, or the day's allowance ran out, and
none of those is something it can fix. It inspects, reports the dominant reason and whether the
failures are concentrated on one target, and escalates. It never rolls back, changes the judge
configuration, or edits a target. Every run files an issue.

## Exercise log

| Date | How induced | Outcome |
|---|---|---|
| 2026-11-12 | Worker killed mid-scan until scans exhausted their attempts | **Fired correctly, after finding two defects.** Details below. |

### 2026-11-12 — first drill

**How the failures were induced, and what that revealed.** Pointing the provider at errors
does not work: a failing probe produces an `errored` finding and the scan still *completes*.
That is deliberate — one bad probe must not fail a scan — but it means the only route to a
genuinely failed scan is attempt exhaustion. The worker was therefore killed mid-scan
repeatedly, with `SCAN_MAX_ATTEMPTS=1`, so each abandoned scan exhausted on the next startup.

Worth recording for whoever runs this next: **a scan failing is rare by design**, so a
sustained failure rate is hard to produce on a healthy system. That is a property, not an
obstacle.

**Defect 1 — the rule could never fire.** A real 71% failure rate produced nothing. The rule
measured the rate over a ten-minute window *and* required the breach to hold for ten minutes.
Those are not the same thing: a ten-minute average is already smoothed, so demanding it also
persist for ten minutes means twenty minutes of sustained failure — and the failing scans age
out of the measurement window while the sustain window is still counting. The condition
expires before it can be satisfied.

**Defect 2 — the fix looked like it worked.** After separating the two on the rule, the call
site still passed the sustain. The alert fired, which reads as success, but the payload said
`window_seconds: 60`: one failed scan in an otherwise quiet minute was being reported as a
100% failure rate. That is worse than unfireable, because an alert that twitchy gets muted.

**After both fixes:**

```
/oncall  signed=true   scan-failure-rate-high  value=0.8  failed=4/5  window=600s
/human   signed=false  scan-failure-rate-high  value=0.8  failed=4/5  window=600s
```

| Signal | Confirmed |
|---|---|
| Fires on a genuine sustained breach | yes, 80% over ten minutes |
| Measures over the declared ten-minute window | yes, asserted in the payload |
| Does not fire on a healthy 10% rate | yes, covered by a test |
| Delivered to the on-call endpoint, signed | yes |
| Delivered to a human channel | yes |
| Payload carries no scan content | yes |

Neither defect would have been found by the unit tests. They fed the evaluator synthetic
values at times of their choosing, so nothing ever aged out underneath it and nothing ever
checked which window was actually used. Both are now pinned by integration tests that were
verified to fail against the old behaviour.

### 2026-11-12 — pacing, measured

The limits this is built against, read from the provider rather than guessed: **8,000 tokens
per minute per model**, 1,000 requests per day, and 200,000 tokens per day. A probe costs two
calls against two different models — about 140 tokens to the target, about 410 to the judge,
the judge being the expensive one because it reasons about the exchange. At 8,000 per minute
the judge allows roughly 19 probes a minute, and that is the binding constraint.

| Run | Probes | Errored | |
|---|---|---|---|
| Hardened prompt, before (#32) | 32 | 5 | 16% |
| Naive prompt, before (#32) | 32 | 11 | 34% |
| Full corpus, after | **42** | **0** | **0%** |

The scan took 3.9 minutes and produced a real verdict (grade D). Most of that time was spent
waiting: `provider.pacing model=openai/gpt-oss-20b remaining_tokens=375 waiting_seconds=57` is
the mechanism choosing a minute of waiting over a handful of probes that would have come back
undetermined. **Scans are slower now, deliberately.**

One honest caveat about this measurement. It used `openai/gpt-oss-20b` as the judge, because
the production judge's daily allowance had already been spent by other work the same day. The
pacing reads the allowance the provider reports and knows nothing about which model it is
pacing, so the mechanism is the same — but the number above was not produced with the
production judge, and that is worth knowing before quoting it.

The spent-allowance path was exercised for real on the way there: a full-corpus scan against
the exhausted judge stopped after 4 probes and reported one scan-level condition. Under the
old behaviour the remaining 38 would each have retried three times and arrived as 38 identical
errored findings — 90% of the report, none of it about the target.

That run also exposed something the issue had not asked about: the partial scan reported
**grade A**. The four probes that ran had all resisted, so the arithmetic was right and the
headline was indefensible — "this target resisted everything", over a scan that never attempted
nine-tenths of the corpus. The caveat was recorded on the scan, but nobody reads a caveat
before a letter. A scan that does not cover the corpus now carries no grade.

### 2026-11-12 — making scans slower broke the lease

Pacing was measured and working before anyone noticed what it implied for the queue.

`scans.locked_at` is written once, at claim time, and never renewed. A scan that runs longer
than `SCAN_LEASE_SECONDS` is therefore indistinguishable from one whose worker died, so another
worker claims it and runs it again. Duplicate findings, and duplicate spend against the exact
provider allowance pacing exists to protect — the change would have traded a third of a report
being undetermined for the whole report being run twice.

It had always been possible and had never mattered, because scans took seconds. The first
paced full-corpus run used **234 seconds of a 300-second lease**, and that was with a judge
model cheaper than production's. The next slightly larger corpus, or one slightly unluckier
window, goes over.

A heartbeat now renews the claim at a third of the lease while the scan runs, on its own
session — a renewal queued behind the work it protects protects nothing. It is guarded on
`locked_by` and `status`, so a worker that has already lost its lease cannot take the scan back
from whoever holds it now, and a finished scan cannot be dragged back to `running`.

If scans appear to run twice, check for `lease.lost` in the worker log: it means a heartbeat
found the claim gone, which points at a renewal interval too long for the lease or a worker
that was paused.
