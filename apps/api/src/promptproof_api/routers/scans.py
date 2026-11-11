"""Scan submission and reporting.

Submitting a scan writes a row and returns. The API makes no model call and runs
no probe (ADR-0001); the worker picks the row up off the queue.
"""

import base64
import binascii
import uuid
from datetime import datetime

from fastapi import APIRouter, HTTPException, Response, status
from promptproof_core.logging import get_logger
from promptproof_core.models import Finding, Scan, Target, TargetFamily
from promptproof_core.probes import get_corpus
from promptproof_core.suites import unknown_suites
from promptproof_core.telemetry import current_traceparent, set_attributes, span
from sqlalchemy import func, select
from sqlalchemy.orm import aliased

from ..deps import SessionDep
from ..schemas import (
    FindingOut,
    ScanCreate,
    ScanCreated,
    ScanDetail,
    ScanOut,
    ScanPage,
)

router = APIRouter(tags=["scans"])
log = get_logger(__name__)

DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100

# Findings are ordered for reading, not for storage: what got through comes
# first, then what we could not determine, then what held. Within each group,
# worst severity first.
_OUTCOME_ORDER = {"exploited": 0, "errored": 1, "resisted": 2}
_SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}


def _encode_cursor(queued_at: datetime, scan_id: uuid.UUID) -> str:
    raw = f"{queued_at.isoformat()}|{scan_id}".encode()
    return base64.urlsafe_b64encode(raw).decode()


def _decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    try:
        raw = base64.urlsafe_b64decode(cursor.encode()).decode()
        queued_at_str, scan_id_str = raw.split("|", 1)
        return datetime.fromisoformat(queued_at_str), uuid.UUID(scan_id_str)
    except (ValueError, binascii.Error) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Malformed cursor.",
        ) from exc


@router.post(
    "/scans",
    response_model=ScanCreated,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Submit a scan",
)
async def submit_scan(payload: ScanCreate, session: SessionDep) -> ScanCreated:
    with span("scan.submit"):
        target = await session.get(Target, payload.target_id)
        if target is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Target not found")

        if unknown := unknown_suites(payload.suites):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Unknown suite(s): {', '.join(unknown)}",
            )

        corpus = get_corpus()
        probes = corpus.for_suites(payload.suites)
        if not probes:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="The selected suites contain no probes.",
            )

        # Serialised from this span and persisted on the row so the worker can
        # continue this trace after the queue. Without it, submission and
        # execution are two unrelated traces and the queue wait is invisible.
        traceparent = current_traceparent()

        scan = Scan(
            target_id=target.id,
            suites=list(dict.fromkeys(payload.suites)),
            status="pending",
            probe_count=len(probes),
            probe_corpus_version=corpus.version,
            traceparent=traceparent,
        )
        session.add(scan)
        await session.flush()

        set_attributes(
            **{
                "scan.id": str(scan.id),
                "target.id": str(target.id),
                "target.kind": target.kind,
                "target.version": target.version,
                "suite.count": len(scan.suites),
                "probe.count": scan.probe_count,
                "corpus.version": corpus.version,
            }
        )

        log.info(
            "scan.submitted",
            scan_id=str(scan.id),
            target_id=str(target.id),
            target_kind=target.kind,
            suite_count=len(scan.suites),
            probe_count=scan.probe_count,
        )

    return ScanCreated(id=scan.id, status=scan.status, probe_count=scan.probe_count)


@router.get("/scans", response_model=ScanPage, summary="List scans, newest first")
async def list_scans(
    session: SessionDep,
    limit: int = DEFAULT_PAGE_SIZE,
    cursor: str | None = None,
) -> ScanPage:
    limit = max(1, min(limit, MAX_PAGE_SIZE))
    target = aliased(Target)
    family = aliased(TargetFamily)

    # No aggregate over findings here: the denormalised `counts` column is what
    # the list reads (R-003-1).
    stmt = (
        select(Scan, family.name, target.version)
        .join(target, target.id == Scan.target_id)
        .join(family, family.id == target.family_id)
        .order_by(Scan.queued_at.desc(), Scan.id.desc())
        .limit(limit + 1)
    )

    if cursor:
        queued_at, scan_id = _decode_cursor(cursor)
        stmt = stmt.where(
            (Scan.queued_at < queued_at) | ((Scan.queued_at == queued_at) & (Scan.id < scan_id))
        )

    rows = (await session.execute(stmt)).all()
    has_more = len(rows) > limit
    rows = rows[:limit]

    items = [ScanOut.of(scan, name, version) for scan, name, version in rows]
    next_cursor = (
        _encode_cursor(rows[-1][0].queued_at, rows[-1][0].id) if has_more and rows else None
    )
    return ScanPage(items=items, next_cursor=next_cursor)


@router.get("/scans/{scan_id}", response_model=ScanDetail, summary="Get a scan and its findings")
async def get_scan(scan_id: uuid.UUID, session: SessionDep, response: Response) -> ScanDetail:
    row = (
        await session.execute(
            select(Scan, TargetFamily.name, Target.version)
            .join(Target, Target.id == Scan.target_id)
            .join(TargetFamily, TargetFamily.id == Target.family_id)
            .where(Scan.id == scan_id)
        )
    ).first()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Scan not found")
    scan, name, version = row

    findings = list(
        (await session.scalars(select(Finding).where(Finding.scan_id == scan_id))).all()
    )
    findings.sort(
        key=lambda f: (
            _OUTCOME_ORDER.get(f.outcome, 9),
            _SEVERITY_ORDER.get(f.severity, 9),
            f.probe_id,
        )
    )

    # A running scan is polled, so tell caches not to hold it.
    if scan.status in ("pending", "running"):
        response.headers["Cache-Control"] = "no-store"

    # One lookup for the whole report, so each finding can show the payload that
    # produced it and the remediation for it.
    probes = {p.id: p for p in get_corpus().probes}

    return ScanDetail(
        scan=ScanOut.of(scan, name, version),
        resolved_count=len(findings),
        findings=[FindingOut.of(f, probes.get(f.probe_id)) for f in findings],
    )


@router.get("/scans/{scan_id}/findings/count", summary="Resolved probe count")
async def finding_count(scan_id: uuid.UUID, session: SessionDep) -> dict[str, int]:
    """Cheap progress poll for a running scan, without re-sending every finding."""
    count = await session.scalar(
        select(func.count()).select_from(Finding).where(Finding.scan_id == scan_id)
    )
    return {"resolved_count": int(count or 0)}
