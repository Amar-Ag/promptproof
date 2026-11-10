"""Structured logging.

Every line is JSON, and every line emitted while handling a scan carries the
trace and span ID so logs join to traces and metrics on one identifier
(docs/observability.md).

The OpenTelemetry processor that injects those IDs is added in Phase 5. The
contract is established here so no module logs through a channel that bypasses
it: nothing calls `logging.getLogger` directly, everything calls `get_logger`.
"""

import logging
import sys
from typing import Any

import structlog


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
