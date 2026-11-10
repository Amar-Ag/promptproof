"""The queue, against a real Postgres.

`FOR UPDATE SKIP LOCKED` is the whole reason this project needs no broker
(ADR-0002), and lease expiry is the failure the on-call agent is built to
remediate. Neither can be tested against a fake, so these run for real.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from promptproof_core.models import Scan, Target, TargetFamily
from promptproof_core.queue import (
    EXHAUSTED_REASON,
    claim_next_scan,
    fail_exhausted_scans,
    queue_stats,
)
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

LEASE = 300
MAX_ATTEMPTS = 3


async def make_target(session: AsyncSession, name: str = "t") -> Target:
    family = TargetFamily(name=name)
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


async def queue_scan(
    session: AsyncSession,
    target: Target,
    *,
    queued_at: datetime | None = None,
    status: str = "pending",
    attempts: int = 0,
    locked_at: datetime | None = None,
) -> Scan:
    scan = Scan(
        target_id=target.id,
        suites=["direct-injection"],
        status=status,
        probe_count=1,
        attempts=attempts,
        locked_at=locked_at,
    )
    session.add(scan)
    await session.flush()
    if queued_at is not None:
        await session.execute(
            text("UPDATE scans SET queued_at = :q WHERE id = :i").bindparams(q=queued_at, i=scan.id)
        )
    await session.commit()
    return scan


def claim(session: AsyncSession, worker: str = "w1"):  # type: ignore[no-untyped-def]
    return claim_next_scan(
        session, worker_id=worker, lease_seconds=LEASE, max_attempts=MAX_ATTEMPTS
    )


class TestClaiming:
    async def test_claims_a_pending_scan_and_marks_it_running(self, session: AsyncSession) -> None:
        target = await make_target(session)
        queued = await queue_scan(session, target)

        claimed = await claim(session)
        await session.commit()

        assert claimed is not None
        assert claimed.id == queued.id
        assert claimed.status == "running"
        assert claimed.locked_by == "w1"
        assert claimed.locked_at is not None
        assert claimed.started_at is not None
        # Incremented on claim, not on failure, so a scan that kills the worker
        # still converges on the attempt limit.
        assert claimed.attempts == 1

    async def test_returns_none_when_the_queue_is_empty(self, session: AsyncSession) -> None:
        assert await claim(session) is None

    async def test_claims_oldest_first(self, session: AsyncSession) -> None:
        target = await make_target(session)
        now = datetime.now(UTC)
        newer = await queue_scan(session, target, queued_at=now - timedelta(minutes=1))
        older = await queue_scan(session, target, queued_at=now - timedelta(minutes=5))

        first = await claim(session)
        await session.commit()
        assert first is not None and first.id == older.id

        second = await claim(session)
        await session.commit()
        assert second is not None and second.id == newer.id

    async def test_two_workers_cannot_claim_the_same_scan(
        self, sessions: tuple[AsyncSession, AsyncSession]
    ) -> None:
        """The property the whole no-broker design rests on.

        Session A claims and holds its transaction open. Session B must skip the
        locked row and come back empty rather than blocking on it or claiming it
        a second time.
        """
        a, b = sessions
        target = await make_target(a)
        await queue_scan(a, target)

        claimed_by_a = await claim(a, "worker-a")
        assert claimed_by_a is not None

        # A has not committed: the row is locked.
        claimed_by_b = await claim(b, "worker-b")
        assert claimed_by_b is None, "SKIP LOCKED let a second worker claim the same scan"

        await a.commit()

        # Now committed and `running` with a fresh lease, so still not claimable.
        assert await claim(b, "worker-b") is None

    async def test_a_second_worker_gets_the_next_scan(
        self, sessions: tuple[AsyncSession, AsyncSession]
    ) -> None:
        a, b = sessions
        target = await make_target(a)
        now = datetime.now(UTC)
        await queue_scan(a, target, queued_at=now - timedelta(minutes=5))
        await queue_scan(a, target, queued_at=now - timedelta(minutes=4))

        first = await claim(a, "worker-a")
        second = await claim(b, "worker-b")

        assert first is not None
        assert second is not None
        assert first.id != second.id


class TestLeaseExpiry:
    async def test_reclaims_a_scan_whose_lease_expired(self, session: AsyncSession) -> None:
        """A worker died mid-scan. Another one picks the work up."""
        target = await make_target(session)
        stale = await queue_scan(
            session,
            target,
            status="running",
            attempts=1,
            locked_at=datetime.now(UTC) - timedelta(seconds=LEASE + 60),
        )

        reclaimed = await claim(session, "worker-b")
        await session.commit()

        assert reclaimed is not None
        assert reclaimed.id == stale.id
        assert reclaimed.locked_by == "worker-b"
        assert reclaimed.attempts == 2

    async def test_does_not_reclaim_a_live_lease(self, session: AsyncSession) -> None:
        target = await make_target(session)
        await queue_scan(
            session,
            target,
            status="running",
            attempts=1,
            locked_at=datetime.now(UTC) - timedelta(seconds=5),
        )
        assert await claim(session, "worker-b") is None

    async def test_exhausted_scan_is_not_reclaimed(self, session: AsyncSession) -> None:
        target = await make_target(session)
        await queue_scan(
            session,
            target,
            status="running",
            attempts=MAX_ATTEMPTS,
            locked_at=datetime.now(UTC) - timedelta(seconds=LEASE + 60),
        )
        assert await claim(session, "worker-b") is None

    async def test_exhausted_scan_is_failed_with_an_honest_error(
        self, session: AsyncSession
    ) -> None:
        """The user sees a failure rather than waiting forever."""
        target = await make_target(session)
        scan = await queue_scan(
            session,
            target,
            status="running",
            attempts=MAX_ATTEMPTS,
            locked_at=datetime.now(UTC) - timedelta(seconds=LEASE + 60),
        )

        failed = await fail_exhausted_scans(session, lease_seconds=LEASE, max_attempts=MAX_ATTEMPTS)
        await session.commit()

        assert failed == [scan.id]
        refreshed = await session.get(Scan, scan.id, populate_existing=True)
        assert refreshed is not None
        assert refreshed.status == "failed"
        assert refreshed.error == EXHAUSTED_REASON
        assert refreshed.finished_at is not None
        assert refreshed.locked_by is None

    async def test_failing_exhausted_scans_is_idempotent(self, session: AsyncSession) -> None:
        """Duplicate alerts mean the on-call agent may run this twice."""
        target = await make_target(session)
        await queue_scan(
            session,
            target,
            status="running",
            attempts=MAX_ATTEMPTS,
            locked_at=datetime.now(UTC) - timedelta(seconds=LEASE + 60),
        )

        first = await fail_exhausted_scans(session, lease_seconds=LEASE, max_attempts=MAX_ATTEMPTS)
        await session.commit()
        second = await fail_exhausted_scans(session, lease_seconds=LEASE, max_attempts=MAX_ATTEMPTS)
        await session.commit()

        assert len(first) == 1
        assert second == []


class TestQueueStats:
    async def test_reports_depth_and_oldest_age(self, session: AsyncSession) -> None:
        """These two numbers are the primary alerting signal."""
        target = await make_target(session)
        now = datetime.now(UTC)
        await queue_scan(session, target, queued_at=now - timedelta(seconds=600))
        await queue_scan(session, target, queued_at=now - timedelta(seconds=30))
        await queue_scan(
            session,
            target,
            status="running",
            locked_at=now,
            queued_at=now - timedelta(seconds=90),
        )

        stats = await queue_stats(session)

        assert stats.pending == 2
        assert stats.running == 1
        assert stats.depth == 3
        assert 590 < stats.oldest_age_seconds < 700

    async def test_an_empty_queue_reports_zero_age(self, session: AsyncSession) -> None:
        stats = await queue_stats(session)
        assert stats.depth == 0
        assert stats.oldest_age_seconds == 0


class TestTargetVersioning:
    async def test_duplicate_versions_are_impossible(self, session: AsyncSession) -> None:
        """Two concurrent version inserts cannot both win (ADR-0004)."""
        target = await make_target(session)

        duplicate = Target(
            family_id=target.family_id,
            version=1,
            kind="prompt",
            model="qwen/qwen3.8-27b",
            system_prompt="different text, same version",
        )
        session.add(duplicate)
        with pytest.raises(IntegrityError):
            await session.flush()
        await session.rollback()

    async def test_a_half_specified_target_is_rejected_by_the_database(
        self, session: AsyncSession
    ) -> None:
        """A prompt target with no prompt should be impossible, not merely unlikely."""
        family = TargetFamily(name="broken")
        session.add(family)
        await session.flush()

        session.add(Target(family_id=family.id, version=1, kind="prompt"))
        with pytest.raises(IntegrityError):
            await session.flush()
        await session.rollback()

    async def test_findings_cascade_when_a_scan_is_deleted(self, session: AsyncSession) -> None:
        from promptproof_core.models import Finding

        target = await make_target(session)
        scan = await queue_scan(session, target)
        session.add(
            Finding(
                scan_id=scan.id,
                probe_id="di-001",
                suite="direct-injection",
                severity="high",
                outcome="resisted",
            )
        )
        await session.commit()

        await session.delete(scan)
        await session.commit()

        remaining = (await session.scalars(select(Finding).where(Finding.scan_id == scan.id))).all()
        assert list(remaining) == []

    async def test_one_finding_per_probe_per_scan(self, session: AsyncSession) -> None:
        """What makes re-running a reclaimed scan idempotent."""
        from promptproof_core.models import Finding

        target = await make_target(session)
        scan = await queue_scan(session, target)

        for _ in range(2):
            session.add(
                Finding(
                    scan_id=scan.id,
                    probe_id="di-001",
                    suite="direct-injection",
                    severity="high",
                    outcome="resisted",
                )
            )
        with pytest.raises(IntegrityError):
            await session.flush()
        await session.rollback()

    async def test_unknown_uuid_is_simply_absent(self, session: AsyncSession) -> None:
        assert await session.get(Scan, uuid.uuid4()) is None
