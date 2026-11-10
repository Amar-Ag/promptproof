"""Running one probe against one target.

**Phase 1 placeholder.** Real execution — calling the target, then asking the
judge for a verdict — lands with issues #13 (provider client), #14 (probe
execution) and #15 (judge). Until then this records every probe as `errored`
with an honest reason.

That choice is deliberate. Returning a fabricated `resisted` would make the
walking skeleton display a clean grade for a scan that never ran, and
`compute_grade` exists precisely to refuse to grade a scan where nothing was
determined. An all-errored scan therefore completes with no grade, which is the
correct behaviour and is what the UI shows.
"""

import time
from dataclasses import dataclass

from promptproof_core.models import Target
from promptproof_core.probes import Probe
from promptproof_core.settings import Settings

NOT_IMPLEMENTED_REASON = "probe execution not implemented yet (Phase 2)"


@dataclass(frozen=True, slots=True)
class ProbeResult:
    """The outcome of one probe, plus the evidence for it."""

    outcome: str
    response_excerpt: str | None = None
    judge_rationale: str | None = None
    judge_confidence: float | None = None
    error_reason: str | None = None
    target_latency_ms: int | None = None
    judge_latency_ms: int | None = None


async def execute_probe(
    probe: Probe,
    target: Target,
    settings: Settings,
) -> ProbeResult:
    started = time.monotonic()
    # Phase 2 replaces this body with: call the target through the shared provider
    # client, then judge the response against `probe.success_criteria`.
    elapsed_ms = int((time.monotonic() - started) * 1000)
    return ProbeResult(
        outcome="errored",
        error_reason=NOT_IMPLEMENTED_REASON,
        target_latency_ms=elapsed_ms,
    )
