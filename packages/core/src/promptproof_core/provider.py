"""The one place this system calls a model.

Every target call and every judge call goes through here, so timeouts, bounded
retry, and the guarantee that no prompt or response text reaches telemetry are
decided once rather than per call site.

Failures are typed by what the caller should do about them: `Transient` is worth
retrying, `Permanent` is not. The retry loop below acts on that distinction so
callers do not have to.
"""

import asyncio
import json
import random
import time
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

import httpx

from .logging import get_logger
from .scrub import reason_code, register_secret
from .settings import Settings

log = get_logger(__name__)

GROQ_BASE_URL = "https://api.groq.com/openai/v1"


class ProviderError(Exception):
    """A model call failed. `reason` is safe to store and to log."""

    transient = False

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class ProviderTimeout(ProviderError):
    transient = True


class ProviderRateLimited(ProviderError):
    transient = True

    def __init__(self, reason: str, retry_after: float | None = None) -> None:
        super().__init__(reason)
        self.retry_after = retry_after


class ProviderUnavailable(ProviderError):
    transient = True


class ProviderRejected(ProviderError):
    """A 4xx that will fail identically however many times it is retried."""

    transient = False


@dataclass(frozen=True, slots=True)
class Completion:
    text: str
    latency_ms: int
    model: str
    finish_reason: str | None = None


@runtime_checkable
class ModelClient(Protocol):
    """What the worker depends on. Tests substitute their own implementation."""

    async def complete(
        self,
        *,
        model: str,
        system: str,
        user: str,
        temperature: float = 0.0,
        json_object: bool = False,
    ) -> Completion: ...


class GroqClient:
    """Groq's OpenAI-compatible chat completions endpoint.

    One instance per process, holding one connection pool.
    """

    def __init__(
        self,
        *,
        api_key: str,
        timeout_seconds: float = 60.0,
        max_retries: int = 3,
        base_url: str = GROQ_BASE_URL,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("GROQ_API_KEY is not set; the worker cannot call a model.")

        # So that a key echoed back in an error body can never reach a log line.
        register_secret(api_key)

        self._max_retries = max_retries
        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=httpx.Timeout(timeout_seconds, connect=10.0),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "GroqClient":
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def complete(
        self,
        *,
        model: str,
        system: str,
        user: str,
        temperature: float = 0.0,
        json_object: bool = False,
    ) -> Completion:
        payload: dict[str, Any] = {
            "model": model,
            "temperature": temperature,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        if json_object:
            payload["response_format"] = {"type": "json_object"}

        started = time.monotonic()
        response = await self._post_with_retry("/chat/completions", payload, model)
        latency_ms = int((time.monotonic() - started) * 1000)

        return self._parse(response, model, latency_ms)

    async def _post_with_retry(
        self, path: str, payload: dict[str, Any], model: str
    ) -> httpx.Response:
        attempt = 0
        while True:
            attempt += 1
            try:
                response = await self._client.post(path, json=payload)
            except httpx.TimeoutException as exc:
                error: ProviderError = ProviderTimeout(reason_code(exc))
            except httpx.HTTPError as exc:
                error = ProviderUnavailable(reason_code(exc))
            else:
                if response.status_code < 400:
                    return response
                error = self._classify(response)

            if not error.transient or attempt > self._max_retries:
                # Model identity and the reason code only — never the prompt.
                log.warning(
                    "provider.failed",
                    model=model,
                    attempts=attempt,
                    transient=error.transient,
                    reason=error.reason,
                )
                raise error

            delay = self._backoff(attempt, error)
            log.info(
                "provider.retrying",
                model=model,
                attempt=attempt,
                delay_seconds=round(delay, 2),
                reason=error.reason,
            )
            await asyncio.sleep(delay)

    @staticmethod
    def _classify(response: httpx.Response) -> ProviderError:
        # The body may echo the request, so it is never read into the reason.
        # Status and the provider's own retry hint are enough to decide.
        status = response.status_code
        if status == 429:
            retry_after = response.headers.get("retry-after")
            parsed: float | None = None
            if retry_after:
                try:
                    parsed = float(retry_after)
                except ValueError:
                    parsed = None
            return ProviderRateLimited("HTTP 429 rate limited", retry_after=parsed)
        if status >= 500:
            return ProviderUnavailable(f"HTTP {status} from provider")
        return ProviderRejected(f"HTTP {status} from provider")

    @staticmethod
    def _backoff(attempt: int, error: ProviderError) -> float:
        """Exponential with jitter, honouring the provider's own hint.

        Jitter matters here: this worker fires several probes concurrently, and
        without it they would all rate-limit together and then all retry
        together, reproducing the burst that caused the limit.
        """
        if isinstance(error, ProviderRateLimited) and error.retry_after is not None:
            return min(error.retry_after, 30.0)
        base = min(2.0**attempt, 30.0)
        return base * (0.5 + random.random() / 2)  # noqa: S311 - jitter, not crypto

    @staticmethod
    def _parse(response: httpx.Response, model: str, latency_ms: int) -> Completion:
        try:
            body = response.json()
            choice = body["choices"][0]
            text = choice["message"]["content"] or ""
            finish_reason = choice.get("finish_reason")
        except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
            # The malformed body itself is not logged: it is provider output and
            # may contain the prompt echoed back.
            raise ProviderRejected(f"unparseable provider response: {type(exc).__name__}") from exc

        return Completion(
            text=text,
            latency_ms=latency_ms,
            model=body.get("model", model),
            finish_reason=finish_reason,
        )


def build_client(
    settings: Settings, transport: httpx.AsyncBaseTransport | None = None
) -> GroqClient:
    return GroqClient(
        api_key=settings.groq_api_key,
        timeout_seconds=settings.model_timeout_seconds,
        max_retries=settings.model_max_retries,
        transport=transport,
    )
