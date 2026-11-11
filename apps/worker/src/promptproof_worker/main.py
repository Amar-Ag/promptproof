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
from datetime import UTC, datetime

from promptproof_core.db import dispose_engine, get_sessionmaker, init_engine
from promptproof_core.grading import grade_findings
from promptproof_core.logging import bind_scan, clear_context, configure_logging, get_logger
from promptproof_core.models import Finding, Scan, Target
from promptproof_core.probes import Probe, get_corpus
from promptproof_core.provider import GroqClient, ModelClient, build_client
from promptproof_core.queue import claim_next_scan, fail_exhausted_scans
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


async def _run_probes(
    session: AsyncSession,
    scan: Scan,
    target: Target,
    probes: tuple[Probe, ...],
    settings: Settings,
    client: ModelClient,
) -> None:
    """Execute probes with at most `scan_concurrency` in flight."""
    semaphore = asyncio.Semaphore(settings.scan_concurrency)

    async def one(probe: Probe) -> tuple[Probe, ProbeResult]:
        async with semaphore:
            try:
                with span(
                    "probe.execute",
                    attributes={
                        "probe.id": probe.id,
                        "probe.suite": probe.suite,
                        "probe.severity": probe.severity,
                    },
                ):
                    result = await execute_probe(probe, target, settings, client)
                    set_attributes(**{"finding.outcome": result.outcome})
                    return probe, result
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
        probe, result = await coro
        await _record(session, scan.id, probe, result)
        # Committed as each probe resolves, so the UI can show partial results
        # while the scan is still running (R-003-2).
        await session.commit()


async def _complete(session: AsyncSession, scan: Scan) -> None:
    """Grade and close the scan in one transaction."""
    findings = list(
        (await session.scalars(select(Finding).where(Finding.scan_id == scan.id))).all()
    )
    grade, counts = grade_findings(findings)

    scan.grade = grade
    scan.counts = counts.to_json()
    scan.finished_at = datetime.now(UTC)
    scan.status = "completed"
    scan.locked_at = None
    scan.locked_by = None
    await session.commit()

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
    log.error("scan.failed", scan_id=str(scan.id), reason=reason)


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
            await _execute(session, scan, settings, client)
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
    await _run_probes(session, scan, target, pending, settings, client)
    await _complete(session, scan)
    set_attributes(**{"scan.grade": scan.grade or "none"})


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
                log.warning("scan.exhausted", count=len(exhausted))

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
        log.info("worker.stop")


def run() -> None:
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    run()
