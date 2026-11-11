"""Structured logging.

Every line is JSON, and every line emitted while handling a scan carries the
trace and span ID so logs join to traces and metrics on one identifier
(docs/observability.md).

Correlation is a processor rather than something call sites remember to pass:
`_add_trace_correlation` reads the active span and attaches `trace_id` and
`span_id` to every record. That is the only way the guarantee holds — a rule
saying "always include the trace id" is a rule that gets forgotten in the one
error path you most wanted it on.

Nothing calls `logging.getLogger` directly; everything calls `get_logger`, so
there is no channel that bypasses this.
"""

import logging
import sys
from collections.abc import MutableMapping
from typing import Any

import structlog


def _add_trace_correlation(
    _logger: Any, _method: str, event: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """Attach the active trace and span id to every record.

    Imported lazily: `logging` is the lowest-level module here and importing
    telemetry at module scope would make the two circular.
    """
    try:
        from opentelemetry import trace
    except ImportError:  # pragma: no cover - the dependency is declared
        return event

    context = trace.get_current_span().get_span_context()
    if context.is_valid:
        event["trace_id"] = format(context.trace_id, "032x")
        event["span_id"] = format(context.span_id, "016x")
    return event


def configure_logging(level: str = "INFO", environment: str = "local") -> None:
    """Configure structlog once, at process start.

    Local development gets human-readable output; everywhere else gets JSON, so
    log aggregation does not have to parse prose.
    """
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=getattr(logging, level.upper(), logging.INFO),
    )

    shared: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        _add_trace_correlation,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.UnicodeDecoder(),
    ]

    renderer: Any = (
        structlog.dev.ConsoleRenderer()
        if environment == "local"
        else structlog.processors.JSONRenderer()
    )

    structlog.configure(
        processors=[*shared, structlog.processors.format_exc_info, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    return structlog.stdlib.get_logger(name)


def bind_scan(scan_id: str) -> None:
    """Bind the scan ID for the duration of this task's context.

    Every log line emitted downstream then carries it without being passed it.
    """
    structlog.contextvars.bind_contextvars(scan_id=scan_id)


def clear_context() -> None:
    structlog.contextvars.clear_contextvars()
