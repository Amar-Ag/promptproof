"""Metrics.

The instrument set is defined in docs/observability.md and exists to answer one
question: *is a user waiting longer than they should, and where is the time
going?* Anything that does not help answer it is not here.

**Every label in this module is bounded.** A metric label with unbounded values
— a probe payload, a provider's error text, a scan id — creates a new time
series per distinct value, which is how a metrics backend falls over and how
user content leaks into a place nobody thinks to check. Error reasons are
therefore mapped onto a fixed vocabulary before they are ever used as a label.
"""

from collections.abc import Callable
from typing import Any

from opentelemetry import metrics
from opentelemetry.metrics import CallbackOptions, Observation
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource

from .logging import get_logger
from .settings import Settings

log = get_logger(__name__)

# The only values that may appear in a `reason` label. Anything else becomes
# "other" rather than growing the cardinality of the series.
REASON_LABELS = frozenset(
    {"rate_limited", "timeout", "unavailable", "rejected", "parse", "guard", "other"}
)

_meter: metrics.Meter | None = None
_instruments: dict[str, Any] = {}
_queue_snapshot: Callable[[], tuple[int, int, float]] | None = None


def reason_label(reason: str | None) -> str:
    """Collapse a free-text failure reason onto the fixed label vocabulary.

    Provider errors arrive as prose that varies per call and can echo the
    request. Using it as a label would be both a cardinality explosion and a
    content leak, so it is classified instead.
    """
    if not reason:
        return "other"
    lowered = reason.lower()
    if "429" in lowered or "rate limit" in lowered:
        return "rate_limited"
    if "timeout" in lowered or "timed out" in lowered:
        return "timeout"
    if "unparseable" in lowered or "json" in lowered or "rationale" in lowered:
        return "parse"
    if "guard" in lowered or "ssrf" in lowered or "refused" in lowered:
        return "guard"
    if any(code in lowered for code in ("500", "502", "503", "504", "unavailable", "connect")):
        return "unavailable"
    if "http 4" in lowered or "rejected" in lowered:
        return "rejected"
    return "other"


def configure_metrics(settings: Settings, service_name: str) -> None:
    """Install a meter provider and create the instruments. Safe to call twice."""
    global _meter
    if _meter is not None:
        return

    resource = Resource.create(
        {
            "service.name": service_name,
            "service.namespace": settings.otel_service_name,
            "deployment.environment.name": settings.environment,
        }
    )

    readers = []
    exporter = _build_exporter(settings)
    if exporter is not None:
        readers.append(PeriodicExportingMetricReader(exporter, export_interval_millis=30_000))

    provider = MeterProvider(resource=resource, metric_readers=readers)
    metrics.set_meter_provider(provider)
    _meter = metrics.get_meter("promptproof")

    _instruments["scans_submitted"] = _meter.create_counter(
        "promptproof.scans.submitted", unit="1", description="Scans accepted for execution"
    )
    _instruments["scans_completed"] = _meter.create_counter(
        "promptproof.scans.completed", unit="1", description="Scans reaching a terminal state"
    )
    _instruments["scan_duration"] = _meter.create_histogram(
        "promptproof.scan.duration", unit="ms", description="Submission to completion"
    )
    _instruments["probe_duration"] = _meter.create_histogram(
        "promptproof.probe.duration", unit="ms", description="One probe, target plus judge"
    )
    _instruments["target_errors"] = _meter.create_counter(
        "promptproof.target.errors", unit="1", description="Failed calls to a target"
    )
    _instruments["judge_errors"] = _meter.create_counter(
        "promptproof.judge.errors", unit="1", description="Failed or unusable judge verdicts"
    )
    _instruments["reclaims"] = _meter.create_counter(
        "promptproof.scan.reclaims", unit="1", description="Scans reclaimed after a lease expired"
    )

    # Observable: read when the exporter collects, rather than pushed.
    _meter.create_observable_gauge(
        "promptproof.queue.depth",
        callbacks=[_observe_depth],
        unit="1",
        description="Scans pending or running",
    )
    _meter.create_observable_gauge(
        "promptproof.queue.oldest_age_seconds",
        callbacks=[_observe_oldest_age],
        unit="s",
        description="Age of the oldest unfinished scan",
    )

    log.info("metrics.configured", service=service_name, exporting=exporter is not None)


def _build_exporter(settings: Settings) -> Any:
    try:
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import (
            OTLPMetricExporter,
        )
    except ImportError:  # pragma: no cover - the dependency is declared
        return None

    if settings.logfire_token:
        return OTLPMetricExporter(
            endpoint="https://logfire-api.pydantic.dev/v1/metrics",
            headers={"Authorization": settings.logfire_token},
        )
    if settings.otel_exporter_otlp_endpoint:
        return OTLPMetricExporter(
            endpoint=f"{settings.otel_exporter_otlp_endpoint.rstrip('/')}/v1/metrics"
        )
    return None


# ── Queue gauges ─────────────────────────────────────────────────────────────


def set_queue_snapshot(provider: Callable[[], tuple[int, int, float]] | None) -> None:
    """Register a source for the queue gauges: (pending, running, oldest_age).

    A callback, not a direct query: OpenTelemetry collects synchronously and the
    queue lives behind an async database session. The worker refreshes a cached
    snapshot on each poll and this reads it, so collection never blocks on I/O
    and a slow database cannot stall the exporter.
    """
    global _queue_snapshot
    _queue_snapshot = provider


def _observe_depth(options: CallbackOptions) -> list[Observation]:
    if _queue_snapshot is None:
        return []
    pending, running, _ = _queue_snapshot()
    return [
        Observation(pending, {"status": "pending"}),
        Observation(running, {"status": "running"}),
    ]


def _observe_oldest_age(options: CallbackOptions) -> list[Observation]:
    if _queue_snapshot is None:
        return []
    _, _, oldest = _queue_snapshot()
    return [Observation(oldest)]


# ── Recording ────────────────────────────────────────────────────────────────


def record_scan_submitted(target_kind: str) -> None:
    if c := _instruments.get("scans_submitted"):
        c.add(1, {"target.kind": target_kind})


def record_scan_completed(status: str, grade: str | None, duration_ms: int) -> None:
    attrs = {"status": status, "grade": grade or "none"}
    if c := _instruments.get("scans_completed"):
        c.add(1, attrs)
    if h := _instruments.get("scan_duration"):
        h.record(duration_ms, {"status": status})


def record_probe(suite: str, outcome: str, duration_ms: int) -> None:
    if h := _instruments.get("probe_duration"):
        h.record(duration_ms, {"suite": suite, "outcome": outcome})


def record_target_error(reason: str | None) -> None:
    if c := _instruments.get("target_errors"):
        c.add(1, {"reason": reason_label(reason)})


def record_judge_error(reason: str | None) -> None:
    if c := _instruments.get("judge_errors"):
        c.add(1, {"reason": reason_label(reason)})


def record_reclaim(reason: str, count: int = 1) -> None:
    if c := _instruments.get("reclaims"):
        c.add(count, {"reason": reason_label(reason)})


def shutdown_metrics() -> None:
    """Flush pending metrics at exit. Never worth a crash."""
    provider = metrics.get_meter_provider()
    shutdown = getattr(provider, "shutdown", None)
    if shutdown is None:
        return
    try:
        shutdown()
    except Exception as exc:  # pragma: no cover - best effort by design
        log.warning("metrics.shutdown_failed", reason=type(exc).__name__)
