"""A whole scan, through the real worker, against a real database.

The provider is stubbed — no test here touches the network — but everything
else is the production path: claim, execute, judge, write findings, grade,
complete.
"""

from datetime import UTC, datetime, timedelta

from promptproof_core.models import Finding, Scan, Target, TargetFamily
from promptproof_core.probes import get_corpus
from promptproof_core.provider import ProviderRateLimited
from promptproof_core.queue import claim_next_scan
from promptproof_core.settings import Settings
from promptproof_worker.main import _process
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tests.stubs import StubModelClient, verdict_json

# Derived, not frozen: the corpus grows, and a hardcoded count would make
# every one of these tests fail the next time a probe is added.
ALL_SUITES = [
    "direct-injection",
    "jailbreak",
    "system-prompt-leak",
    "data-exfiltration",
    "indirect-injection",
    "tool-abuse",
]

# How many probes a full-suite scan runs, read from the corpus itself.
TOTAL_PROBES = len(get_corpus().probes)


def settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "default_target_model": "qwen/qwen3.8-27b",
        "judge_model": "openai/gpt-oss-120b",
        "groq_api_key": "gsk_test_key_abcdefghijklmnop",
        "scan_concurrency": 4,
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def client_exploiting(probe_ids: set[str], judge_model: str) -> StubModelClient:
    """Target replies are tagged with the probe id so the judge stub can route."""

    def responder(model: str, system: str, user: str) -> str:
        if model == judge_model:
            exploited = any(f"<<{pid}>>" in user for pid in probe_ids)
            return verdict_json(exploited, "stub verdict")
        probe = next(p for p in get_corpus().probes if p.payload.strip() == user.strip())
        return f"reply for <<{probe.id}>>"

    return StubModelClient(responder=responder)


async def make_scan(session: AsyncSession, suites: list[str] | None = None) -> Scan:
    family = TargetFamily(name="Cheese support bot")
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
    await session.flush()

    chosen = suites or ALL_SUITES
    scan = Scan(
        target_id=target.id,
        suites=chosen,
        status="pending",
        probe_count=len(get_corpus().for_suites(chosen)),
    )
    session.add(scan)
    await session.commit()
    return scan


async def run(session: AsyncSession, scan: Scan, client: StubModelClient, cfg: Settings) -> Scan:
    claimed = await claim_next_scan(session, worker_id="test", lease_seconds=300, max_attempts=3)
    assert claimed is not None and claimed.id == scan.id
    await session.commit()
    await _process(session, claimed, cfg, client)
    refreshed = await session.get(Scan, scan.id, populate_existing=True)
    assert refreshed is not None
    return refreshed


async def findings_of(session: AsyncSession, scan: Scan) -> list[Finding]:
    return list((await session.scalars(select(Finding).where(Finding.scan_id == scan.id))).all())


class TestCompletion:
    async def test_a_clean_target_completes_with_an_a(self, session: AsyncSession) -> None:
        cfg = settings()
        scan = await make_scan(session)
        done = await run(session, scan, client_exploiting(set(), cfg.judge_model), cfg)

        assert done.status == "completed"
        assert done.grade == "A"
        assert done.finished_at is not None
        assert done.judge_model == cfg.judge_model
        assert done.counts is not None
        assert done.counts["total"] == TOTAL_PROBES
        assert done.counts["errored"] == 0

    async def test_one_finding_per_probe(self, session: AsyncSession) -> None:
        cfg = settings()
        scan = await make_scan(session)
        await run(session, scan, client_exploiting(set(), cfg.judge_model), cfg)

        found = await findings_of(session, scan)
        assert len(found) == TOTAL_PROBES
        assert {f.probe_id for f in found} == {p.id for p in get_corpus().probes}
        assert all(f.judge_rationale == "stub verdict" for f in found)
        assert all(f.response_excerpt for f in found)

    async def test_a_critical_exploit_grades_f(self, session: AsyncSession) -> None:
        cfg = settings()
        scan = await make_scan(session)
        done = await run(session, scan, client_exploiting({"spl-001"}, cfg.judge_model), cfg)

        assert done.grade == "F"
        assert done.counts is not None
        assert done.counts["exploited"]["critical"] == 1

    async def test_two_high_exploits_grade_d(self, session: AsyncSession) -> None:
        cfg = settings()
        scan = await make_scan(session, ["direct-injection", "data-exfiltration", "jailbreak"])
        done = await run(
            session, scan, client_exploiting({"di-001", "dx-001"}, cfg.judge_model), cfg
        )

        assert done.grade == "D"

    async def test_one_high_exploit_grades_c(self, session: AsyncSession) -> None:
        cfg = settings()
        scan = await make_scan(session, ["direct-injection", "jailbreak"])
        done = await run(session, scan, client_exploiting({"di-001"}, cfg.judge_model), cfg)

        assert done.grade == "C"


class TestFailureHandling:
    async def test_a_provider_outage_completes_without_a_grade(self, session: AsyncSession) -> None:
        """The property that stops a broken scan looking like a clean one."""
        cfg = settings()
        scan = await make_scan(session)

        def responder(model: str, system: str, user: str) -> str:
            raise ProviderRateLimited("HTTP 429 rate limited")

        done = await run(session, scan, StubModelClient(responder=responder), cfg)

        assert done.status == "completed"
        assert done.grade is None
        assert done.counts is not None
        assert done.counts["errored"] == TOTAL_PROBES
        assert done.counts["total"] == TOTAL_PROBES

    async def test_one_failing_probe_does_not_fail_the_scan(self, session: AsyncSession) -> None:
        cfg = settings()
        scan = await make_scan(session)

        def responder(model: str, system: str, user: str) -> str:
            if model == cfg.judge_model:
                return verdict_json(False, "stub verdict")
            if "badge" in user:  # the data-exfiltration probe
                raise ProviderRateLimited("HTTP 429 rate limited")
            return "I only discuss cheese."

        done = await run(session, scan, StubModelClient(responder=responder), cfg)

        assert done.status == "completed"
        assert done.grade == "A"
        assert done.counts is not None
        assert done.counts["errored"] == 1

        errored = [f for f in await findings_of(session, scan) if f.outcome == "errored"]
        assert len(errored) == 1
        assert errored[0].suite == "data-exfiltration"
        assert errored[0].error_reason is not None


class TestResumption:
    async def test_a_reclaimed_scan_resumes_without_duplicating_findings(
        self, session: AsyncSession
    ) -> None:
        """A worker died halfway. The replacement finishes the job, once."""
        cfg = settings()
        scan = await make_scan(session)

        # Pretend a previous attempt resolved two probes before dying.
        already = sorted(p.id for p in get_corpus().probes)[:2]
        for probe_id in already:
            probe = next(p for p in get_corpus().probes if p.id == probe_id)
            session.add(
                Finding(
                    scan_id=scan.id,
                    probe_id=probe.id,
                    suite=probe.suite,
                    severity=probe.severity,
                    outcome="resisted",
                    judge_rationale="from the first attempt",
                )
            )
        scan.status = "running"
        scan.attempts = 1
        scan.locked_at = datetime.now(UTC) - timedelta(seconds=400)
        await session.commit()

        client = client_exploiting(set(), cfg.judge_model)
        done = await run(session, scan, client, cfg)

        found = await findings_of(session, scan)
        assert len(found) == TOTAL_PROBES
        assert done.status == "completed"

        # The already-resolved probes were not re-run and kept their evidence.
        preserved = [f for f in found if f.probe_id in already]
        assert all(f.judge_rationale == "from the first attempt" for f in preserved)

        # Only the remaining four probes were sent to the target.
        target_calls = [c for c in client.calls if c["model"] != cfg.judge_model]
        assert len(target_calls) == TOTAL_PROBES - len(already)


class TestConcurrency:
    async def test_probes_in_flight_are_bounded(self, session: AsyncSession) -> None:
        """Unbounded fan-out is how a scan rate-limits itself into all-errored."""
        cfg = settings(scan_concurrency=2)
        scan = await make_scan(session)
        client = client_exploiting(set(), cfg.judge_model)

        await run(session, scan, client, cfg)

        assert client.peak_in_flight <= 2
        assert client.peak_in_flight > 0
