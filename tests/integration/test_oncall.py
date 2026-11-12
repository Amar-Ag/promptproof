"""What the on-call responder is allowed to do.

Two properties carry the weight here.

**Idempotency.** Duplicate alerts happen and retries happen. An agent that
double-applies a remediation is worse than one that does nothing, so every
action is run twice and the second run must be a no-op.

**Bounded authority.** The agent's limits come from this module, not from its
prompt. A prompt saying "do not delete anything" is a request; a toolset with
no delete in it is a boundary. So the authorised set is asserted directly.
"""

from datetime import UTC, datetime, timedelta

from promptproof_core.models import Scan, Target, TargetFamily
from promptproof_core.oncall.remediations import (
    AUTHORISED_ACTIONS,
    fail_exhausted_scans,
    inspect_queue,
    inspect_recent_failures,
    inspect_worker_activity,
    reclaim_expired_leases,
)
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

LEASE = 300
MAX_ATTEMPTS = 3


async def make_target(session: AsyncSession) -> Target:
    family = TargetFamily(name="On-call target")
    session.add(family)
    await session.flush()
    target = Target(
        family_id=family.id,
        version=1,
        kind="prompt",
        model="qwen/qwen3.8-27b",
        system_prompt="You only discuss cheese.",
    )
    session.add(target)
    await session.commit()
    return target


async def add_scan(
    session: AsyncSession,
    target: Target,
    *,
    status: str,
    attempts: int = 0,
    locked_ago: int | None = None,
    finished_ago: int | None = None,
    error: str | None = None,
) -> Scan:
    scan = Scan(
        target_id=target.id,
        suites=["tool-abuse"],
        status=status,
        probe_count=1,
        attempts=attempts,
        error=error,
    )
    session.add(scan)
    await session.flush()
    now = datetime.now(UTC)
    if locked_ago is not None:
        await session.execute(
            text(
                "UPDATE scans SET locked_at = :t, locked_by = 'dead-worker' WHERE id = :i"
            ).bindparams(t=now - timedelta(seconds=locked_ago), i=scan.id)
        )
    if finished_ago is not None:
        await session.execute(
            text("UPDATE scans SET finished_at = :t WHERE id = :i").bindparams(
                t=now - timedelta(seconds=finished_ago), i=scan.id
            )
        )
    await session.commit()
    return scan


class TestAuthority:
    def test_the_authorised_set_contains_nothing_destructive(self) -> None:
        """The boundary is the toolset, so the toolset is asserted."""
        forbidden = ("delete", "drop", "truncate", "deploy", "migrate", "rollback", "restart")
        for action in AUTHORISED_ACTIONS:
            assert not any(word in action for word in forbidden), (
                f"{action} looks like something the responder must not be able to do"
            )

    def test_it_can_inspect_and_remediate_but_nothing_else(self) -> None:
        assert AUTHORISED_ACTIONS == {
            "inspect_queue",
            "inspect_recent_failures",
            "inspect_worker_activity",
            "reclaim_expired_leases",
            "fail_exhausted_scans",
        }

    def test_nothing_claims_to_retry_a_transient_failure(self) -> None:
        """There was such an action, and it could never have matched a row.

        `scans.error` only ever holds one of three literals, none of them
        transient: a rate limit or timeout is a probe-level result that leaves
        the scan completed. The tool filtered scan rows for '%rate limit%' and
        would have reported "requeued 0" forever while reading, to anyone
        looking at the authorised set, as though retrying were possible. Its
        tests passed only by seeding an error string no code path can write.

        Removed rather than left as theatre; see #45. Asserted so it cannot
        come back without the data to make it real.
        """
        assert not any("requeue" in action for action in AUTHORISED_ACTIONS)


class TestInspection:
    async def test_queue_inspection_counts_stale_leases(self, session: AsyncSession) -> None:
        target = await make_target(session)
        await add_scan(session, target, status="pending")
        await add_scan(session, target, status="running", attempts=1, locked_ago=LEASE + 60)
        await add_scan(session, target, status="running", attempts=1, locked_ago=5)

        finding = await inspect_queue(session, LEASE)
        assert finding.detail["pending"] == 1
        assert finding.detail["running"] == 2
        assert finding.detail["stale_leases"] == 1

    async def test_worker_activity_distinguishes_absent_from_stuck(
        self, session: AsyncSession
    ) -> None:
        """The queue alone cannot tell these apart, and the fix differs."""
        target = await make_target(session)
        await add_scan(session, target, status="running", attempts=1, locked_ago=9_000)

        finding = await inspect_worker_activity(session)
        assert finding.detail["workers_seen"] == 0
        assert "no worker" in finding.summary

        await add_scan(session, target, status="running", attempts=1, locked_ago=10)
        finding = await inspect_worker_activity(session)
        assert finding.detail["workers_seen"] == 1

    async def test_it_reports_the_most_recent_claim_not_the_oldest(
        self, session: AsyncSession
    ) -> None:
        """`max(now - locked_at)` is the age of the OLDEST outstanding claim;
        the time since the most recent one is `now - max(locked_at)`. They
        differ by the whole width of the queue.

        It matters because the agent chooses between "a worker died mid-scan,
        reclaim" and "no worker is running, escalate" on this number. With one
        stale row beside one healthy claim, the wrong aggregate says no worker
        has been seen for two and a half hours while a worker is in fact
        working — so the responder escalates a healthy system and a human is
        woken for nothing.

        The earlier version asserted only `workers_seen`, so it passed against
        the wrong expression and its name promised more than it checked.
        """
        target = await make_target(session)
        await add_scan(session, target, status="running", attempts=1, locked_ago=9_000)
        await add_scan(session, target, status="running", attempts=1, locked_ago=10)

        finding = await inspect_worker_activity(session)
        since = finding.detail["seconds_since_last_claim"]
        assert since is not None
        assert since < 60, (
            f"reported {since}s since the last claim, but a worker claimed work "
            "10 seconds ago - this is the oldest claim, not the most recent"
        )

    async def test_failures_are_grouped_by_reason(self, session: AsyncSession) -> None:
        target = await make_target(session)
        for _ in range(3):
            await add_scan(
                session, target, status="failed", finished_ago=60, error="HTTP 429 rate limited"
            )
        await add_scan(session, target, status="failed", finished_ago=60, error="Target gone")

        finding = await inspect_recent_failures(session)
        assert finding.detail["by_reason"]["HTTP 429 rate limited"] == 3
        assert finding.detail["by_reason"]["Target gone"] == 1


class TestReclaim:
    async def test_it_returns_abandoned_scans_to_the_queue(self, session: AsyncSession) -> None:
        target = await make_target(session)
        stale = await add_scan(session, target, status="running", attempts=1, locked_ago=LEASE + 60)

        finding = await reclaim_expired_leases(session, LEASE, MAX_ATTEMPTS)
        assert finding.detail["count"] == 1

        refreshed = await session.get(Scan, stale.id, populate_existing=True)
        assert refreshed is not None
        assert refreshed.status == "pending"
        assert refreshed.locked_by is None

    async def test_it_leaves_a_live_lease_alone(self, session: AsyncSession) -> None:
        """Reclaiming a scan a worker is actively running would duplicate work."""
        target = await make_target(session)
        await add_scan(session, target, status="running", attempts=1, locked_ago=5)

        finding = await reclaim_expired_leases(session, LEASE, MAX_ATTEMPTS)
        assert finding.detail["count"] == 0

    async def test_it_leaves_exhausted_scans_for_the_other_action(
        self, session: AsyncSession
    ) -> None:
        target = await make_target(session)
        await add_scan(
            session, target, status="running", attempts=MAX_ATTEMPTS, locked_ago=LEASE + 60
        )
        finding = await reclaim_expired_leases(session, LEASE, MAX_ATTEMPTS)
        assert finding.detail["count"] == 0

    async def test_running_it_twice_changes_nothing_the_second_time(
        self, session: AsyncSession
    ) -> None:
        target = await make_target(session)
        await add_scan(session, target, status="running", attempts=1, locked_ago=LEASE + 60)

        first = await reclaim_expired_leases(session, LEASE, MAX_ATTEMPTS)
        second = await reclaim_expired_leases(session, LEASE, MAX_ATTEMPTS)
        assert first.detail["count"] == 1
        assert second.detail["count"] == 0


class TestFailExhausted:
    async def test_it_fails_scans_that_are_out_of_attempts(self, session: AsyncSession) -> None:
        target = await make_target(session)
        scan = await add_scan(
            session, target, status="running", attempts=MAX_ATTEMPTS, locked_ago=LEASE + 60
        )

        finding = await fail_exhausted_scans(session, LEASE, MAX_ATTEMPTS)
        assert finding.detail["count"] == 1

        refreshed = await session.get(Scan, scan.id, populate_existing=True)
        assert refreshed is not None
        assert refreshed.status == "failed"
        assert refreshed.finished_at is not None
        assert "abandoned" in (refreshed.error or "")

    async def test_running_it_twice_changes_nothing_the_second_time(
        self, session: AsyncSession
    ) -> None:
        target = await make_target(session)
        await add_scan(
            session, target, status="running", attempts=MAX_ATTEMPTS, locked_ago=LEASE + 60
        )
        first = await fail_exhausted_scans(session, LEASE, MAX_ATTEMPTS)
        second = await fail_exhausted_scans(session, LEASE, MAX_ATTEMPTS)
        assert first.detail["count"] == 1
        assert second.detail["count"] == 0
