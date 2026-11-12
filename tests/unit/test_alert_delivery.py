"""That a delivered alert can actually be authenticated by its receiver.

This is the test that was missing, and its absence hid a defect that made the
whole on-call path inert: the signature was computed over canonical JSON while
the HTTP client serialised the payload its own way, so the bytes signed were
never the bytes sent. Every real alert would have been rejected as forged.

The earlier drill recorded `signed=true` and read as a pass. It was only
reporting that a signature header was present — nobody had checked that it
verified. So the assertion here is deliberately the strict one: take the bytes
that left the client, and run the receiver's own `verify` against them.
"""

import json
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from promptproof_api import alerting
from promptproof_core.alerts import QUEUE_STALLED, Alert, verify
from promptproof_core.settings import Settings

SECRET = "drill-secret"


def make_settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "groq_api_key": "test-key",
        "database_url": "postgresql+asyncpg://u:p@localhost:5432/db",
        "alert_oncall_webhook_url": "http://oncall.invalid/alert",
        "alert_human_webhook_url": "http://human.invalid/alert",
        "oncall_webhook_secret": SECRET,
    }
    base.update(overrides)
    return Settings(**base)


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch) -> list[httpx.Request]:
    """Capture the requests `deliver` makes, bytes and all."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(202, json={"ok": True})

    transport = httpx.MockTransport(handler)
    original = httpx.AsyncClient

    def client_factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return original(*args, **kwargs)

    monkeypatch.setattr(alerting.httpx, "AsyncClient", client_factory)
    return requests


def an_alert() -> Alert:
    return Alert(
        rule=QUEUE_STALLED,
        value=427.794,
        context={"waiting": 3},
        fired_at=datetime(2026, 11, 12, 17, 4, tzinfo=UTC),
    )


class TestSignature:
    async def test_the_receiver_can_verify_what_was_actually_sent(
        self, captured: list[httpx.Request]
    ) -> None:
        """The whole point. Signed bytes and sent bytes must be the same bytes."""
        await alerting.deliver(an_alert(), make_settings())

        oncall = next(r for r in captured if "oncall" in str(r.url))
        signature = oncall.headers.get("x-promptproof-signature")
        assert signature, "the on-call delivery carried no signature at all"
        assert verify(oncall.content, signature, SECRET), (
            "the receiver rejected a genuine alert: the bytes signed are not the bytes sent"
        )

    async def test_the_body_is_still_valid_json(self, captured: list[httpx.Request]) -> None:
        await alerting.deliver(an_alert(), make_settings())
        oncall = next(r for r in captured if "oncall" in str(r.url))
        assert json.loads(oncall.content)["value"] == pytest.approx(427.794)

    async def test_a_tampered_body_fails_verification(self, captured: list[httpx.Request]) -> None:
        """Guards against the signature being over something trivially forgeable."""
        await alerting.deliver(an_alert(), make_settings())
        oncall = next(r for r in captured if "oncall" in str(r.url))
        signature = oncall.headers["x-promptproof-signature"]
        tampered = oncall.content.replace(b"427.794", b"0.001")
        assert not verify(tampered, signature, SECRET)


class TestTargets:
    async def test_the_human_channel_is_not_signed(self, captured: list[httpx.Request]) -> None:
        """It is a chat webhook, not something that can change production state."""
        await alerting.deliver(an_alert(), make_settings())
        human = next(r for r in captured if "human" in str(r.url))
        assert "x-promptproof-signature" not in human.headers

    async def test_both_targets_receive_the_alert(self, captured: list[httpx.Request]) -> None:
        """Both, not either: the agent acts, and a human needs to know it is acting."""
        await alerting.deliver(an_alert(), make_settings())
        assert len(captured) == 2

    async def test_the_human_is_told_before_the_agent_is_woken(
        self, captured: list[httpx.Request]
    ) -> None:
        """The on-call call blocks for as long as the agent takes to work the
        runbook. A person should not hear about an incident only after a machine
        has finished thinking about it."""
        await alerting.deliver(an_alert(), make_settings())
        order = ["human" if "human" in str(r.url) else "oncall" for r in captured]
        assert order == ["human", "oncall"]

    async def test_the_agent_is_given_longer_than_a_chat_webhook(
        self, captured: list[httpx.Request]
    ) -> None:
        """One timeout for both would cut the agent off mid-run."""
        assert alerting.ONCALL_TIMEOUT_SECONDS > alerting.DELIVERY_TIMEOUT_SECONDS

    async def test_a_failing_target_does_not_stop_the_other(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A dead on-call endpoint must not also silence the human."""
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(str(request.url))
            if "oncall" in str(request.url):
                raise httpx.ConnectError("refused", request=request)
            return httpx.Response(202)

        transport = httpx.MockTransport(handler)
        original = httpx.AsyncClient
        monkeypatch.setattr(
            alerting.httpx,
            "AsyncClient",
            lambda *a, **k: original(*a, **{**k, "transport": transport}),
        )

        await alerting.deliver(an_alert(), make_settings())
        assert any("human" in url for url in seen), "the human was never told"
