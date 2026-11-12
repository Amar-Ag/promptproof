"""What the on-call agent is allowed to do, and nothing else.

The agent's authority is defined by this module, not by its prompt. A prompt
saying "only reclaim expired leases" is a request; a toolset containing only
reclaim-expired-leases is a boundary. If the model decides at 3am that the
right fix is to drop a table, it has no way to express that.

Every function here is **idempotent**. Duplicate alerts happen, retries happen,
and an agent that double-applies a remediation is worse than one that does
nothing. Each predicate excludes rows already in the target state, so running
twice changes nothing the second time.

Each corresponds to a step marked `AGENT` in a runbook. Nothing here deploys,
migrates, deletes data, or changes configuration — those are `HUMAN` steps, and
the agent's only move when it needs one is to escalate.
"""

from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..logging import get_logger

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class Finding:
    """The result of one inspection or action, for the agent and the issue."""

    summary: str
    detail: dict[str, Any]


# ── Inspection (read-only) ───────────────────────────────────────────────────

_QUEUE_STATE_SQL = """
SELECT count(*) FILTER (WHERE status = 'pending')                          AS pending,
       count(*) FILTER (WHERE status = 'running')                          AS running,
       coalesce(max(extract(epoch FROM now() - queued_at))
                FILTER (WHERE status = 'pending'), 0)::int                 AS oldest_pending_s,
       count(*) FILTER (WHERE status = 'running'
                          AND locked_at < now() - (:lease * interval '1 second'))
                                                                           AS stale_leases
  FROM scans
 WHERE status IN ('pending', 'running')
"""


async def inspect_queue(session: AsyncSession, lease_seconds: int) -> Finding:
    """How deep the queue is, how old, and how many leases have expired."""
    row = (
        (await session.execute(text(_QUEUE_STATE_SQL).bindparams(lease=lease_seconds)))
        .mappings()
        .one()
    )
    detail = {k: int(v) for k, v in row.items()}
    return Finding(
        summary=(
            f"{detail['pending']} pending, {detail['running']} running, "
            f"oldest pending {detail['oldest_pending_s']}s, "
            f"{detail['stale_leases']} with an expired lease"
        ),
        detail=detail,
    )


_RECENT_FAILURES_SQL = """
SELECT coalesce(error, 'none') AS reason, count(*) AS count
  FROM scans
 WHERE status = 'failed'
   AND finished_at > now() - (:window * interval '1 second')
 GROUP BY 1
 ORDER BY 2 DESC
 LIMIT 10
"""


async def inspect_recent_failures(session: AsyncSession, window_seconds: int = 1800) -> Finding:
    """Why scans have been failing, grouped by reason."""
    rows = (
        (await session.execute(text(_RECENT_FAILURES_SQL).bindparams(window=window_seconds)))
        .mappings()
        .all()
    )
    by_reason = {str(r["reason"])[:120]: int(r["count"]) for r in rows}
    total = sum(by_reason.values())
    return Finding(
        summary=(
            f"{total} failed scans in the last {window_seconds // 60} minutes"
            if total
            else "no failed scans in the window"
        ),
        detail={"by_reason": by_reason, "window_seconds": window_seconds},
    )


_WORKER_ACTIVITY_SQL = """
SELECT count(DISTINCT locked_by) FILTER (WHERE locked_at > now() - interval '5 minutes')
           AS workers_seen,
       max(extract(epoch FROM now() - locked_at))::int AS since_last_claim_s
  FROM scans
 WHERE locked_at IS NOT NULL
"""


async def inspect_worker_activity(session: AsyncSession) -> Finding:
    """Whether any worker has claimed anything recently.

    Distinguishes the two cases that look identical from the queue alone: a
    worker that is absent, and a worker that is present but not progressing.
    """
    row = (await session.execute(text(_WORKER_ACTIVITY_SQL))).mappings().one()
    workers = int(row["workers_seen"] or 0)
    since = row["since_last_claim_s"]
    return Finding(
        summary=(
            f"{workers} worker(s) claimed work in the last 5 minutes"
            if workers
            else "no worker has claimed anything in the last 5 minutes"
        ),
        detail={
            "workers_seen": workers,
            "seconds_since_last_claim": int(since) if since is not None else None,
        },
    )


# ── Remediation (bounded, idempotent) ────────────────────────────────────────

# Runbook stuck-queue.md, D2. Only touches rows whose lease has genuinely
# expired and which have attempts left; anything already reset or exhausted is
# excluded, so a second run is a no-op.
_RECLAIM_SQL = """
UPDATE scans
   SET status = 'pending', locked_at = NULL, locked_by = NULL
 WHERE status = 'running'
   AND locked_at < now() - (:lease * interval '1 second')
   AND attempts < :max_attempts
RETURNING id
"""


async def reclaim_expired_leases(
    session: AsyncSession, lease_seconds: int, max_attempts: int
) -> Finding:
    """Return abandoned scans to the queue so a live worker can pick them up."""
    rows = (
        await session.execute(
            text(_RECLAIM_SQL).bindparams(lease=lease_seconds, max_attempts=max_attempts)
        )
    ).fetchall()
    await session.commit()
    ids = [str(r[0]) for r in rows]
    log.info("oncall.reclaimed", count=len(ids))
    return Finding(
        summary=f"reclaimed {len(ids)} scan(s) with an expired lease",
        detail={"count": len(ids), "scan_ids": ids[:20]},
    )


# Runbook stuck-queue.md, D2 continued. A scan out of attempts is failed so the
# user sees an honest error instead of waiting forever.
_FAIL_EXHAUSTED_SQL = """
UPDATE scans
   SET status = 'failed', finished_at = now(), locked_at = NULL, locked_by = NULL,
       error = 'Scan abandoned after repeated worker failures'
 WHERE status = 'running'
   AND locked_at < now() - (:lease * interval '1 second')
   AND attempts >= :max_attempts
RETURNING id
"""


async def fail_exhausted_scans(
    session: AsyncSession, lease_seconds: int, max_attempts: int
) -> Finding:
    """Tell the user the truth about scans that will never finish."""
    rows = (
        await session.execute(
            text(_FAIL_EXHAUSTED_SQL).bindparams(lease=lease_seconds, max_attempts=max_attempts)
        )
    ).fetchall()
    await session.commit()
    ids = [str(r[0]) for r in rows]
    log.info("oncall.failed_exhausted", count=len(ids))
    return Finding(
        summary=f"marked {len(ids)} exhausted scan(s) as failed",
        detail={"count": len(ids), "scan_ids": ids[:20]},
    )


# Runbook scan-failures.md, D1/D2. Only scans that failed on a transient reason
# and still have attempts left; a permanently broken scan is not retried.
_REQUEUE_TRANSIENT_SQL = """
UPDATE scans
   SET status = 'pending', locked_at = NULL, locked_by = NULL, error = NULL,
       finished_at = NULL
 WHERE status = 'failed'
   AND finished_at > now() - (:window * interval '1 second')
   AND attempts < :max_attempts
   AND (error ILIKE '%rate limit%' OR error ILIKE '%429%' OR error ILIKE '%timeout%')
RETURNING id
"""


async def requeue_transient_failures(
    session: AsyncSession, max_attempts: int, window_seconds: int = 1800
) -> Finding:
    """Retry scans that failed for a reason that may since have cleared."""
    rows = (
        await session.execute(
            text(_REQUEUE_TRANSIENT_SQL).bindparams(
                window=window_seconds, max_attempts=max_attempts
            )
        )
    ).fetchall()
    await session.commit()
    ids = [str(r[0]) for r in rows]
    log.info("oncall.requeued", count=len(ids))
    return Finding(
        summary=f"requeued {len(ids)} scan(s) that failed on a transient error",
        detail={"count": len(ids), "scan_ids": ids[:20]},
    )


# What the agent may do, by name. Anything absent from this mapping does not
# exist as far as the agent is concerned — including everything destructive.
READ_ONLY_ACTIONS = frozenset(
    {
        "inspect_queue",
        "inspect_recent_failures",
        "inspect_worker_activity",
    }
)

# Actions that change rows. Kept separate because when a run is cut off partway
# through — a provider rate limit, a timeout — the difference between "it only
# looked" and "it had already changed something" is the whole content of the
# report a human reads next.
WRITE_ACTIONS = frozenset(
    {
        "reclaim_expired_leases",
        "fail_exhausted_scans",
        "requeue_transient_failures",
    }
)

AUTHORISED_ACTIONS = READ_ONLY_ACTIONS | WRITE_ACTIONS
