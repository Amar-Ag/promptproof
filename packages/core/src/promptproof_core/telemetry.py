"""Tracing.

One trace per scan, spanning two processes. The API opens `scan.submit` and
persists the W3C trace context on the scan row; the worker reads it back when it
claims the row and continues the same trace. That join is the whole point —
without it the queue is an unexplained gap and the two halves of a scan look
like unrelated work.

Span and attribute names are defined in docs/observability.md, not invented here.

Three rules this module exists to enforce:

* **Telemetry never breaks the application.** Every exporter failure is
  swallowed. A scan must not fail because a collector is down.
* **No content leaves in a span.** Attributes are identifiers, counts,
  latencies, outcomes and model names. The scrubber guards what slips through
  an exception message; this module simply never passes content in.
* **It is optional.** With nothing configured it installs a no-op provider and
  the application behaves identically, which is what local development and the
  test suite run against.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.propagate import extract, inject
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import Span, Status, StatusCode

from .logging import get_logger
from .settings import Settings

log = get_logger(__name__)

# Pydantic Logfire speaks OTLP, so it needs no vendor SDK — only an endpoint and
# a token. Anything else that speaks OTLP works by setting the endpoint instead.
LOGFIRE_ENDPOINT = "https://logfire-api.pydantic.dev"

_configured = False


def configure_telemetry(settings: Settings, service_name: str) -> None:
    """Install a tracer provider. Safe to call twice; safe to call never."""
    global _configured
    if _configured:
        return
    _configured = True

    resource = Resource.create(
        {
            "service.name": service_name,
            "service.namespace": settings.otel_service_name,
            "deployment.environment.name": settings.environment,
        }
    )
    provider = TracerProvider(resource=resource)

    exporter = _build_exporter(settings)
    if exporter is not None:
        provider.add_span_processor(BatchSpanProcessor(exporter))
        log.info("telemetry.enabled", service=service_name, environment=settings.environment)
    else:
        # No exporter: spans are still created and context still propagates, so
        # the cross-process join is exercised in tests without a collector.
        log.info("telemetry.local_only", service=service_name)

    trace.set_tracer_provider(provider)


def _build_exporter(settings: Settings) -> Any:
    try:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    except ImportError:  # pragma: no cover - the dependency is declared
        return None

    if settings.logfire_token:
        return OTLPSpanExporter(
            endpoint=f"{LOGFIRE_ENDPOINT}/v1/traces",
            headers={"Authorization": settings.logfire_token},
        )
    if settings.otel_exporter_otlp_endpoint:
        return OTLPSpanExporter(
            endpoint=f"{settings.otel_exporter_otlp_endpoint.rstrip('/')}/v1/traces"
        )
    return None


def get_tracer(name: str) -> trace.Tracer:
    return trace.get_tracer(name)


# ── Carrying context across the queue ────────────────────────────────────────


def current_traceparent() -> str | None:
    """Serialise the active span context into a W3C `traceparent`.

    Stored on the scan row so the worker can continue this trace minutes later,
    in another process. Returns None when nothing is being traced.
    """
    carrier: dict[str, str] = {}
    inject(carrier)
    return carrier.get("traceparent")


def context_from_traceparent(traceparent: str | None) -> Context | None:
    """Rebuild the submitting request's context from a stored `traceparent`.

    A scan written before tracing existed has no traceparent; it simply starts
    a new trace rather than failing.
    """
    if not traceparent:
        return None
    return extract({"traceparent": traceparent})


@contextmanager
def span(
    name: str,
    *,
    parent: Context | None = None,
    attributes: dict[str, Any] | None = None,
) -> Iterator[Span]:
    """Start a span, recording failures on it without swallowing them.

    An exception is marked on the span and re-raised: the caller decides what a
    failure means, and a probe failing is not the same as a scan failing.
    """
    tracer = get_tracer("promptproof")
    with tracer.start_as_current_span(name, context=parent, attributes=attributes or {}) as current:
        try:
            yield current
        except Exception as exc:
            current.set_status(Status(StatusCode.ERROR, type(exc).__name__))
            # record_exception stores the message, which can carry content, so
            # only the type is kept.
            current.set_attribute("error.type", type(exc).__name__)
            raise


def set_attributes(**attributes: Any) -> None:
    """Add attributes to the active span, if there is one.

    Only identifiers, counts, latencies, outcomes and model names belong here.
    Never prompt text, response text or a rationale.
    """
    current = trace.get_current_span()
    if current.is_recording():
        for key, value in attributes.items():
            if value is not None:
                current.set_attribute(key, value)


def current_trace_id() -> str | None:
    """The active trace ID as hex, for correlating a log line to its trace."""
    context = trace.get_current_span().get_span_context()
    if not context.is_valid:
        return None
    return format(context.trace_id, "032x")


def shutdown_telemetry() -> None:
    """Flush pending spans at exit. Failure here is never worth a crash."""
    provider = trace.get_tracer_provider()
    shutdown = getattr(provider, "shutdown", None)
    if shutdown is None:
        return
    try:
        shutdown()
    except Exception as exc:  # pragma: no cover - best effort by design
        log.warning("telemetry.shutdown_failed", reason=type(exc).__name__)
