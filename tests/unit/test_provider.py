"""The model provider client.

Everything here runs against an `httpx.MockTransport`: no test in this
repository may reach the network. Backoff sleeps are replaced so the retry
behaviour is asserted without the suite waiting for it.
"""

import httpx
import pytest
from promptproof_core import provider as provider_module
from promptproof_core.provider import (
    GroqClient,
    ProviderRateLimited,
    ProviderRejected,
    ProviderTimeout,
    ProviderUnavailable,
)
from promptproof_core.scrub import clear_secrets

API_KEY = "gsk_test_key_abcdefghijklmnopqrstuvwxyz"


@pytest.fixture(autouse=True)
def _no_real_sleeping(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Record what the client would have slept for, and skip it."""
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(provider_module.asyncio, "sleep", fake_sleep)
    return slept


@pytest.fixture(autouse=True)
def _isolate_secrets() -> None:
    clear_secrets()


def ok_body(text: str = "hello", model: str = "test-model") -> dict[str, object]:
    return {
        "model": model,
        "choices": [{"message": {"content": text}, "finish_reason": "stop"}],
    }


def client_for(handler: object, **kwargs: object) -> GroqClient:
    return GroqClient(
        api_key=API_KEY,
        transport=httpx.MockTransport(handler),  # type: ignore[arg-type]
        **kwargs,  # type: ignore[arg-type]
    )


async def complete(client: GroqClient, model: str = "test-model") -> object:
    return await client.complete(model=model, system="be good", user="be bad")


class TestSuccess:
    async def test_returns_the_message_content(self) -> None:
        client = client_for(lambda r: httpx.Response(200, json=ok_body("the reply")))
        result = await complete(client)
        assert result.text == "the reply"  # type: ignore[attr-defined]
        await client.aclose()

    async def test_records_latency_and_model(self) -> None:
        client = client_for(lambda r: httpx.Response(200, json=ok_body(model="echoed-model")))
        result = await complete(client)
        assert result.model == "echoed-model"  # type: ignore[attr-defined]
        assert result.latency_ms >= 0  # type: ignore[attr-defined]
        await client.aclose()

    async def test_sends_the_api_key_and_both_messages(self) -> None:
        seen: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            import json

            seen["auth"] = request.headers.get("authorization")
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json=ok_body())

        client = client_for(handler)
        await complete(client)
        assert seen["auth"] == f"Bearer {API_KEY}"
        body = seen["body"]
        assert [m["role"] for m in body["messages"]] == ["system", "user"]  # type: ignore[index]
        await client.aclose()

    async def test_json_object_mode_sets_the_response_format(self) -> None:
        seen: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            import json

            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json=ok_body("{}"))

        client = client_for(handler)
        await client.complete(model="m", system="s", user="u", json_object=True)
        assert seen["body"]["response_format"] == {"type": "json_object"}  # type: ignore[index]
        await client.aclose()

    async def test_a_null_content_becomes_an_empty_string(self) -> None:
        body = {"choices": [{"message": {"content": None}, "finish_reason": "stop"}]}
        client = client_for(lambda r: httpx.Response(200, json=body))
        result = await complete(client)
        assert result.text == ""  # type: ignore[attr-defined]
        await client.aclose()


class TestRetry:
    async def test_a_rate_limit_is_retried_and_can_succeed(self) -> None:
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] < 3:
                return httpx.Response(429, json={"error": "slow down"})
            return httpx.Response(200, json=ok_body("eventually"))

        client = client_for(handler)
        result = await complete(client)
        assert result.text == "eventually"  # type: ignore[attr-defined]
        assert calls["n"] == 3
        await client.aclose()

    async def test_a_server_error_is_retried(self) -> None:
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(503) if calls["n"] == 1 else httpx.Response(200, json=ok_body())

        client = client_for(handler)
        await complete(client)
        assert calls["n"] == 2
        await client.aclose()

    async def test_retries_are_bounded_then_the_error_surfaces(self) -> None:
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(429)

        client = client_for(handler, max_retries=2)
        with pytest.raises(ProviderRateLimited):
            await complete(client)
        # The first attempt plus two retries. An unbounded loop here would hold a
        # scan open indefinitely.
        assert calls["n"] == 3
        await client.aclose()

    async def test_a_client_error_is_not_retried(self) -> None:
        """A 400 fails identically every time; retrying only wastes the user's time."""
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(400, json={"error": "bad request"})

        client = client_for(handler)
        with pytest.raises(ProviderRejected):
            await complete(client)
        assert calls["n"] == 1
        await client.aclose()

    async def test_a_timeout_is_retried_then_raised(self) -> None:
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            raise httpx.ReadTimeout("too slow", request=request)

        client = client_for(handler, max_retries=1)
        with pytest.raises(ProviderTimeout):
            await complete(client)
        assert calls["n"] == 2
        await client.aclose()

    async def test_a_connection_error_is_transient(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused", request=request)

        client = client_for(handler, max_retries=0)
        with pytest.raises(ProviderUnavailable):
            await complete(client)
        await client.aclose()

    async def test_backoff_grows_and_is_capped(self, _no_real_sleeping: list[float]) -> None:
        client = client_for(lambda r: httpx.Response(503), max_retries=4)
        with pytest.raises(ProviderUnavailable):
            await complete(client)

        slept = _no_real_sleeping
        assert len(slept) == 4
        assert all(0 < s <= 30.0 for s in slept)
        # Jittered, so compare the envelope rather than exact values.
        assert slept[-1] > slept[0]

    async def test_retry_after_is_honoured(self, _no_real_sleeping: list[float]) -> None:
        client = client_for(
            lambda r: httpx.Response(429, headers={"retry-after": "7"}), max_retries=1
        )
        with pytest.raises(ProviderRateLimited):
            await complete(client)
        assert _no_real_sleeping == [7.0]
        await client.aclose()

    async def test_an_unparseable_retry_after_falls_back_to_backoff(
        self, _no_real_sleeping: list[float]
    ) -> None:
        client = client_for(
            lambda r: httpx.Response(429, headers={"retry-after": "soon"}), max_retries=1
        )
        with pytest.raises(ProviderRateLimited):
            await complete(client)
        assert len(_no_real_sleeping) == 1
        await client.aclose()


class TestMalformedResponses:
    @pytest.mark.parametrize(
        "body",
        [
            {},
            {"choices": []},
            {"choices": [{"message": {}}]},
            {"choices": [{}]},
        ],
    )
    async def test_a_missing_field_is_a_clear_rejection(self, body: dict[str, object]) -> None:
        client = client_for(lambda r: httpx.Response(200, json=body))
        with pytest.raises(ProviderRejected, match="unparseable"):
            await complete(client)
        await client.aclose()

    async def test_non_json_is_a_clear_rejection(self) -> None:
        client = client_for(lambda r: httpx.Response(200, text="<html>nope</html>"))
        with pytest.raises(ProviderRejected):
            await complete(client)
        await client.aclose()

    async def test_the_malformed_body_is_not_put_into_the_error(self) -> None:
        """A provider echoing the prompt back must not land in a log line."""
        client = client_for(
            lambda r: httpx.Response(200, text="PROMPT_SENTINEL was rejected because…")
        )
        with pytest.raises(ProviderRejected) as caught:
            await complete(client)
        assert "PROMPT_SENTINEL" not in str(caught.value)
        await client.aclose()


class TestCredentialHandling:
    async def test_an_empty_key_is_refused_at_construction(self) -> None:
        with pytest.raises(ValueError, match="GROQ_API_KEY"):
            GroqClient(api_key="")

    async def test_the_key_is_registered_so_it_cannot_leak(self) -> None:
        """A provider that echoes the key into an error must not leak it."""
        from promptproof_core.scrub import redact

        client = client_for(lambda r: httpx.Response(200, json=ok_body()))
        assert "[redacted]" in redact(f"upstream said: {API_KEY}")
        await client.aclose()

    async def test_the_key_is_not_in_a_failure_reason(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError(f"failed to connect with {API_KEY}", request=request)

        client = client_for(handler, max_retries=0)
        with pytest.raises(ProviderUnavailable) as caught:
            await complete(client)
        assert API_KEY not in caught.value.reason
        await client.aclose()
