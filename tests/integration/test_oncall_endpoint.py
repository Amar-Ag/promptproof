"""The webhook that wakes the responder.

This endpoint can change production rows, so the only thing standing between an
alert and a stranger is the signature check. It had no test: the behaviour was
confirmed once by hand during a drill and then trusted, which is exactly the
arrangement that lets an inverted condition ship green.

The failure this guards against is not subtle. Invert `if not signature or not
verify(...)`, or drop the fail-closed branch, and an unauthenticated POST runs
the agent against live data while the whole suite stays passing.

The agent itself is never called here: `respond` is replaced, because what is
under test is who gets through the door.
"""

import hashlib
import hmac
import json
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import pytest_asyncio
from promptproof_api.main import create_app
from promptproof_api.routers import oncall as oncall_router
from promptproof_core.db import init_engine
from promptproof_core.oncall.agent import OnCallReport
from promptproof_core.settings import Settings, get_settings

SECRET = "endpoint-drill-secret"

ALERT: dict[str, Any] = {
    "alert": "scan-queue-stalled",
    "symptom": "Scans are sitting in the queue.",
    "value": 607.8,
    "threshold": 120.0,
    "runbook": "docs/runbooks/stuck-queue.md",
}


def signed_body(payload: dict[str, Any], secret: str) -> tuple[bytes, str]:
    body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return body, hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


DEFAULT_URL = "postgresql+asyncpg://promptproof:promptproof@localhost:5433/promptproof"


def integration_database_url() -> str:
    """The same database the rest of the integration suite uses."""
    base = os.environ.get("DATABASE_URL", DEFAULT_URL)
    head, _, name = base.rpartition("/")
    return f"{head}/{name}_test"


def settings_with(secret: str) -> Settings:
    """Settings as the app would hold them, with the on-call secret we choose.

    `get_settings` is cached, so an environment variable set in a test never
    reaches the app. The dependency is overridden instead.
    """
    return Settings(
        groq_api_key="test-key",
        database_url=integration_database_url(),
        oncall_webhook_secret=secret,
        oncall_github_token="",
    )


def app_with(secret: str, respond_stub: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(oncall_router, "respond", respond_stub)
    # The handler opens its own session now, so that it can outlive the request
    # it was started by. That needs a real engine even when the agent is stubbed.
    settings = settings_with(secret)
    init_engine(settings)
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    return app


async def accepted_stub(
    alert: dict[str, Any], session: Any, settings: Any
) -> tuple[OnCallReport, list[str]]:
    return (
        OnCallReport(branch="D2 stale lease", diagnosis="stub", actions=["reclaimed 1 scan"]),
        ["inspect_queue"],
    )


@pytest_asyncio.fixture
async def client(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[httpx.AsyncClient]:
    """The real app, with the agent run and its paperwork stubbed out."""
    app = app_with(SECRET, accepted_stub, monkeypatch)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


class TestAuthentication:
    async def test_a_correctly_signed_alert_is_accepted(self, client: httpx.AsyncClient) -> None:
        body, signature = signed_body(ALERT, SECRET)
        response = await client.post(
            "/internal/oncall/alert",
            content=body,
            headers={
                "content-type": "application/json",
                "x-promptproof-signature": signature,
            },
        )
        assert response.status_code == 202
        assert response.json()["branch"] == "D2 stale lease"

    async def test_an_unsigned_alert_is_rejected(self, client: httpx.AsyncClient) -> None:
        body, _ = signed_body(ALERT, SECRET)
        response = await client.post(
            "/internal/oncall/alert",
            content=body,
            headers={"content-type": "application/json"},
        )
        assert response.status_code == 401

    async def test_a_wrongly_signed_alert_is_rejected(self, client: httpx.AsyncClient) -> None:
        body, _ = signed_body(ALERT, "not-the-secret")
        _, wrong = signed_body(ALERT, "not-the-secret")
        response = await client.post(
            "/internal/oncall/alert",
            content=body,
            headers={
                "content-type": "application/json",
                "x-promptproof-signature": wrong,
            },
        )
        assert response.status_code == 401

    async def test_a_tampered_body_is_rejected(self, client: httpx.AsyncClient) -> None:
        """The signature must cover the content, not merely accompany it."""
        body, signature = signed_body(ALERT, SECRET)
        tampered = body.replace(b"607.8", b"0.1")
        response = await client.post(
            "/internal/oncall/alert",
            content=tampered,
            headers={
                "content-type": "application/json",
                "x-promptproof-signature": signature,
            },
        )
        assert response.status_code == 401

    async def test_a_non_ascii_signature_is_rejected_not_crashed(
        self, client: httpx.AsyncClient
    ) -> None:
        """`hmac.compare_digest` raises on a non-ASCII str. Unguarded, a junk
        header becomes an unhandled 500 and the rejection is never logged — the
        refused caller reads as a server fault."""
        body, _ = signed_body(ALERT, SECRET)
        response = await client.post(
            "/internal/oncall/alert",
            content=body,
            headers={
                "content-type": "application/json",
                "x-promptproof-signature": ("é" * 64).encode("latin-1"),
            },
        )
        assert response.status_code == 401


class TestFailsClosed:
    async def test_without_a_secret_it_refuses_to_run_at_all(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With no secret there is no authenticated caller, so the only safe
        answer is to refuse — never to run unauthenticated."""

        async def fake_respond(*args: Any, **kwargs: Any) -> tuple[OnCallReport, list[str]]:
            raise AssertionError("the agent ran without any authentication")

        app = app_with("", fake_respond, monkeypatch)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            body, signature = signed_body(ALERT, SECRET)
            response = await c.post(
                "/internal/oncall/alert",
                content=body,
                headers={
                    "content-type": "application/json",
                    "x-promptproof-signature": signature,
                },
            )
        assert response.status_code == 503


class TestMalformed:
    async def test_a_signed_non_json_body_is_a_bad_request(self, client: httpx.AsyncClient) -> None:
        body = b"not json at all"
        signature = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
        response = await client.post(
            "/internal/oncall/alert",
            content=body,
            headers={
                "content-type": "application/json",
                "x-promptproof-signature": signature,
            },
        )
        assert response.status_code == 400


class TestReplay:
    """A valid signature is otherwise valid forever.

    Anyone who captures one signed body can resend it indefinitely, and each
    replay is a full agent run spending tokens from the daily per-model quota
    that ADR-0006 calls this responder's scarce resource. Enough replays
    overnight and the responder is starved when a real alert fires — the
    failure the ADR says to watch for, made reachable deliberately.

    `fired_at` is inside the signed bytes, so it cannot be adjusted without
    breaking the signature.
    """

    async def test_a_fresh_alert_is_accepted(self, client: httpx.AsyncClient) -> None:
        payload = {**ALERT, "fired_at": datetime.now(UTC).isoformat()}
        body, signature = signed_body(payload, SECRET)
        response = await client.post(
            "/internal/oncall/alert",
            content=body,
            headers={
                "content-type": "application/json",
                "x-promptproof-signature": signature,
            },
        )
        assert response.status_code == 202

    async def test_a_replayed_alert_is_refused(self, client: httpx.AsyncClient) -> None:
        old = datetime.now(UTC) - timedelta(hours=9)
        payload = {**ALERT, "fired_at": old.isoformat()}
        body, signature = signed_body(payload, SECRET)
        response = await client.post(
            "/internal/oncall/alert",
            content=body,
            headers={
                "content-type": "application/json",
                "x-promptproof-signature": signature,
            },
        )
        assert response.status_code == 401, (
            "a nine-hour-old alert with a valid signature was accepted; "
            "one captured body could then drain the day's model quota"
        )

    async def test_an_unparseable_timestamp_is_a_bad_request(
        self, client: httpx.AsyncClient
    ) -> None:
        payload = {**ALERT, "fired_at": "not a timestamp"}
        body, signature = signed_body(payload, SECRET)
        response = await client.post(
            "/internal/oncall/alert",
            content=body,
            headers={
                "content-type": "application/json",
                "x-promptproof-signature": signature,
            },
        )
        assert response.status_code == 400
