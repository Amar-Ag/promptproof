"""Telling "too fast" apart from "nothing left today".

Both arrive as HTTP 429, and treating them as one thing is why a scan could
come back with a third of its probes errored (#32). Every probe queued behind
an allowance that would not reset for hours, retried its three times, and
failed identically — spending the requests-per-day allowance on calls that
could not succeed, and producing a report whose gaps looked like facts about
the target.

Measured against the account these defaults are for: each model allows 8,000
tokens per minute, 1,000 requests per day, and 200,000 tokens per day. The
per-minute limit is a pacing problem. The daily ones are not.
"""

import httpx
import pytest
from promptproof_core.provider import (
    GroqClient,
    ProviderQuotaExhausted,
    ProviderRateLimited,
    TokenBudget,
    parse_reset,
)


def client_for(handler: object, **kwargs: object) -> GroqClient:
    return GroqClient(
        api_key="gsk_test_key",
        transport=httpx.MockTransport(handler),  # type: ignore[arg-type]
        **kwargs,  # type: ignore[arg-type]
    )


# The provider's own phrasing, which is what the classifier reads. Quoted here
# in the test rather than in the code, where a reason string must never carry
# provider text that could echo a prompt back.
TPD_BODY = {
    "error": {
        "message": (
            "Rate limit reached for model `openai/gpt-oss-120b` in organization "
            "`org_redacted` service tier `on_demand` on tokens per day (TPD): "
            "Limit 200000, Used 199446, Requested 1011. Please try again in 3m17s."
        ),
        "code": "rate_limit_exceeded",
    }
}
TPM_BODY = {
    "error": {
        "message": (
            "Rate limit reached for model `qwen/qwen3.8-27b` in organization "
            "`org_redacted` service tier `on_demand` on tokens per minute (TPM): "
            "Limit 8000, Used 7900, Requested 500. Please try again in 1.9s."
        ),
        "code": "rate_limit_exceeded",
    }
}


class TestParseReset:
    """Groq states the reset as a duration, in several shapes."""

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("1.41s", 1.41),
            ("120ms", 0.12),
            ("8h5m16.8s", 8 * 3600 + 5 * 60 + 16.8),
            ("2m", 120.0),
        ],
    )
    def test_it_reads_the_shapes_the_provider_sends(self, value: str, expected: float) -> None:
        parsed = parse_reset(value)
        assert parsed is not None
        assert parsed == pytest.approx(expected)

    @pytest.mark.parametrize("value", [None, "", "soon", "17", "1x2y"])
    def test_anything_unrecognised_is_no_information_rather_than_zero(
        self, value: str | None
    ) -> None:
        """Zero would turn pacing into a busy loop against a provider that is
        already refusing us."""
        assert parse_reset(value) is None


class TestClassification:
    async def test_a_daily_limit_is_not_retried(self) -> None:
        """Retrying is not merely futile: each attempt spends from the
        requests-per-day allowance on a call that cannot succeed."""
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(429, json=TPD_BODY)

        client = client_for(handler, max_retries=3)
        with pytest.raises(ProviderQuotaExhausted):
            await client.complete(model="m", system="s", user="u")
        await client.aclose()

        assert calls == 1, f"a spent daily allowance was retried {calls} times"

    async def test_a_per_minute_limit_is_retried(self) -> None:
        """Here waiting is exactly the right answer."""
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            if calls == 1:
                return httpx.Response(429, json=TPM_BODY, headers={"retry-after": "0"})
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]},
            )

        client = client_for(handler, max_retries=3)
        completion = await client.complete(model="m", system="s", user="u")
        await client.aclose()

        assert completion.text == "ok"
        assert calls == 2

    async def test_an_unreadable_body_is_treated_as_the_recoverable_case(self) -> None:
        """Refusing a scan that could have run costs the user their scan;
        retrying a per-minute limit costs a wait."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(429, content=b"", headers={"retry-after": "0"})

        client = client_for(handler, max_retries=1)
        with pytest.raises(ProviderRateLimited):
            await client.complete(model="m", system="s", user="u")
        await client.aclose()

    async def test_the_reason_never_carries_provider_text(self) -> None:
        """The body names the organisation and echoes the request size. The
        reason is stored and logged, so it is written here, not quoted."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(429, json=TPD_BODY)

        client = client_for(handler, max_retries=0)
        with pytest.raises(ProviderQuotaExhausted) as caught:
            await client.complete(model="m", system="s", user="u")
        await client.aclose()

        reason = caught.value.reason
        assert "org_redacted" not in reason
        assert "199446" not in reason
        assert reason == "provider daily allowance exhausted"


class TestPacing:
    """Staying under the allowance instead of discovering it by being refused."""

    async def test_it_does_not_wait_while_there_is_room(self) -> None:
        budget = TokenBudget(floor=1500)
        budget.observe(
            "m",
            httpx.Headers(
                {
                    "x-ratelimit-remaining-tokens": "7000",
                    "x-ratelimit-reset-tokens": "30s",
                }
            ),
        )
        assert await budget.wait_for_room("m") == 0.0

    async def test_it_waits_when_the_allowance_is_nearly_spent(self) -> None:
        budget = TokenBudget(floor=1500)
        budget.observe(
            "m",
            httpx.Headers(
                {
                    "x-ratelimit-remaining-tokens": "200",
                    "x-ratelimit-reset-tokens": "30ms",
                }
            ),
        )
        waited = await budget.wait_for_room("m")
        assert waited > 0, "ran straight into a limit it had been told about"

    async def test_a_wait_is_not_repeated_for_the_same_window(self) -> None:
        """After waiting out the window the allowance has rolled over; holding
        every later call back on stale numbers would stall the scan."""
        budget = TokenBudget(floor=1500)
        budget.observe(
            "m",
            httpx.Headers(
                {
                    "x-ratelimit-remaining-tokens": "200",
                    "x-ratelimit-reset-tokens": "10ms",
                }
            ),
        )
        assert await budget.wait_for_room("m") > 0
        assert await budget.wait_for_room("m") == 0.0

    async def test_budgets_are_per_model(self) -> None:
        """A scan's target calls and its judge calls draw on separate
        allowances, and the judge's runs out first."""
        budget = TokenBudget(floor=1500)
        budget.observe(
            "judge",
            httpx.Headers(
                {
                    "x-ratelimit-remaining-tokens": "50",
                    "x-ratelimit-reset-tokens": "10ms",
                }
            ),
        )
        assert await budget.wait_for_room("target") == 0.0
        assert await budget.wait_for_room("judge") > 0

    async def test_a_wait_is_bounded(self) -> None:
        """A provider reporting an eight-hour reset must not park a worker for
        eight hours holding a lease."""
        budget = TokenBudget(floor=1500, max_wait_seconds=0.05)
        budget.observe(
            "m",
            httpx.Headers(
                {
                    "x-ratelimit-remaining-tokens": "0",
                    "x-ratelimit-reset-tokens": "8h5m16.8s",
                }
            ),
        )
        waited = await budget.wait_for_room("m")
        assert waited <= 0.05

    async def test_missing_headers_leave_pacing_inert(self) -> None:
        """A provider that says nothing about allowances must not be paced on
        invented numbers."""
        budget = TokenBudget(floor=1500)
        budget.observe("m", httpx.Headers({}))
        assert await budget.wait_for_room("m") == 0.0

    async def test_a_429_is_observed_too(self) -> None:
        """A refusal carries the allowance, and that is exactly when it
        matters: the next call should wait rather than pile on."""
        budget = TokenBudget(floor=1500)
        budget.observe(
            "m",
            httpx.Headers(
                {
                    "x-ratelimit-remaining-tokens": "0",
                    "x-ratelimit-reset-tokens": "20ms",
                }
            ),
        )
        assert await budget.wait_for_room("m") > 0
