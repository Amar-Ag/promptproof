# Runbook — `scan-queue-stalled`

> **Status:** exercised. See the drill log at the foot of this page.

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
| 2026-11-12 | Worker killed mid-scan; on-call responder woken by the alert | **Responder worked the runbook correctly, after finding four defects.** Details below. |

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

### 2026-11-12 — second drill, the on-call responder

The first drill proved the alert fires. This one put the responder behind it: a real scan was
queued, the worker killed mid-scan, and the lease left to expire. Nothing was simulated.

**The cycle, once it worked.**

```
18:08:39  alert.delivered    target=human     value=607.8  waiting=1
18:08:43  oncall.responded   branch='stale lease'  tools=[inspect_worker_activity,
                             inspect_queue, reclaim_expired_leases, fail_exhausted_scans]
18:08:43  alert.delivered    target=oncall
```

The responder inspected before it acted, marked the abandoned scan failed so the user sees an
honest error instead of waiting forever, and **escalated** — because no worker was running and
starting a process is outside its authority. Its own words:

> No worker has claimed work in 883s. A human needs to start/restart the worker process
> (deploy/restart is outside the authorised tool set).

That is the behaviour the design is for: fix what it is allowed to fix, say plainly what it
cannot, and leave a record either way.

**Defect 1 — the signature could never verify.** The evaluator signed a canonical JSON
rendering of the payload and then handed the payload to the HTTP client, which serialised it
again its own way. The bytes signed were never the bytes sent, so every genuine alert would
have been rejected as forged and the responder would never have run at all.

The first drill recorded `signed=true` and read as a pass. It was only reporting that a
signature *header was present*. Nobody had checked that it verified, and delivery was not
covered by a single test. Both halves now go through one `encode()`, and the test takes the
bytes that leave the client and runs the receiver's own `verify` against them.

**Defect 2 — the responder reasoned with the wrong configuration.** It runs inside the API,
and the API had never been given `SCAN_LEASE_SECONDS` or `SCAN_MAX_ATTEMPTS`. So it judged
leases against the 300-second default while the worker expired them at 30, and it reclaimed a
scan the worker considered out of attempts. The scan went back to `pending`, where nothing
would ever pick it up again, and the responder reported success.

A silently wrong remediation that reports success is the worst outcome available to an on-call
system. The settings are now wired to both services and a test fails if they ever diverge.

**Defect 3 — an interrupted run claimed it had changed nothing.** Groq cut a run off partway
through, *after* it had already marked a scan failed. The fallback report said "Nothing was
remediated." That was false, in precisely the situation where the person reading the issue most
needs the truth. A partial run now reports which writes had already landed, separates that from
a run that only looked, and says a retry is safe because every action is idempotent.

**Defect 4 — parallel tool calls corrupted the session.** A model is free to ask for two tools
in one turn, and a single `AsyncSession` cannot serve concurrent operations. With a model that
parallelises, the run raised `concurrent operations are not permitted` after remediations had
already been applied — failing *and* leaving the queue in a state nobody had signed off on.
The first model tried never parallelised, so nothing revealed this. Tool bodies now serialise
on a per-run lock.

**On quotas.** The drills kept dying on `tokens per day` until the cause was read properly: the
free tier allows 200,000 tokens per model per day across the whole service, and a day of
scanning and judging had spent the judge model's allowance. The responder now sends only the
runbook for the alert that fired rather than both, and allows one retry rather than two.

This does not solve it. On a free tier there is no model this service does not already use, so
the responder shares a daily quota with the workload it responds to — a day busy enough to
cause an incident is a day that can starve the responder. Worth knowing before trusting it.

`openai/gpt-oss-20b` was tried as a model nothing else touches, and rejected: Groq returns
`output_parse_failed` when it has to produce a typed report after tool calls.

| Signal | Confirmed |
|---|---|
| Alert reaches the responder with a signature that verifies | yes, after defect 1 |
| Responder inspects before it acts | yes, both inspections ran first |
| Remediation is correct against the worker's real configuration | yes, after defect 2 |
| Abandoned scan surfaced to the user as failed, not left hanging | yes |
| Refuses and escalates when the fix needs a human | yes, worker restart |
| A partial run reports what it had already changed | yes, after defect 3 |
| Parallel tool calls are safe | yes, after defect 4 |
| Issue filed on every run | yes — [#47](https://github.com/Amar-Ag/promptproof/issues/47) |
| Survives a caller that gives up mid-run | yes, added in review |
| Replayed alerts refused | yes, added in review |

Three of the four defects were invisible to the test suite and to reading the code. They
appeared only when the thing was run against a real worker that had really died.

### 2026-11-12 — what review found afterwards

The drill above was written up as a success. Review of the same change found four more
defects, two of them in the parts the write-up was proudest of.

**The diagnostic the agent branches on was reporting the wrong number.**
`inspect_worker_activity` computed `max(now() - locked_at)` — the age of the *oldest*
outstanding claim — and labelled it "time since the last claim", which is
`now() - max(locked_at)`. With one stale row beside one healthy claim it reports that no
worker has been seen for two and a half hours while a worker is in fact working. The agent
chooses between "a worker died, reclaim" and "no worker is running, escalate" on exactly this
number, so the result is a human woken for a healthy system.

The drill quote above — "No worker has claimed work in 883s" — came from the broken
expression. It was right only because there was a single row. The test that should have caught
it asserted `workers_seen` and never looked at the number at all.

**One of the three authorised remediations could never have matched a row.**
`requeue_transient_failures` filtered `scans.error` for `%rate limit%`, `%429%` and
`%timeout%`. `scans.error` is written in three places and only ever holds one of three
literals, none of them transient — a provider rate limit is a *probe-level* result that leaves
the scan `completed`, deliberately, because one bad probe must not fail a scan.

So during a real rate-limit episode the agent would have called it, been told "requeued 0", and
filed an issue reporting nothing transient to retry while every user-visible failure was
transient. Its three tests passed only because they seeded an error string no code path can
write. The tool is removed rather than left as theatre; [#45](https://github.com/Amar-Ag/promptproof/issues/45)
covers giving it something real to match.

**A caller that gave up took the report with it.** The run had no deadline of its own while the
deliverer gave up at 90 seconds and disconnected, cancelling the handler. `CancelledError` is a
`BaseException`, so it passed through every handler on the path: no report, no issue — after
remediations had already committed. The alert loop would log a delivery failure, so the written
history would say the responder was unreachable at the moment it was changing production rows.
The run now owns its session, runs shielded, and carries its report out through the
cancellation.

**A valid signature was valid forever.** Nothing bound a signed body to a moment, so a captured
alert could be replayed indefinitely — each replay a full agent run spending the daily
per-model quota that [ADR-0006](../adr/0006-bounded-oncall-agent.md) names as this responder's
scarce resource. Enough of them overnight and the responder is starved when a real alert fires:
the failure that ADR says to watch for, reachable on purpose. `fired_at` is already inside the
signed bytes, so alerts older than ten minutes are now refused, and runs are single-flight.

**Known limitation, not a defect.** `inspect_worker_activity` cannot tell an *idle* worker from
an absent one: both have claimed nothing recently. It does not mislead in practice, because the
alert only fires when scans are waiting, and a worker not claiming while work waits is the
thing worth reporting. But a synthetic alert sent at an empty queue will produce a confident
"no worker" from a perfectly healthy system, as one did while testing the fixes above.

**The pattern, again.** Two of these four were in code the first write-up presented as
verified, and two of the tests backing that write-up asserted less than their names promised —
one built its own lock rather than checking the agent took one, another grepped the source for
a string. The endpoint that authenticates the whole path had no test at all. Running the thing
found four defects; having someone else read it found four more.

### 2026-11-12 — the first issue it filed was wrong

With a token in place the responder filed [#46](https://github.com/Amar-Ag/promptproof/issues/46).
The mechanism worked. The content did not:

```
## What I changed
**Nothing.** The condition did not call for any remediation I am authorised to apply.
```

In that same run `fail_exhausted_scans` had marked an abandoned scan `failed`. The database
changed and the issue denied it. `inspected` was empty too, though four tools had run, and the
diagnosis was the single character `1`.

Nothing was broken in the sense of raising. The report was assembled from `report.actions` —
the model's own account of its run — and the model had simply left it empty. **An on-call
record built from a model's narration is a story, not an audit trail**, and it is worth noticing
that this failed in the safe-looking direction: the issue under-claimed. A model that
over-claims produces the same class of document and is read the same way.

The record is now built from what the tools returned: which ran, and how many rows each
changed. The model's account is still printed, under its own heading, so a disagreement between
the two is visible rather than quietly resolved in the model's favour.

[#47](https://github.com/Amar-Ag/promptproof/issues/47) is the same drill after the fix —
inspections listed from tool results, one row changed, both accounts agreeing.

This is the third time in this feature that a report claimed nothing had happened when
something had: once in the interrupted-run fallback, once in the router's fallback, once here.
The lesson is not about any of those three code paths. It is that every path which writes the
record has to take its facts from the tools.
