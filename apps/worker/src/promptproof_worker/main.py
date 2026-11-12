"""The scan worker.

Polls the queue, claims a scan, runs its probes with bounded concurrency, writes
one finding per probe, then grades and completes it.

Two properties matter more than anything else here:

* **A failing probe never fails the scan.** It is recorded `errored` and the rest
  continue. A scan fails only if the target is unreachable or its attempts run out.
* **Re-running a reclaimed scan is idempotent.** Findings are inserted with
  `ON CONFLICT DO NOTHING` against `uq_findings_scan_probe`, and probes that
  already have a finding are skipped, so a worker that died halfway resumes
  rather than duplicating.
"""

import asyncio
import signal
import sys
import time
from datetime import UTC, datetime
from typing import Any

from promptproof_core.db import (
    dispose_engine,
    get_sessionmaker,
    init_engine,
    session_scope,
)
from promptproof_core.grading import grade_findings
from promptproof_core.logging import bind_scan, clear_context, configure_logging, get_logger
from promptproof_core.metrics import (
    configure_metrics,
    record_probe,
    record_reclaim,
    record_scan_completed,
    set_queue_snapshot,
    shutdown_metrics,
)
from promptproof_core.models import Finding, Scan, Target
from promptproof_core.probes import Probe, get_corpus
from promptproof_core.provider import (
    GroqClient,
    ModelClient,
    ProviderQuotaExhausted,
    build_client,
)
from promptproof_core.queue import (
    claim_next_scan,
    fail_exhausted_scans,
    queue_stats,
    renew_lease,
)
from promptproof_core.settings import Settings, get_settings
from promptproof_core.telemetry import (
    configure_telemetry,
    context_from_traceparent,
    set_attributes,
    shutdown_telemetry,
    span,
)
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from .executor import ProbeResult, execute_probe

log = get_logger(__name__)

_shutdown = asyncio.Event()

# Refreshed each poll and read by the queue gauges. The callback that
# OpenTelemetry invokes is synchronous and runs on its own thread, so it
# cannot await a session — it reads this snapshot instead, and a slow
# database therefore cannot stall metric collection.
_QUEUE_SNAPSHOT: list[float] = [0, 0, 0.0]


def _request_shutdown(*_: object) -> None:
    """Finish the scan in hand, then stop. The lease covers us if we are killed."""
    log.info("worker.shutdown_requested")
    _shutdown.set()


async def _wait_for_database(settings: Settings, timeout_seconds: float = 60.0) -> None:
    """Block until Postgres answers and the schema has been migrated.

    The API owns migrations; the worker waits for them rather than racing to
    apply them itself.
    """
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while True:
        try:
            async with get_sessionmaker()() as session:
                await session.execute(text("SELECT 1 FROM alembic_version LIMIT 1"))
            log.info("worker.database_ready")
            return
        except Exception as exc:  # retried until the deadline, then fatal
            if asyncio.get_running_loop().time() > deadline:
                log.error("worker.database_unavailable", reason=type(exc).__name__)
                raise
            await asyncio.sleep(1.0)


async def _already_resolved(session: AsyncSession, scan_id: object) -> set[str]:
    rows = await session.scalars(select(Finding.probe_id).where(Finding.scan_id == scan_id))
    return set(rows.all())


async def _record(
    session: AsyncSession, scan_id: object, probe: Probe, result: ProbeResult
) -> None:
    """Write one finding, ignoring a duplicate from a previous attempt."""
    await session.execute(
        pg_insert(Finding)
        .values(
            scan_id=scan_id,
            probe_id=probe.id,
            suite=probe.suite,
            severity=probe.severity,
            outcome=result.outcome,
            response_excerpt=result.response_excerpt,
            judge_rationale=result.judge_rationale,
            judge_confidence=result.judge_confidence,
            error_reason=result.error_reason,
            target_latency_ms=result.target_latency_ms,
            judge_latency_ms=result.judge_latency_ms,
        )
        .on_conflict_do_nothing(constraint="uq_findings_scan_probe")
    )


QUOTA_EXHAUSTED_REASON = (
    "Provider daily allowance exhausted before the scan could finish. "
    "No verdict was produced. Re-run once the allowance resets."
)

QUOTA_PARTIAL_REASON = (
    "Provider daily allowance exhausted partway through: only {done} of {total} "
    "probes ran. The findings below are genuine, but the target was not fully "
    "tested, so no grade is given. Re-run once the allowance resets."
)


async def _run_probes(
    session: AsyncSession,
    scan: Scan,
    target: Target,
    probes: tuple[Probe, ...],
    settings: Settings,
    client: ModelClient,
) -> bool:
    """Execute probes with at most `scan_concurrency` in flight.

    Returns whether the provider's daily allowance ran out partway through.

    A failing probe never fails the scan -- except for this one condition. When
    the day's allowance is spent, every remaining probe would fail identically,
    and letting them try produces a report whose probes are a third
    `errored` for a reason that has nothing to do with the target. Worse, each
    futile attempt spends from the requests-per-day allowance as well. So the
    first probe to hit it stops the ones that have not started, and the scan
    reports the condition once.
    """
    semaphore = asyncio.Semaphore(settings.scan_concurrency)
    quota_spent = asyncio.Event()

    async def one(probe: Probe) -> tuple[Probe, ProbeResult] | None:
        if quota_spent.is_set():
            # Not started, so not recorded: an unattempted probe is absent from
            # the report rather than present as a failure it never had.
            return None
        async with semaphore:
            if quota_spent.is_set():
                return None
            try:
                with span(
                    "probe.execute",
                    attributes={
                        "probe.id": probe.id,
                        "probe.suite": probe.suite,
                        "probe.severity": probe.severity,
                    },
                ):
                    probe_started = time.monotonic()
                    result = await execute_probe(probe, target, settings, client)
                    record_probe(
                        probe.suite,
                        result.outcome,
                        int((time.monotonic() - probe_started) * 1000),
                    )
                    set_attributes(**{"finding.outcome": result.outcome})
                    return probe, result
            except ProviderQuotaExhausted:
                # The one failure that is about the account rather than the
                # target. Recorded once, at scan level, by the caller.
                if not quota_spent.is_set():
                    log.error(
                        "scan.quota_exhausted",
                        probe_id=probe.id,
                        remaining_probes=len(probes),
                    )
                    quota_spent.set()
                return None
            except Exception as exc:  # a probe never fails the scan
                log.warning(
                    "probe.errored",
                    probe_id=probe.id,
                    suite=probe.suite,
                    reason=type(exc).__name__,
                )
                return probe, ProbeResult(
                    outcome="errored", error_reason=f"unexpected: {type(exc).__name__}"
                )

    for coro in asyncio.as_completed([one(p) for p in probes]):
        outcome = await coro
        if outcome is None:
            continue
        probe, result = outcome
        await _record(session, scan.id, probe, result)
        # Committed as each probe resolves, so the UI can show partial results
        # while the scan is still running (R-003-2).
        await session.commit()

    return quota_spent.is_set()


async def _complete(
    session: AsyncSession, scan: Scan, incomplete_reason: str | None = None
) -> None:
    """Grade and close the scan in one transaction.

    `incomplete_reason` marks a scan that finished with findings but did not
    cover every probe it was asked to. It is recorded on the scan rather than
    spread across absent findings, so a reader can tell "this target passed
    everything" from "this is as far as the day's allowance got".

    Such a scan gets **no grade**. A live drill produced grade `A` from four of
    forty-two probes, because the four that ran all resisted: a headline saying
    the target resisted everything, over a scan that never tried nine-tenths of
    the corpus. The caveat was recorded, but nobody reads a caveat before a
    letter. A grade asserts coverage it did not have, so there is none.
    """
    findings = list(
        (await session.scalars(select(Finding).where(Finding.scan_id == scan.id))).all()
    )
    grade, counts = grade_findings(findings)

    # No grade on a scan that did not cover the corpus: see above.
    scan.grade = None if incomplete_reason else grade
    scan.counts = counts.to_json()
    scan.finished_at = datetime.now(UTC)
    scan.status = "completed"
    # Set on a completed scan deliberately: the findings it does have are
    # genuine and worth showing, and the caveat belongs beside them.
    scan.error = incomplete_reason
    scan.locked_at = None
    scan.locked_by = None
    await session.commit()

    # Measured from submission, not from claim: the queue wait is part of what
    # the person waited through.
    record_scan_completed(
        "completed", grade, int((scan.finished_at - scan.queued_at).total_seconds() * 1000)
    )

    log.info(
        "scan.completed",
        scan_id=str(scan.id),
        grade=grade,
        total=counts.total,
        errored=counts.errored,
    )


async def _fail(session: AsyncSession, scan: Scan, reason: str) -> None:
    scan.status = "failed"
    scan.error = reason
    scan.finished_at = datetime.now(UTC)
    scan.locked_at = None
    scan.locked_by = None
    await session.commit()

    record_scan_completed(
        "failed", None, int((scan.finished_at - scan.queued_at).total_seconds() * 1000)
    )
    log.error("scan.failed", scan_id=str(scan.id), reason=reason)


async def _hold_lease(scan_id: Any, settings: Settings, stop: asyncio.Event) -> None:
    """Keep this worker's claim alive while the scan is still running.

    `locked_at` is written once, at claim time. A scan that outlives its lease
    is indistinguishable from one whose worker died, so another worker claims
    it and runs it again -- duplicate findings, and duplicate spend against the
    provider allowance that pacing exists to protect.

    That stopped being theoretical when scans began waiting out rate-limit
    windows deliberately: a full-corpus scan now takes minutes, and a single
    paced wait can be a minute of it. The first measured run used 234 seconds
    of a 300-second lease, with a cheaper judge than production uses.

    Renewed at a third of the lease, so two consecutive failures still leave a
    full period of slack. Runs on its own session: the scan's session is busy
    committing findings, and a heartbeat that queues behind the work it is
    meant to protect protects nothing.
    """
    interval = max(settings.scan_lease_seconds / 3.0, 5.0)
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
            return  # stopped: the scan finished
        except TimeoutError:
            pass

        try:
            async with session_scope() as session:
                held = await renew_lease(session, scan_id=scan_id, worker_id=settings.worker_id)
        except Exception as exc:
            # A failed renewal is not fatal on its own: there is slack, and the
            # next one may succeed. Logged because a run of them is not.
            log.warning("lease.renew_failed", reason=type(exc).__name__)
            continue

        if not held:
            # Someone else holds this scan now, or it has already been resolved.
            # Carrying on would duplicate work; the scan itself is left alone
            # rather than torn down, since whoever holds it now is running it.
            log.warning("lease.lost", scan_id=str(scan_id))
            return


async def _process(
    session: AsyncSession,
    scan: Scan,
    settings: Settings,
    client: ModelClient,
    parent: object | None = None,
) -> None:
    bind_scan(str(scan.id))
    try:
        with span(
            "scan.execute",
            parent=parent,  # type: ignore[arg-type]
            attributes={
                "scan.id": str(scan.id),
                "scan.attempt": scan.attempts,
                "scan.concurrency": settings.scan_concurrency,
            },
        ):
            stop = asyncio.Event()
            keepalive = asyncio.create_task(_hold_lease(scan.id, settings, stop))
            try:
                await _execute(session, scan, settings, client)
            finally:
                stop.set()
                await keepalive
    finally:
        clear_context()


async def _execute(
    session: AsyncSession, scan: Scan, settings: Settings, client: ModelClient
) -> None:
    target = await session.get(Target, scan.target_id)
    if target is None:
        await _fail(session, scan, "Target no longer exists")
        return

    probes = get_corpus().for_suites(list(scan.suites))
    if not probes:
        await _fail(session, scan, "The selected suites contain no probes")
        return

    done = await _already_resolved(session, scan.id)
    pending = tuple(p for p in probes if p.id not in done)

    scan.judge_model = settings.judge_model
    await session.commit()

    log.info(
        "scan.started",
        attempt=scan.attempts,
        probe_count=len(probes),
        resuming=len(done),
    )
    quota_spent = await _run_probes(session, scan, target, pending, settings, client)

    if quota_spent:
        resolved = await _already_resolved(session, scan.id)
        if not resolved:
            # Nothing to grade, and the cause is the account rather than the
            # target. One honest scan-level failure beats a report of
            # identical errors that look like findings about the target.
            await _fail(session, scan, QUOTA_EXHAUSTED_REASON)
            set_attributes(**{"scan.quota_exhausted": True})
            return
        log.warning(
            "scan.partial",
            reason="provider daily allowance exhausted",
            graded=len(resolved),
            requested=len(probes),
        )
        incomplete = QUOTA_PARTIAL_REASON.format(done=len(resolved), total=len(probes))
    else:
        incomplete = None

    await _complete(session, scan, incomplete_reason=incomplete)
    set_attributes(**{"scan.grade": scan.grade or "none", "scan.quota_exhausted": quota_spent})


async def _loop(settings: Settings, client: ModelClient) -> None:
    sessionmaker = get_sessionmaker()
    idle_logged = False

    while not _shutdown.is_set():
        async with sessionmaker() as session:
            # Abandoned scans that are out of attempts become an honest failure
            # for the user rather than sitting in the queue forever.
            exhausted = await fail_exhausted_scans(
                session,
                lease_seconds=settings.scan_lease_seconds,
                max_attempts=settings.scan_max_attempts,
            )
            if exhausted:
                await session.commit()
                record_reclaim("exhausted", len(exhausted))
                log.warning("scan.exhausted", count=len(exhausted))

            stats = await queue_stats(session)
            _QUEUE_SNAPSHOT[:] = [stats.pending, stats.running, stats.oldest_age_seconds]

            scan = await claim_next_scan(
                session,
                worker_id=settings.worker_id,
                lease_seconds=settings.scan_lease_seconds,
                max_attempts=settings.scan_max_attempts,
            )
            if scan is None:
                await session.commit()
                if not idle_logged:
                    log.info("worker.idle")
                    idle_logged = True
                await asyncio.sleep(settings.worker_poll_seconds)
                continue

            idle_logged = False
            await session.commit()

            if scan.attempts > 1:
                record_reclaim("lease_expired")

            # This is the join. The context was serialised by the API when the
            # scan was submitted, possibly minutes ago in another process;
            # rebuilding it here is what makes submission and execution one
            # trace rather than two unrelated ones.
            parent = context_from_traceparent(scan.traceparent)
            queue_wait_ms = int((datetime.now(UTC) - scan.queued_at).total_seconds() * 1000)

            with span(
                "scan.claim",
                parent=parent,
                attributes={
                    "scan.id": str(scan.id),
                    "worker.id": settings.worker_id,
                    "scan.attempt": scan.attempts,
                    # Makes the queue wait a measured quantity rather than an
                    # unexplained gap between two spans.
                    "queue.wait_ms": queue_wait_ms,
                },
            ):
                pass

            await _process(session, scan, settings, client, parent)


async def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level, settings.environment)
    settings.assert_judge_differs()
    settings.assert_production_safe()

    init_engine(settings)
    await _wait_for_database(settings)
    log.info(
        "worker.start",
        worker_id=settings.worker_id,
        concurrency=settings.scan_concurrency,
        lease_seconds=settings.scan_lease_seconds,
    )

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _request_shutdown)
        except NotImplementedError:
            # Windows: signal handlers are not available on the proactor loop.
            signal.signal(sig, _request_shutdown)

    configure_telemetry(settings, service_name="promptproof-worker")
    configure_metrics(settings, service_name="promptproof-worker")
    set_queue_snapshot(
        lambda: (int(_QUEUE_SNAPSHOT[0]), int(_QUEUE_SNAPSHOT[1]), _QUEUE_SNAPSHOT[2])
    )

    try:
        client: GroqClient = build_client(settings)
    except ValueError as exc:
        # A missing key is a configuration error, not a runtime one. Exiting
        # with the reason beats idling while scans pile up unexplained.
        log.error("worker.misconfigured", reason=str(exc))
        raise SystemExit(1) from exc

    try:
        await _loop(settings, client)
    finally:
        await client.aclose()
        await dispose_engine()
        shutdown_telemetry()
        shutdown_metrics()
        log.info("worker.stop")


def run() -> None:
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    run()
