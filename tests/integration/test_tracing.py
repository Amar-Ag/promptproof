"""Trace context has to survive the queue.

This is the property the observability criterion turns on, and the one that a
single-process design would never have had to solve. The API opens a span,
serialises its context onto the scan row, and minutes later another process
picks it up and continues the same trace.

Asserted against spans a real exporter received, not against the code's
intentions.
"""

import uuid
from collections.abc import Iterator

import pytest
from opentelemetry import trace as trace_api
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from promptproof_api.routers.scans import submit_scan
from promptproof_api.schemas import ScanCreate
from promptproof_core.models import Scan, Target, TargetFamily
from promptproof_core.queue import claim_next_scan
from promptproof_core.settings import Settings
from promptproof_worker.main import _process
from sqlalchemy.ext.asyncio import AsyncSession

from tests.stubs import StubModelClient, verdict_json

EXPORTER = InMemorySpanExporter()


def _install_provider() -> None:
    """Install an in-memory tracer provider for the whole test session.

    OpenTelemetry refuses to replace a provider once set, so the private
    one-shot guard is reset first. That is deliberate use of private API in a
    test: the alternative is threading a provider through every call site for
    the benefit of tests alone, which would make the production code worse.
    """
    trace_api._TRACER_PROVIDER = None
    trace_api._TRACER_PROVIDER_SET_ONCE._done = False
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(EXPORTER))
    trace_api.set_tracer_provider(provider)


_install_provider()


@pytest.fixture(autouse=True)
def _clear_spans() -> Iterator[None]:
    EXPORTER.clear()
    yield
    EXPORTER.clear()


def spans_by_name() -> dict[str, list[object]]:
    out: dict[str, list[object]] = {}
    for s in EXPORTER.get_finished_spans():
        out.setdefault(s.name, []).append(s)
    return out


def settings() -> Settings:
    return Settings(
        default_target_model="qwen/qwen3.8-27b",
        judge_model="openai/gpt-oss-120b",
        groq_api_key="gsk_test_key_abcdefghijklmnop",
        scan_concurrency=2,
    )


async def make_target(session: AsyncSession) -> Target:
    family = TargetFamily(name="Traced target")
    session.add(family)
    await session.flush()
    target = Target(
        family_id=family.id,
        version=1,
        kind="prompt",
        model="qwen/qwen3.8-27b",
        system_prompt="You only discuss cheese.",
    )
    session.add(target)
    await session.commit()
    return target


def stub_client(cfg: Settings) -> StubModelClient:
    def responder(model: str, system: str, user: str) -> str:
        if model == cfg.judge_model:
            return verdict_json(False, "stub verdict")
        return "I only discuss cheese."

    return StubModelClient(responder=responder)


class TestSubmission:
    async def test_submitting_records_a_traceparent(self, session: AsyncSession) -> None:
        target = await make_target(session)

        created = await submit_scan(ScanCreate(target_id=target.id, suites=["tool-abuse"]), session)
        await session.commit()

        scan = await session.get(Scan, created.id)
        assert scan is not None
        assert scan.traceparent is not None
        # W3C format: version-traceid-spanid-flags
        parts = scan.traceparent.split("-")
        assert len(parts) == 4
        assert len(parts[1]) == 32
        assert len(parts[2]) == 16

    async def test_the_submit_span_carries_identifiers_not_content(
        self, session: AsyncSession
    ) -> None:
        target = await make_target(session)
        await submit_scan(ScanCreate(target_id=target.id, suites=["tool-abuse"]), session)
        await session.commit()

        submit = spans_by_name()["scan.submit"][0]
        attrs = dict(submit.attributes or {})  # type: ignore[attr-defined]

        assert attrs["scan.id"]
        assert attrs["target.kind"] == "prompt"
        assert attrs["probe.count"] >= 1

        # The system prompt must not have travelled with it.
        assert "You only discuss cheese." not in str(attrs)
        assert not any("prompt" in k and k != "probe.count" for k in attrs)


class TestTheJoin:
    async def test_worker_spans_share_the_submitting_trace_id(self, session: AsyncSession) -> None:
        """The whole point: one trace across two processes."""
        cfg = settings()
        target = await make_target(session)

        await submit_scan(ScanCreate(target_id=target.id, suites=["tool-abuse"]), session)
        await session.commit()

        claimed = await claim_next_scan(
            session, worker_id="test", lease_seconds=300, max_attempts=3
        )
        assert claimed is not None
        await session.commit()

        from promptproof_core.telemetry import context_from_traceparent

        parent = context_from_traceparent(claimed.traceparent)
        await _process(session, claimed, cfg, stub_client(cfg), parent)

        found = spans_by_name()
        submit_trace = found["scan.submit"][0].context.trace_id  # type: ignore[attr-defined]

        for name in ("scan.execute", "probe.execute", "target.invoke", "judge.evaluate"):
            assert name in found, f"no {name} span was recorded"
            for s in found[name]:
                assert s.context.trace_id == submit_trace, (  # type: ignore[attr-defined]
                    f"{name} is on a different trace from scan.submit — "
                    "the context did not survive the queue"
                )

    async def test_a_scan_with_no_traceparent_still_runs(self, session: AsyncSession) -> None:
        """Scans written before tracing existed must not break the worker."""
        cfg = settings()
        target = await make_target(session)
        scan = Scan(
            target_id=target.id,
            suites=["tool-abuse"],
            status="pending",
            probe_count=1,
            traceparent=None,
        )
        session.add(scan)
        await session.commit()

        claimed = await claim_next_scan(
            session, worker_id="test", lease_seconds=300, max_attempts=3
        )
        assert claimed is not None
        await session.commit()

        from promptproof_core.telemetry import context_from_traceparent

        await _process(session, claimed, cfg, stub_client(cfg), context_from_traceparent(None))

        refreshed = await session.get(Scan, scan.id, populate_existing=True)
        assert refreshed is not None
        assert refreshed.status == "completed"

    async def test_the_queue_wait_is_measured(self, session: AsyncSession) -> None:
        """A gap between two spans explains nothing; a number does."""
        from promptproof_core.telemetry import context_from_traceparent, span

        target = await make_target(session)
        created = await submit_scan(ScanCreate(target_id=target.id, suites=["tool-abuse"]), session)
        await session.commit()

        scan = await session.get(Scan, created.id)
        assert scan is not None
        with span(
            "scan.claim",
            parent=context_from_traceparent(scan.traceparent),
            attributes={"queue.wait_ms": 1234, "worker.id": "test"},
        ):
            pass

        claim = spans_by_name()["scan.claim"][0]
        attrs = dict(claim.attributes or {})  # type: ignore[attr-defined]
        assert attrs["queue.wait_ms"] == 1234
        assert claim.context.trace_id == spans_by_name()["scan.submit"][0].context.trace_id  # type: ignore[attr-defined]


class TestResilience:
    async def test_an_unparseable_traceparent_does_not_break_anything(self) -> None:
        from promptproof_core.telemetry import context_from_traceparent, span

        # Garbage in the column must start a fresh trace, not raise.
        parent = context_from_traceparent("not-a-traceparent")
        with span("scan.execute", parent=parent):
            pass

        assert spans_by_name()["scan.execute"]

    async def test_probe_outcomes_are_recorded_on_their_spans(self, session: AsyncSession) -> None:
        cfg = settings()
        target = await make_target(session)
        await submit_scan(ScanCreate(target_id=target.id, suites=["tool-abuse"]), session)
        await session.commit()

        claimed = await claim_next_scan(
            session, worker_id="test", lease_seconds=300, max_attempts=3
        )
        assert claimed is not None
        await session.commit()

        from promptproof_core.telemetry import context_from_traceparent

        await _process(
            session,
            claimed,
            cfg,
            stub_client(cfg),
            context_from_traceparent(claimed.traceparent),
        )

        outcomes = {
            dict(s.attributes or {}).get("finding.outcome")  # type: ignore[attr-defined]
            for s in spans_by_name()["probe.execute"]
        }
        assert outcomes <= {"exploited", "resisted", "errored"}
        assert outcomes

    async def test_unknown_target_id_is_a_clean_404(self, session: AsyncSession) -> None:
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as caught:
            await submit_scan(ScanCreate(target_id=uuid.uuid4(), suites=["tool-abuse"]), session)
        assert caught.value.status_code == 404
