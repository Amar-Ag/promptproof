"""HTTP request and response shapes.

Separate from the persistence models on purpose: an ORM object is never returned
from an endpoint, and a field like `endpoint_auth_header` exists in the database
but must never appear in a response.
"""

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal, Self

from promptproof_core.catalog import TARGET_MODEL_IDS
from promptproof_core.models import Finding, Scan, Target, TargetFamily
from pydantic import BaseModel, ConfigDict, Field, model_validator

MAX_SYSTEM_PROMPT = 20_000

Name = Annotated[str, Field(min_length=1, max_length=200)]
SystemPrompt = Annotated[str, Field(min_length=1, max_length=MAX_SYSTEM_PROMPT)]


class SuiteOut(BaseModel):
    key: str
    title: str
    description: str
    probe_count: int


class ModelOut(BaseModel):
    id: str
    label: str


# ── Targets ───────────────────────────────────────────────────────────────────


class TargetSpec(BaseModel):
    """The part of a target that changes between versions."""

    kind: Literal["prompt", "endpoint"] = "prompt"

    system_prompt: SystemPrompt | None = None
    model: str | None = None

    endpoint_url: str | None = None
    endpoint_auth_header: str | None = None
    ownership_confirmed: bool = False

    @model_validator(mode="after")
    def _check_kind_fields(self) -> Self:
        if self.kind == "prompt":
            missing = [
                f
                for f, v in (("system_prompt", self.system_prompt), ("model", self.model))
                if v is None
            ]
            if missing:
                raise ValueError(f"A prompt target requires {' and '.join(missing)}.")
            if self.model not in TARGET_MODEL_IDS:
                raise ValueError(
                    f"Unknown model {self.model!r}. Allowed models: {', '.join(TARGET_MODEL_IDS)}."
                )
        else:
            if not self.endpoint_url:
                raise ValueError("An endpoint target requires endpoint_url.")
            if not self.ownership_confirmed:
                raise ValueError(
                    "An endpoint target requires ownership_confirmed. Only scan an "
                    "endpoint you own or are authorised to test."
                )
        return self


class TargetCreate(TargetSpec):
    name: Name


class TargetVersionCreate(TargetSpec):
    """A new version of an existing family. The name belongs to the family."""


class TargetOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    family_id: uuid.UUID
    name: str
    version: int
    kind: str
    model: str | None
    system_prompt: str | None
    endpoint_url: str | None
    # The stored auth header is never returned, only its presence.
    has_auth_header: bool
    ownership_confirmed: bool
    created_at: datetime

    @classmethod
    def of(cls, target: Target, name: str) -> "TargetOut":
        return cls(
            id=target.id,
            family_id=target.family_id,
            name=name,
            version=target.version,
            kind=target.kind,
            model=target.model,
            system_prompt=target.system_prompt,
            endpoint_url=target.endpoint_url,
            has_auth_header=target.endpoint_auth_header is not None,
            ownership_confirmed=target.ownership_confirmed,
            created_at=target.created_at,
        )


class TargetFamilyOut(BaseModel):
    id: uuid.UUID
    name: str
    created_at: datetime
    latest_version: int
    kind: str
    versions: list[TargetOut]

    @classmethod
    def of(cls, family: TargetFamily, versions: list[Target]) -> "TargetFamilyOut":
        ordered = sorted(versions, key=lambda t: t.version, reverse=True)
        return cls(
            id=family.id,
            name=family.name,
            created_at=family.created_at,
            latest_version=ordered[0].version if ordered else 0,
            kind=ordered[0].kind if ordered else "prompt",
            versions=[TargetOut.of(t, family.name) for t in ordered],
        )


# ── Scans ─────────────────────────────────────────────────────────────────────


class ScanCreate(BaseModel):
    target_id: uuid.UUID
    suites: Annotated[list[str], Field(min_length=1)]


class ScanOut(BaseModel):
    id: uuid.UUID
    target_id: uuid.UUID
    target_name: str
    target_version: int
    suites: list[str]
    status: str
    grade: str | None
    counts: dict[str, Any] | None
    probe_count: int
    attempts: int
    queued_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    error: str | None
    probe_corpus_version: str | None
    judge_model: str | None

    @classmethod
    def of(cls, scan: Scan, target_name: str, target_version: int) -> "ScanOut":
        return cls(
            id=scan.id,
            target_id=scan.target_id,
            target_name=target_name,
            target_version=target_version,
            suites=list(scan.suites),
            status=scan.status,
            grade=scan.grade,
            counts=scan.counts,
            probe_count=scan.probe_count,
            attempts=scan.attempts,
            queued_at=scan.queued_at,
            started_at=scan.started_at,
            finished_at=scan.finished_at,
            error=scan.error,
            probe_corpus_version=scan.probe_corpus_version,
            judge_model=scan.judge_model,
        )


class ScanCreated(BaseModel):
    id: uuid.UUID
    status: str
    probe_count: int


class ScanPage(BaseModel):
    items: list[ScanOut]
    next_cursor: str | None


class FindingOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    probe_id: str
    suite: str
    severity: str
    outcome: str
    response_excerpt: str | None
    judge_rationale: str | None
    judge_confidence: float | None
    error_reason: str | None
    target_latency_ms: int | None
    judge_latency_ms: int | None
    created_at: datetime

    @classmethod
    def of(cls, finding: Finding) -> "FindingOut":
        return cls(
            id=finding.id,
            probe_id=finding.probe_id,
            suite=finding.suite,
            severity=finding.severity,
            outcome=finding.outcome,
            response_excerpt=finding.response_excerpt,
            judge_rationale=finding.judge_rationale,
            judge_confidence=(
                float(finding.judge_confidence) if finding.judge_confidence is not None else None
            ),
            error_reason=finding.error_reason,
            target_latency_ms=finding.target_latency_ms,
            judge_latency_ms=finding.judge_latency_ms,
            created_at=finding.created_at,
        )


class ScanDetail(BaseModel):
    scan: ScanOut
    resolved_count: int
    findings: list[FindingOut]
