"""What the alert rules actually measure.

The state machine is unit-tested; this is about whether the two queries report
the truth. A rule with perfect firing logic and a wrong measurement is worse
than no rule, because it is believed.
"""

from datetime import UTC, datetime, timedelta

from promptproof_core.alerts import (
    FAILURE_RATE_HIGH,
    QUEUE_STALLED,
    Evaluator,
    evaluate_all,
    measure_failure_rate,
    measure_queue_age,
)
from promptproof_core.models import Scan, Target, TargetFamily
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def make_target(session: AsyncSession) -> Target:
    family = TargetFamily(name="Alert target")
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
    queued_ago: int = 0,
    finished_ago: int | None = None,
) -> Scan:
    scan = Scan(target_id=target.id, suites=["tool-abuse"], status=status, probe_count=1)
    session.add(scan)
    await session.flush()

    now = datetime.now(UTC)
    await session.execute(
        text("UPDATE scans SET queued_at = :q WHERE id = :i").bindparams(
            q=now - timedelta(seconds=queued_ago), i=scan.id
        )
    )
    if finished_ago is not None:
        await session.execute(
            text("UPDATE scans SET finished_at = :f WHERE id = :i").bindparams(
                f=now - timedelta(seconds=finished_ago), i=scan.id
            )
        )
    await session.commit()
    return scan


class TestQueueAge:
    async def test_an_empty_queue_reports_zero(self, session: AsyncSession) -> None:
        age, context = await measure_queue_age(session)
        assert age == 0
        assert context["waiting"] == 0

    async def test_it_reports_the_oldest_pending_scan(self, session: AsyncSession) -> None:
        target = await make_target(session)
        await add_scan(session, target, status="pending", queued_ago=30)
        await add_scan(session, target, status="pending", queued_ago=600)

        age, context = await measure_queue_age(session)
        assert 590 < age < 700
        assert context["waiting"] == 2

    async def test_a_running_scan_is_not_waiting(self, session: AsyncSession) -> None:
        """A scan being worked on is progress, not a stalled queue.

        Counting it would mean a single slow scan fires the alert that exists
        to say nothing is being picked up.
        """
        target = await make_target(session)
        await add_scan(session, target, status="running", queued_ago=9999)

        age, context = await measure_queue_age(session)
        assert age == 0
        assert context["waiting"] == 0

    async def test_completed_scans_are_ignored(self, session: AsyncSession) -> None:
        target = await make_target(session)
        await add_scan(session, target, status="completed", queued_ago=9999, finished_ago=1)
        age, _ = await measure_queue_age(session)
        assert age == 0


class TestFailureRate:
    async def test_nothing_finished_means_no_rate(self, session: AsyncSession) -> None:
        """An idle system must stay quiet.

        Reporting 1.0 for zero scans would alert every night, which is how an
        alert gets muted and then ignored when it matters.
        """
        rate, context = await measure_failure_rate(session)
        assert rate == 0.0
        assert context["total"] == 0

    async def test_it_measures_the_recent_window_only(self, session: AsyncSession) -> None:
        target = await make_target(session)
        # Old failures, outside the window: must not count.
        for _ in range(5):
            await add_scan(session, target, status="failed", finished_ago=5_000)
        # Recent, healthy.
        for _ in range(4):
            await add_scan(session, target, status="completed", finished_ago=10)

        rate, context = await measure_failure_rate(session, window_seconds=600)
        assert rate == 0.0
        assert context["total"] == 4

    async def test_it_computes_the_proportion(self, session: AsyncSession) -> None:
        target = await make_target(session)
        for _ in range(3):
            await add_scan(session, target, status="failed", finished_ago=10)
        for _ in range(7):
            await add_scan(session, target, status="completed", finished_ago=10)

        rate, context = await measure_failure_rate(session, window_seconds=600)
        assert rate == 0.3
        assert context == {"failed": 3, "total": 10, "window_seconds": 600}

    async def test_unfinished_scans_do_not_move_the_rate(self, session: AsyncSession) -> None:
        """Including in-flight scans would make the rate swing as a batch lands."""
        target = await make_target(session)
        await add_scan(session, target, status="failed", finished_ago=10)
        for _ in range(9):
            await add_scan(session, target, status="running", queued_ago=10)

        rate, context = await measure_failure_rate(session)
        assert context["total"] == 1
        assert rate == 1.0


class TestEvaluateAll:
    async def test_a_healthy_system_fires_nothing(self, session: AsyncSession) -> None:
        target = await make_target(session)
        await add_scan(session, target, status="completed", finished_ago=5)
        await add_scan(session, target, status="pending", queued_ago=3)

        evaluator = Evaluator()
        now = datetime.now(UTC)
        assert await evaluate_all(session, evaluator, now) == []
        assert await evaluate_all(session, evaluator, now + timedelta(seconds=900)) == []

    async def test_a_stalled_queue_fires_once_sustained(self, session: AsyncSession) -> None:
        target = await make_target(session)
        await add_scan(session, target, status="pending", queued_ago=900)

        evaluator = Evaluator()
        now = datetime.now(UTC)

        assert await evaluate_all(session, evaluator, now) == []
        fired = await evaluate_all(
            session, evaluator, now + timedelta(seconds=QUEUE_STALLED.sustain_seconds + 1)
        )
        assert [a.rule.name for a in fired] == ["scan-queue-stalled"]
        assert fired[0].context["waiting"] == 1

        # And not again while it is still firing.
        assert await evaluate_all(session, evaluator, now + timedelta(seconds=1200)) == []

    async def test_a_bad_failure_rate_fires(self, session: AsyncSession) -> None:
        target = await make_target(session)
        for _ in range(4):
            await add_scan(session, target, status="failed", finished_ago=10)
        for _ in range(6):
            await add_scan(session, target, status="completed", finished_ago=10)

        evaluator = Evaluator()
        now = datetime.now(UTC)
        assert await evaluate_all(session, evaluator, now) == []

        fired = await evaluate_all(
            session, evaluator, now + timedelta(seconds=FAILURE_RATE_HIGH.sustain_seconds + 1)
        )
        assert [a.rule.name for a in fired] == ["scan-failure-rate-high"]
        assert fired[0].value == 0.4
