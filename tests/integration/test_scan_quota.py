"""What a scan does when the day's model allowance runs out.

The complaint in #32 was a report with a third of its probes `errored`. Every
one of those errors said the same thing, none of them was about the target, and
together they read as findings. The cause was that a spent daily allowance and
a momentary rate limit arrived as the same HTTP 429 and were handled the same
way, so all 32 probes queued up behind an allowance that would not return for
hours and each failed three times on the way.

So the behaviour asserted here is: the first probe to meet a spent allowance
stops the ones that have not started, and the condition is recorded once, on
the scan, rather than once per probe.

A probe-level failure still never fails a scan. This is the single exception,
and it is an exception because the condition is about the account rather than
about anything the target did.
"""

import pytest
from promptproof_core.models import Finding, Scan, Target, TargetFamily
from promptproof_core.probes import get_corpus
from promptproof_core.provider import ProviderQuotaExhausted, ProviderRateLimited
from promptproof_core.queue import claim_next_scan
from promptproof_core.settings import Settings
from promptproof_worker.main import _execute
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tests.stubs import StubModelClient, verdict_json

SUITES = ["direct-injection"]


def settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "default_target_model": "qwen/qwen3.8-27b",
        "judge_model": "openai/gpt-oss-120b",
        "groq_api_key": "gsk_test_key_abcdefghijklmnop",
        "scan_concurrency": 2,
    }
    base.update(overrides)
    return Settings(**base)


async def make_scan(session: AsyncSession) -> Scan:
    family = TargetFamily(name="Quota test target")
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
    probes = get_corpus().for_suites(SUITES)
    scan = Scan(target_id=target.id, suites=SUITES, status="pending", probe_count=len(probes))
    session.add(scan)
    await session.commit()
    return scan


async def run(session: AsyncSession, scan: Scan, client: StubModelClient) -> Scan:
    claimed = await claim_next_scan(session, worker_id="test", lease_seconds=300, max_attempts=3)
    assert claimed is not None and claimed.id == scan.id
    await _execute(session, claimed, settings(), client)
    await session.refresh(claimed)
    return claimed


async def findings_of(session: AsyncSession, scan: Scan) -> list[Finding]:
    return list((await session.scalars(select(Finding).where(Finding.scan_id == scan.id))).all())


def exhausted_from_the_start() -> StubModelClient:
    """The case from the issue: a re-run that errored on its very first probe."""

    def responder(model: str, system: str, user: str) -> str:
        raise ProviderQuotaExhausted("provider daily allowance exhausted")

    return StubModelClient(responder=responder)


def exhausted_after(n_calls: int, judge_model: str) -> StubModelClient:
    """Serves `n_calls` model calls, then reports the allowance spent."""
    served = 0

    def responder(model: str, system: str, user: str) -> str:
        nonlocal served
        served += 1
        if served > n_calls:
            raise ProviderQuotaExhausted("provider daily allowance exhausted")
        if model == judge_model:
            return verdict_json(False, "stub verdict")
        return "I can only discuss cheese."

    return StubModelClient(responder=responder)


class TestNothingUsable:
    async def test_the_scan_fails_once_instead_of_erroring_every_probe(
        self, session: AsyncSession
    ) -> None:
        scan = await make_scan(session)
        corpus_size = len(get_corpus().for_suites(SUITES))
        assert corpus_size > 1, "this test needs a suite with several probes"

        done = await run(session, scan, exhausted_from_the_start())

        assert done.status == "failed"
        assert done.error is not None
        assert "allowance" in done.error

    async def test_it_records_no_findings_at_all(self, session: AsyncSession) -> None:
        """An unattempted probe is absent from the report, not present as a
        failure it never had. N identical errored findings is the thing #32
        was about."""
        scan = await make_scan(session)
        done = await run(session, scan, exhausted_from_the_start())
        assert await findings_of(session, done) == []

    async def test_it_stops_calling_the_provider(self, session: AsyncSession) -> None:
        """Each futile attempt spends from the requests-per-day allowance as
        well, so the remaining probes must not be tried."""
        scan = await make_scan(session)
        client = exhausted_from_the_start()
        corpus_size = len(get_corpus().for_suites(SUITES))

        await run(session, scan, client)

        assert len(client.calls) < corpus_size, (
            f"made {len(client.calls)} calls for {corpus_size} probes after the "
            "allowance was already spent"
        )

    async def test_the_scan_carries_no_grade(self, session: AsyncSession) -> None:
        """A grade derived from nothing would be a fabricated verdict."""
        scan = await make_scan(session)
        done = await run(session, scan, exhausted_from_the_start())
        assert done.grade is None


class TestPartialResults:
    async def test_findings_already_gathered_are_kept(self, session: AsyncSession) -> None:
        """They are genuine results about the target and worth showing."""
        scan = await make_scan(session)
        done = await run(session, scan, exhausted_after(4, "openai/gpt-oss-120b"))

        findings = await findings_of(session, done)
        assert findings, "threw away results it had already obtained"
        assert done.status == "completed"

    async def test_the_caveat_is_recorded_on_the_scan(self, session: AsyncSession) -> None:
        """So a reader can tell "this target passed everything" from "this is
        as far as the day's allowance got"."""
        scan = await make_scan(session)
        done = await run(session, scan, exhausted_after(4, "openai/gpt-oss-120b"))

        assert done.error is not None
        assert "allowance" in done.error

    async def test_the_unreached_probes_are_not_recorded_as_errors(
        self, session: AsyncSession
    ) -> None:
        scan = await make_scan(session)
        done = await run(session, scan, exhausted_after(4, "openai/gpt-oss-120b"))

        findings = await findings_of(session, done)
        errored = [f for f in findings if f.outcome == "errored"]
        assert not errored, "probes that were never attempted were recorded as errored findings"


class TestTheOtherCase:
    async def test_a_momentary_rate_limit_still_only_errors_its_own_probe(
        self, session: AsyncSession
    ) -> None:
        """The distinction that matters. A per-minute limit is transient: the
        probe that met it errors, the scan carries on, and the scan-level
        condition is not raised."""
        judge = "openai/gpt-oss-120b"
        served = 0

        def responder(model: str, system: str, user: str) -> str:
            nonlocal served
            served += 1
            if served == 3:
                raise ProviderRateLimited("HTTP 429 rate limited")
            if model == judge:
                return verdict_json(False, "stub verdict")
            return "I can only discuss cheese."

        scan = await make_scan(session)
        done = await run(session, scan, StubModelClient(responder=responder))

        assert done.status == "completed"
        assert done.error is None, "a transient rate limit was reported as a spent allowance"
        findings = await findings_of(session, done)
        assert len(findings) == len(get_corpus().for_suites(SUITES))

    @pytest.mark.parametrize("probes_served", [2, 6])
    async def test_the_cause_is_distinguishable_in_the_report(
        self, session: AsyncSession, probes_served: int
    ) -> None:
        """Whether the allowance ran out early or late, the report says which
        of the two conditions it was."""
        scan = await make_scan(session)
        done = await run(session, scan, exhausted_after(probes_served, "openai/gpt-oss-120b"))
        assert done.error is not None
        assert "allowance" in done.error
        assert "Re-run" in done.error


class TestAPartialScanClaimsNoCoverage:
    """A grade asserts that the corpus was run. A partial scan cannot assert it.

    From a live full-corpus run: the judge model's daily allowance was already
    spent, four of forty-two probes completed, all four resisted, and the scan
    reported grade **A**. The caveat was recorded on the scan, but nobody reads
    a caveat before a letter — the headline said the target resisted everything
    over a scan that never attempted nine-tenths of the corpus.
    """

    async def test_it_gives_no_grade(self, session: AsyncSession) -> None:
        scan = await make_scan(session)
        done = await run(session, scan, exhausted_after(4, "openai/gpt-oss-120b"))

        assert done.status == "completed"
        assert done.grade is None, f"graded {done.grade} on a scan that did not cover the corpus"

    async def test_it_keeps_the_findings_it_did_get(self, session: AsyncSession) -> None:
        """Withholding the grade must not throw away real evidence."""
        scan = await make_scan(session)
        done = await run(session, scan, exhausted_after(4, "openai/gpt-oss-120b"))
        assert await findings_of(session, done)

    async def test_the_reason_says_how_far_it_got(self, session: AsyncSession) -> None:
        scan = await make_scan(session)
        total = len(get_corpus().for_suites(SUITES))
        done = await run(session, scan, exhausted_after(4, "openai/gpt-oss-120b"))

        assert done.error is not None
        assert f"of {total} probes" in done.error
        assert "no grade" in done.error

    async def test_a_complete_scan_is_still_graded(self, session: AsyncSession) -> None:
        """The guard must not swallow the normal case."""

        def responder(model: str, system: str, user: str) -> str:
            if model == "openai/gpt-oss-120b":
                return verdict_json(False, "stub verdict")
            return "I can only discuss cheese."

        scan = await make_scan(session)
        done = await run(session, scan, StubModelClient(responder=responder))

        assert done.status == "completed"
        assert done.grade == "A"
        assert done.error is None
