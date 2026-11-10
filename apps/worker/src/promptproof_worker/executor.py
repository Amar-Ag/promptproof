"""Running one probe against one target, and judging the result.

Two model calls per probe: the target, then the judge. Either can fail, and
neither failure may take the scan down with it — a probe that cannot be resolved
becomes an `errored` finding and the scan carries on (R-002-4).

`errored` is a third outcome, not a quiet pass. `compute_grade` excludes errored
findings and refuses to grade a scan where nothing was determined, so a
rate-limited run cannot come out looking clean.
"""

import time
from dataclasses import dataclass

from promptproof_core.judge import JudgeError, evaluate
from promptproof_core.logging import get_logger
from promptproof_core.models import Target
from promptproof_core.probes import Probe
from promptproof_core.provider import ModelClient, ProviderError
from promptproof_core.scrub import excerpt
from promptproof_core.settings import Settings

log = get_logger(__name__)

# Enough to justify a verdict to a reviewer, bounded so one runaway generation
# cannot dominate a report or a response payload (R-002-5).
MAX_EXCERPT = 2_000

ENDPOINT_UNSUPPORTED = "endpoint targets are not supported yet (issue #17)"


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

    @classmethod
    def errored(
        cls, reason: str, *, target_latency_ms: int | None = None, response: str | None = None
    ) -> "ProbeResult":
        return cls(
            outcome="errored",
            error_reason=reason,
            target_latency_ms=target_latency_ms,
            response_excerpt=excerpt(response, MAX_EXCERPT),
        )


async def execute_probe(
    probe: Probe,
    target: Target,
    settings: Settings,
    client: ModelClient,
) -> ProbeResult:
    if target.kind != "prompt":
        return ProbeResult.errored(ENDPOINT_UNSUPPORTED)

    # ── Call the target ──────────────────────────────────────────────────────
    started = time.monotonic()
    try:
        completion = await client.complete(
            model=target.model or settings.default_target_model,
            system=target.system_prompt or "",
            user=probe.payload,
        )
    except ProviderError as exc:
        elapsed = int((time.monotonic() - started) * 1000)
        log.warning(
            "target.errored",
            probe_id=probe.id,
            suite=probe.suite,
            reason=exc.reason,
            transient=exc.transient,
        )
        return ProbeResult.errored(exc.reason, target_latency_ms=elapsed)

    # ── Judge the response ───────────────────────────────────────────────────
    try:
        verdict = await evaluate(
            client,
            judge_model=settings.judge_model,
            probe=probe,
            response_text=completion.text,
        )
    except JudgeError as exc:
        log.warning("judge.errored", probe_id=probe.id, suite=probe.suite, reason=exc.reason)
        # The response is kept as evidence even though no verdict was reached:
        # a human can still read what the target said.
        return ProbeResult.errored(
            exc.reason,
            target_latency_ms=completion.latency_ms,
            response=completion.text,
        )

    log.info(
        "probe.resolved",
        probe_id=probe.id,
        suite=probe.suite,
        severity=probe.severity,
        outcome=verdict.outcome,
        confidence=verdict.confidence,
        # Lengths and latencies, never the text itself.
        response_length=len(completion.text),
        target_latency_ms=completion.latency_ms,
        judge_latency_ms=verdict.latency_ms,
    )

    return ProbeResult(
        outcome=verdict.outcome,
        response_excerpt=excerpt(completion.text, MAX_EXCERPT),
        judge_rationale=verdict.rationale,
        judge_confidence=verdict.confidence,
        target_latency_ms=completion.latency_ms,
        judge_latency_ms=verdict.latency_ms,
    )
