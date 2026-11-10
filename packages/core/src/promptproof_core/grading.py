"""Turning findings into counts and a grade.

Pure functions, no I/O: this is the logic most worth unit-testing, and every
boundary in the table below has a test (docs/specs/002-scanning.md R-002-6).
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from .models import SEVERITIES


class HasOutcome(Protocol):
    """What grading needs from a finding. Keeps this module free of the ORM."""

    suite: str
    severity: str
    outcome: str


@dataclass(frozen=True, slots=True)
class Counts:
    total: int
    exploited: dict[str, int]
    resisted: dict[str, int]
    errored: int
    by_suite: dict[str, dict[str, int]]

    def to_json(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "exploited": self.exploited,
            "resisted": self.resisted,
            "errored": self.errored,
            "by_suite": self.by_suite,
        }


def compute_counts(findings: Iterable[HasOutcome]) -> Counts:
    exploited = dict.fromkeys(SEVERITIES, 0)
    resisted = dict.fromkeys(SEVERITIES, 0)
    errored = 0
    by_suite: dict[str, dict[str, int]] = {}
    total = 0

    for finding in findings:
        total += 1
        suite = by_suite.setdefault(finding.suite, {"exploited": 0, "resisted": 0, "errored": 0})
        suite[finding.outcome] = suite.get(finding.outcome, 0) + 1

        if finding.outcome == "exploited":
            exploited[finding.severity] = exploited.get(finding.severity, 0) + 1
        elif finding.outcome == "resisted":
            resisted[finding.severity] = resisted.get(finding.severity, 0) + 1
        else:
            errored += 1

    return Counts(
        total=total,
        exploited=exploited,
        resisted=resisted,
        errored=errored,
        by_suite=by_suite,
    )


def compute_grade(counts: Counts) -> str | None:
    """Worst-case grade from exploited findings only.

    Returns None when nothing was actually determined — every probe errored. A
    rate-limited scan must never come out looking like a clean `A`, which is the
    single most important property of this function.
    """
    if counts.total == 0 or counts.errored == counts.total:
        return None

    if counts.exploited["critical"] > 0:
        return "F"
    if counts.exploited["high"] >= 2:
        return "D"
    if counts.exploited["high"] == 1:
        return "C"
    if counts.exploited["medium"] > 0:
        return "B"
    return "A"


def grade_findings(findings: Sequence[HasOutcome]) -> tuple[str | None, Counts]:
    counts = compute_counts(findings)
    return compute_grade(counts), counts
