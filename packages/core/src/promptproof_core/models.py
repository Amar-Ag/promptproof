"""Persistence models.

Three properties are load-bearing and are enforced here rather than by convention:

* A `Target` row is immutable once written. Editing inserts a new row in the same
  family with `version + 1` (ADR-0004), guarded by a unique constraint.
* The `scans` table is the job queue (ADR-0002). `status`, `locked_at`, `locked_by`
  and `attempts` are queue state, not decoration.
* `scans.traceparent` carries W3C trace context across the API/worker boundary, so
  one trace spans submission and execution.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    ARRAY,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

TARGET_KINDS = ("prompt", "endpoint")
SCAN_STATUSES = ("pending", "running", "completed", "failed", "cancelled")
TERMINAL_SCAN_STATUSES = ("completed", "failed", "cancelled")
SEVERITIES = ("low", "medium", "high", "critical")
OUTCOMES = ("exploited", "resisted", "errored")
GRADES = ("A", "B", "C", "D", "F")


def _in(column: str, values: tuple[str, ...]) -> str:
    rendered = ", ".join(f"'{v}'" for v in values)
    return f"{column} IN ({rendered})"


class Base(DeclarativeBase):
    pass


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


def _created_at() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class TargetFamily(Base):
    """The lineage of versions of one logically-same target."""

    __tablename__ = "target_families"

    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = _created_at()

    versions: Mapped[list["Target"]] = relationship(
        back_populates="family",
        cascade="all, delete-orphan",
        order_by="desc(Target.version)",
    )


class Target(Base):
    """One immutable version of a target. Never updated, never deleted."""

    __tablename__ = "targets"
    __table_args__ = (
        UniqueConstraint("family_id", "version", name="uq_targets_family_version"),
        CheckConstraint(_in("kind", TARGET_KINDS), name="ck_targets_kind"),
        CheckConstraint("version >= 1", name="ck_targets_version_positive"),
        # A prompt target needs a prompt and a model; an endpoint target needs a URL
        # and explicit ownership confirmation. Enforced in the database so no code
        # path can create a half-specified target.
        CheckConstraint(
            "(kind = 'prompt' AND system_prompt IS NOT NULL AND model IS NOT NULL) "
            "OR (kind = 'endpoint' AND endpoint_url IS NOT NULL AND ownership_confirmed)",
            name="ck_targets_kind_fields",
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    family_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("target_families.id", ondelete="CASCADE"), nullable=False, index=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)

    # Prompt targets
    model: Mapped[str | None] = mapped_column(String(128))
    system_prompt: Mapped[str | None] = mapped_column(Text)

    # Endpoint targets
    endpoint_url: Mapped[str | None] = mapped_column(Text)
    endpoint_auth_header: Mapped[str | None] = mapped_column(Text)
    ownership_confirmed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )

    created_at: Mapped[datetime] = _created_at()

    family: Mapped[TargetFamily] = relationship(back_populates="versions")


class Scan(Base):
    """One execution of chosen suites against one target version.

    Also the queue row. See ADR-0002 and the claim statement in
    `promptproof_core.queue`.
    """

    __tablename__ = "scans"
    __table_args__ = (
        CheckConstraint(_in("status", SCAN_STATUSES), name="ck_scans_status"),
        CheckConstraint("grade IS NULL OR " + _in("grade", GRADES), name="ck_scans_grade"),
        CheckConstraint("attempts >= 0", name="ck_scans_attempts"),
        CheckConstraint("array_length(suites, 1) >= 1", name="ck_scans_suites_nonempty"),
        # The claim predicate scans only pending rows, so the index only indexes them.
        Index(
            "ix_scans_pending_queued_at",
            "queued_at",
            postgresql_where=text("status = 'pending'"),
        ),
        Index(
            "ix_scans_running_locked_at",
            "locked_at",
            postgresql_where=text("status = 'running'"),
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    target_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("targets.id", ondelete="CASCADE"), nullable=False, index=True
    )
    suites: Mapped[list[str]] = mapped_column(ARRAY(String(64)), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'pending'")
    )

    # Reproducibility: what produced this report.
    probe_corpus_version: Mapped[str | None] = mapped_column(String(64))
    judge_model: Mapped[str | None] = mapped_column(String(128))
    probe_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))

    # Trace context, carried across the process boundary.
    traceparent: Mapped[str | None] = mapped_column(String(64))

    # Queue state.
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    locked_by: Mapped[str | None] = mapped_column(String(64))

    queued_at: Mapped[datetime] = _created_at()
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Result.
    grade: Mapped[str | None] = mapped_column(String(1))
    counts: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(Text)

    target: Mapped[Target] = relationship()
    findings: Mapped[list["Finding"]] = relationship(
        back_populates="scan", cascade="all, delete-orphan"
    )


class Finding(Base):
    """The outcome of one probe in one scan, with the evidence for it."""

    __tablename__ = "findings"
    __table_args__ = (
        CheckConstraint(_in("severity", SEVERITIES), name="ck_findings_severity"),
        CheckConstraint(_in("outcome", OUTCOMES), name="ck_findings_outcome"),
        # One finding per probe per scan: makes a reclaimed, re-run scan idempotent.
        UniqueConstraint("scan_id", "probe_id", name="uq_findings_scan_probe"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    scan_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("scans.id", ondelete="CASCADE"), nullable=False, index=True
    )
    probe_id: Mapped[str] = mapped_column(String(64), nullable=False)
    suite: Mapped[str] = mapped_column(String(64), nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)

    # Evidence. Lives here, behind the API — never in telemetry.
    response_excerpt: Mapped[str | None] = mapped_column(Text)
    judge_rationale: Mapped[str | None] = mapped_column(Text)
    judge_confidence: Mapped[float | None] = mapped_column(Numeric(3, 2))
    error_reason: Mapped[str | None] = mapped_column(String(256))

    target_latency_ms: Mapped[int | None] = mapped_column(Integer)
    judge_latency_ms: Mapped[int | None] = mapped_column(Integer)

    created_at: Mapped[datetime] = _created_at()

    scan: Mapped[Scan] = relationship(back_populates="findings")
