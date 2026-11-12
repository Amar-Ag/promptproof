"""Holding a claim for as long as the work takes.

`locked_at` is written once, at claim time, and a scan that outlives its lease
is indistinguishable from one whose worker died — so another worker claims it
and runs it again. Duplicate findings, and duplicate spend against the provider
allowance that pacing exists to protect.

That was survivable while scans took seconds. It stopped being survivable when
scans began waiting out rate-limit windows on purpose: the first measured
full-corpus run used 234 seconds of a 300-second lease, with a cheaper judge
than production uses. Making scans slower without renewing the lease would have
traded one failure for a worse one.
"""

from datetime import UTC, datetime, timedelta

from promptproof_core.models import Scan, Target, TargetFamily
from promptproof_core.queue import claim_next_scan, renew_lease
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

LEASE = 300
MAX_ATTEMPTS = 3


async def make_scan(session: AsyncSession) -> Scan:
    family = TargetFamily(name="Lease renewal target")
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
    await session.flush()
    scan = Scan(
        target_id=target.id, suites=["tool-abuse"], status="pending", probe_count=1
    )
    session.add(scan)
    await session.commit()
    return scan


async def age_the_lease(session: AsyncSession, scan_id: object, seconds: int) -> None:
    await session.execute(
        text("UPDATE scans SET locked_at = :t WHERE id = :i").bindparams(
            t=datetime.now(UTC) - timedelta(seconds=seconds), i=scan_id
        )
    )
    await session.commit()


class TestRenewal:
    async def test_a_renewed_scan_is_not_stolen(self, session: AsyncSession) -> None:
        """The whole point: a slow scan keeps its claim."""
        scan = await make_scan(session)
        claimed = await claim_next_scan(
            session, worker_id="worker-a", lease_seconds=LEASE, max_attempts=MAX_ATTEMPTS
        )
        assert claimed is not None

        # It has been running longer than its lease.
        await age_the_lease(session, scan.id, LEASE + 60)
        assert await renew_lease(session, scan_id=scan.id, worker_id="worker-a")

        stolen = await claim_next_scan(
            session, worker_id="worker-b", lease_seconds=LEASE, max_attempts=MAX_ATTEMPTS
        )
        assert stolen is None, (
            "a second worker claimed a scan whose lease had just been renewed; "
            "it would run again and spend the allowance twice"
        )

    async def test_without_renewal_it_is_stolen(self, session: AsyncSession) -> None:
        """Pins the reason renewal exists, so removing it fails here."""
        scan = await make_scan(session)
        await claim_next_scan(
            session, worker_id="worker-a", lease_seconds=LEASE, max_attempts=MAX_ATTEMPTS
        )
        await age_the_lease(session, scan.id, LEASE + 60)

        stolen = await claim_next_scan(
            session, worker_id="worker-b", lease_seconds=LEASE, max_attempts=MAX_ATTEMPTS
        )
        assert stolen is not None and stolen.id == scan.id


class TestItCannotBeAbused:
    async def test_a_worker_cannot_renew_a_scan_it_no_longer_holds(
        self, session: AsyncSession
    ) -> None:
        """Otherwise a worker that lost its lease could take the scan back from
        whoever is now running it, and both would carry on."""
        scan = await make_scan(session)
        await claim_next_scan(
            session, worker_id="worker-a", lease_seconds=LEASE, max_attempts=MAX_ATTEMPTS
        )
        await age_the_lease(session, scan.id, LEASE + 60)
        taken = await claim_next_scan(
            session, worker_id="worker-b", lease_seconds=LEASE, max_attempts=MAX_ATTEMPTS
        )
        assert taken is not None

        assert not await renew_lease(session, scan_id=scan.id, worker_id="worker-a")

    async def test_it_cannot_drag_a_finished_scan_back_to_running(
        self, session: AsyncSession
    ) -> None:
        """A heartbeat racing the end of its own scan must not resurrect it."""
        scan = await make_scan(session)
        await claim_next_scan(
            session, worker_id="worker-a", lease_seconds=LEASE, max_attempts=MAX_ATTEMPTS
        )
        await session.execute(
            text(
                "UPDATE scans SET status = 'completed', finished_at = now() WHERE id = :i"
            ).bindparams(i=scan.id)
        )
        await session.commit()

        assert not await renew_lease(session, scan_id=scan.id, worker_id="worker-a")

        refreshed = await session.get(Scan, scan.id, populate_existing=True)
        assert refreshed is not None
        assert refreshed.status == "completed"

    async def test_it_cannot_revive_a_failed_scan(self, session: AsyncSession) -> None:
        scan = await make_scan(session)
        await claim_next_scan(
            session, worker_id="worker-a", lease_seconds=LEASE, max_attempts=MAX_ATTEMPTS
        )
        await session.execute(
            text(
                "UPDATE scans SET status = 'failed', finished_at = now(), "
                "error = 'gone' WHERE id = :i"
            ).bindparams(i=scan.id)
        )
        await session.commit()

        assert not await renew_lease(session, scan_id=scan.id, worker_id="worker-a")

    async def test_renewing_twice_is_harmless(self, session: AsyncSession) -> None:
        scan = await make_scan(session)
        await claim_next_scan(
            session, worker_id="worker-a", lease_seconds=LEASE, max_attempts=MAX_ATTEMPTS
        )
        assert await renew_lease(session, scan_id=scan.id, worker_id="worker-a")
        assert await renew_lease(session, scan_id=scan.id, worker_id="worker-a")

    async def test_it_does_not_touch_the_attempt_count(
        self, session: AsyncSession
    ) -> None:
        """A renewal is not a claim. Counting it as an attempt would retire a
        long scan through the attempt limit while it was running correctly."""
        scan = await make_scan(session)
        claimed = await claim_next_scan(
            session, worker_id="worker-a", lease_seconds=LEASE, max_attempts=MAX_ATTEMPTS
        )
        assert claimed is not None
        before = claimed.attempts

        for _ in range(5):
            assert await renew_lease(session, scan_id=scan.id, worker_id="worker-a")

        refreshed = await session.get(Scan, scan.id, populate_existing=True)
        assert refreshed is not None
        assert refreshed.attempts == before
