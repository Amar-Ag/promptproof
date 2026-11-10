"""The scan queue.

There is no broker: the `scans` table *is* the queue (ADR-0002). Everything in
this module exists to make that safe under concurrency and recoverable after a
worker dies.
"""

import uuid
from dataclasses import dataclass

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from .models import Scan

# One statement, one round trip. The inner SELECT picks the oldest eligible scan
# and locks it; SKIP LOCKED makes a concurrent worker move past a row that is
# already being claimed rather than blocking on it, so two workers can never
# claim the same scan and neither waits.
#
# Two kinds of row are eligible:
#   1. `pending`  — never started.
#   2. `running` with an expired lease — the worker holding it died. This is the
#      recovery path the on-call agent is built around (docs/runbooks/stuck-queue.md).
#
# `attempts` is incremented on claim, not on failure, so a scan that crashes the
# worker repeatedly still converges on the attempt limit instead of looping forever.
_CLAIM_SQL = """
UPDATE scans
   SET status      = 'running',
       locked_at   = now(),
       locked_by   = :worker_id,
       attempts    = attempts + 1,
       started_at  = coalesce(started_at, now())
 WHERE id = (
       SELECT id
         FROM scans
        WHERE (status = 'pending'
               OR (status = 'running'
                   AND locked_at < now() - (:lease_seconds * interval '1 second')))
          AND attempts < :max_attempts
        ORDER BY queued_at
          FOR UPDATE SKIP LOCKED
        LIMIT 1)
RETURNING *
"""

# A scan whose lease expired and which has already used all its attempts is not
# retried: it is failed, so the user sees an honest error instead of waiting
# forever. Idempotent — the predicate excludes rows already moved to a terminal
# state.
_FAIL_EXHAUSTED_SQL = """
UPDATE scans
   SET status      = 'failed',
       finished_at = now(),
       locked_at   = NULL,
       locked_by   = NULL,
       error       = :reason
 WHERE status = 'running'
   AND locked_at < now() - (:lease_seconds * interval '1 second')
   AND attempts >= :max_attempts
RETURNING id
"""

EXHAUSTED_REASON = "Scan abandoned after repeated worker failures"


async def claim_next_scan(
    session: AsyncSession,
    *,
    worker_id: str,
    lease_seconds: int,
    max_attempts: int,
) -> Scan | None:
    """Claim the oldest eligible scan, or return None if there is nothing to do."""
    stmt = text(_CLAIM_SQL).bindparams(
        worker_id=worker_id,
        lease_seconds=lease_seconds,
        max_attempts=max_attempts,
    )
    # `populate_existing` is required, not optional. Without it, a session that
    # has already loaded this scan returns its cached instance from the identity
    # map and silently discards the RETURNING row — so the caller would see the
    # pre-claim `status` and `attempts` and act on stale queue state.
    result = await session.execute(
        select(Scan)
        .from_statement(stmt.columns(*Scan.__table__.columns))
        .execution_options(populate_existing=True)
    )
    return result.scalars().one_or_none()


async def fail_exhausted_scans(
    session: AsyncSession,
    *,
    lease_seconds: int,
    max_attempts: int,
) -> list[uuid.UUID]:
    """Mark abandoned scans failed once they are out of attempts."""
    result = await session.execute(
        text(_FAIL_EXHAUSTED_SQL).bindparams(
            lease_seconds=lease_seconds,
            max_attempts=max_attempts,
            reason=EXHAUSTED_REASON,
        )
    )
    return [row[0] for row in result.fetchall()]


@dataclass(frozen=True, slots=True)
class QueueStats:
    """What the queue metrics and the on-call agent both read."""

    pending: int
    running: int
    oldest_age_seconds: float

    @property
    def depth(self) -> int:
        return self.pending + self.running


_STATS_SQL = """
SELECT
    count(*) FILTER (WHERE status = 'pending')                        AS pending,
    count(*) FILTER (WHERE status = 'running')                        AS running,
    coalesce(max(extract(epoch FROM now() - queued_at)), 0)           AS oldest_age_seconds
  FROM scans
 WHERE status IN ('pending', 'running')
"""


async def queue_stats(session: AsyncSession) -> QueueStats:
    """Queue depth and the age of the oldest unfinished scan.

    `oldest_age_seconds` is the primary alerting signal: it is the closest
    available proxy for how long a real person has been waiting.
    """
    row = (await session.execute(text(_STATS_SQL))).mappings().one()
    return QueueStats(
        pending=int(row["pending"]),
        running=int(row["running"]),
        oldest_age_seconds=float(row["oldest_age_seconds"]),
    )
