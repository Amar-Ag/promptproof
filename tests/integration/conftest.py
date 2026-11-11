"""Integration fixtures.

These tests run against a real Postgres, because the behaviour under test —
`FOR UPDATE SKIP LOCKED`, lease expiry, unique-constraint races — does not exist
in a fake. No model provider is ever called from here.

The test database is a separate database alongside the development one, created
on demand, so running the suite never touches data you were looking at.

Every fixture here is function-scoped and the engine uses `NullPool`. That is
deliberate: pytest-asyncio gives each test its own event loop, and a pooled
connection created on one loop cannot be reused on another. `NullPool` opens a
fresh connection per use, so a module-level engine stays safe across loops
without any loop-scope configuration to get wrong.
"""

import os
from collections.abc import AsyncIterator, Iterator

import pytest
import pytest_asyncio
from promptproof_core.models import Base
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

DEFAULT_URL = "postgresql+asyncpg://promptproof:promptproof@localhost:5433/promptproof"

_engine: AsyncEngine | None = None
_schema_ready = False


def _base_url() -> str:
    return os.environ.get("DATABASE_URL", DEFAULT_URL)


def _test_url() -> str:
    base, _, name = _base_url().rpartition("/")
    return f"{base}/{name}_test"


async def _create_test_database_if_missing() -> None:
    name = _test_url().rpartition("/")[2]
    admin = create_async_engine(_base_url(), isolation_level="AUTOCOMMIT", poolclass=NullPool)
    try:
        async with admin.connect() as connection:
            found = (
                await connection.execute(
                    text("SELECT 1 FROM pg_database WHERE datname = :name"), {"name": name}
                )
            ).first()
            if found is None:
                # A database name cannot be a bind parameter, so it is quoted as
                # an identifier instead. `name` is derived from DATABASE_URL, not
                # from anything a test supplies.
                await connection.exec_driver_sql(f'CREATE DATABASE "{name}"')
    finally:
        await admin.dispose()


@pytest_asyncio.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    global _engine, _schema_ready

    if _engine is None:
        await _create_test_database_if_missing()
        _engine = create_async_engine(_test_url(), poolclass=NullPool)

    if not _schema_ready:
        async with _engine.begin() as connection:
            # Built from the models rather than by running Alembic, to keep the
            # suite fast. Migration/model parity is a separate CI check: an
            # autogenerate run must produce an empty diff.
            await connection.run_sync(Base.metadata.drop_all)
            await connection.run_sync(Base.metadata.create_all)
        _schema_ready = True

    yield _engine


@pytest_asyncio.fixture
async def clean(engine: AsyncEngine) -> AsyncIterator[None]:
    """Every test starts from an empty database."""
    async with engine.begin() as connection:
        await connection.exec_driver_sql(
            "TRUNCATE findings, scans, targets, target_families CASCADE"
        )
    yield


@pytest_asyncio.fixture
async def session(engine: AsyncEngine, clean: None) -> AsyncIterator[AsyncSession]:
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s
        await s.rollback()


@pytest_asyncio.fixture
async def sessions(
    engine: AsyncEngine, clean: None
) -> AsyncIterator[tuple[AsyncSession, AsyncSession]]:
    """Two independent sessions on separate connections, for the concurrency tests.

    They must be separate connections, not two sessions sharing one: `SKIP
    LOCKED` only skips rows locked by a *different* transaction.
    """
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as a, maker() as b:
        yield a, b
        await a.rollback()
        await b.rollback()


# ── Telemetry capture ────────────────────────────────────────────────────────
# Installed once, at import, and shared by every test that inspects spans or
# metrics. OpenTelemetry refuses to replace a provider after it is set, so the
# private one-shot guards are reset first. That is deliberate use of private
# API in tests: the alternative is threading providers through every call site
# for the benefit of tests alone, which would make the production code worse.

from opentelemetry import trace as _trace_api  # noqa: E402
from opentelemetry.metrics import _internal as _metrics_internal  # noqa: E402
from opentelemetry.sdk.metrics import MeterProvider  # noqa: E402
from opentelemetry.sdk.metrics.export import InMemoryMetricReader  # noqa: E402
from opentelemetry.sdk.trace import TracerProvider  # noqa: E402
from opentelemetry.sdk.trace.export import SimpleSpanProcessor  # noqa: E402
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (  # noqa: E402
    InMemorySpanExporter,
)

SPAN_EXPORTER = InMemorySpanExporter()
METRIC_READER = InMemoryMetricReader()


def _install_telemetry() -> None:
    _trace_api._TRACER_PROVIDER = None
    _trace_api._TRACER_PROVIDER_SET_ONCE._done = False
    tracer_provider = TracerProvider()
    tracer_provider.add_span_processor(SimpleSpanProcessor(SPAN_EXPORTER))
    _trace_api.set_tracer_provider(tracer_provider)

    # The metrics guards live on the _internal module, unlike tracing's,
    # and set_meter_provider is re-exported from there too.
    _metrics_internal._METER_PROVIDER = None
    _metrics_internal._METER_PROVIDER_SET_ONCE._done = False
    _metrics_internal.set_meter_provider(MeterProvider(metric_readers=[METRIC_READER]))


_install_telemetry()


@pytest.fixture(autouse=True)
def captured_spans() -> Iterator[InMemorySpanExporter]:
    """A clean span exporter per test."""
    SPAN_EXPORTER.clear()
    yield SPAN_EXPORTER
    SPAN_EXPORTER.clear()


@pytest.fixture
def metric_reader() -> InMemoryMetricReader:
    return METRIC_READER
