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
    """Too fast. Waiting helps."""

    transient = True

    def __init__(self, reason: str, retry_after: float | None = None) -> None:
        super().__init__(reason)
        self.retry_after = retry_after


class ProviderQuotaExhausted(ProviderError):
    """The allowance for the day is spent. Waiting does not help.

    Both arrive as HTTP 429 and treating them alike is why a scan could report
    a third of its probes as identical errors: every probe queued behind an
    allowance that would not return for hours, retried its three times, and
    failed the same way. Retrying is not merely futile here, it spends the
    requests-per-day allowance on calls that cannot succeed.

    Not transient, because the only useful response is to stop and say so.
    """

    transient = False

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


def parse_reset(value: str | None) -> float | None:
    """Seconds from a Groq reset header: `1.41s`, `120ms`, `8h5m16.8s`.

    Returns None for anything unrecognised, which the caller treats as "no
    information" rather than as zero -- guessing zero here would turn a pacing
    mechanism into a busy loop.
    """
    if not value:
        return None
    text = value.strip().lower()
    if text.endswith("ms"):
        try:
            return float(text[:-2]) / 1000.0
        except ValueError:
            return None

    total = 0.0
    number = ""
    seen = False
    units = {"h": 3600.0, "m": 60.0, "s": 1.0}
    try:
        for char in text:
            if char.isdigit() or char == ".":
                number += char
            elif char in units:
                if not number:
                    return None
                total += float(number) * units[char]
                number = ""
                seen = True
            else:
                return None
    except ValueError:
        # `1.2.3s` reaches float() looking numeric and is not. Unguarded, this
        # escaped as a bare ValueError all the way out of `complete()` -- past
        # `evaluate`, which catches only ProviderError -- and a malformed
        # header from a provider or an intervening proxy became an errored
        # probe reading "unexpected: ValueError". This function's one job is to
        # answer "how long", and "I cannot tell" is one of the answers.
        return None
    return total if seen and not number else None


class TokenBudget:
    """Paces calls using the allowance the provider reports back.

    Groq returns `x-ratelimit-remaining-tokens` and `x-ratelimit-reset-tokens`
    on every response. Reading them turns rate limiting from something we
    discover by being refused into something we can stay underneath, which is
    the difference between a report with a third of its probes undetermined and
    one without.

    This is deliberately not a guessed concurrency constant. The sustainable
    rate depends on the account, the model and the length of the probes, none
    of which this repository knows. The provider does, and it says so on every
    call.

    Per model throughout -- the allowance is per model, and so is the lock. A
    scan's target calls and its judge calls draw on separate budgets, the
    judge's running out first because it is the more expensive call. One shared
    lock made that untrue in the way that mattered: a target call waiting out
    its window held the lock, so a judge call with nine thousand tokens to
    spare blocked behind it for the full wait and then returned "no wait
    needed". Two models, two locks.
    """

    def __init__(self, floor: int = 1500, max_wait_seconds: float = 65.0) -> None:
        # The floor is roughly three judge calls. Stopping with a little in
        # hand leaves room for calls already in flight, which have not been
        # counted yet in the number we were given.
        self._floor = floor
        self._max_wait = max_wait_seconds
        self._remaining: dict[str, int] = {}
        self._ready_at: dict[str, float] = {}
        # Bumped on every observation, so a waiter can tell whether the numbers
        # it decided to wait on are still the newest ones.
        self._generation: dict[str, int] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock_for(self, model: str) -> asyncio.Lock:
        lock = self._locks.get(model)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[model] = lock
        return lock

    def observe(self, model: str, headers: Any) -> None:
        """Record the allowance reported alongside a response."""
        raw = headers.get("x-ratelimit-remaining-tokens")
        if raw is None:
            return
        try:
            remaining = int(raw)
        except (TypeError, ValueError):
            return
        self._remaining[model] = remaining
        self._generation[model] = self._generation.get(model, 0) + 1
        reset = parse_reset(headers.get("x-ratelimit-reset-tokens"))
        if reset is not None:
            self._ready_at[model] = time.monotonic() + reset

    async def wait_for_room(self, model: str) -> float:
        """Hold a call back if the reported allowance is nearly spent.

        Returns the seconds waited, for the caller to log. Serialised per model
        so a burst of probes does not each independently decide there is room.
        """
        async with self._lock_for(model):
            remaining = self._remaining.get(model)
            if remaining is None or remaining >= self._floor:
                return 0.0
            ready_at = self._ready_at.get(model)
            if ready_at is None:
                return 0.0
            delay = min(max(ready_at - time.monotonic(), 0.0), self._max_wait)
            if delay <= 0:
                return 0.0

            generation = self._generation.get(model, 0)
            log.info(
                "provider.pacing",
                model=model,
                remaining_tokens=remaining,
                waiting_seconds=round(delay, 2),
            )
            await asyncio.sleep(delay)

            # Clear only if nothing newer arrived while we slept. Responses
            # from calls already in flight land during the wait, and one of
            # them may report a window that is *worse* than the one we just
            # waited out. Discarding it unconditionally meant the next call saw
            # no information at all and fired straight into a spent allowance
            # -- producing exactly the undetermined probe this exists to stop.
            if self._generation.get(model, 0) == generation:
                self._remaining.pop(model, None)
                self._ready_at.pop(model, None)
            return delay


# Markers that name a *daily* allowance. `(tpd)` and `(rpd)` are Groq's own
# codes; "per day" alone is ordinary English and is accepted only alongside
# the phrase that makes the body a rate-limit error.
DAILY_LIMIT_CODES = ("(tpd)", "(rpd)")
DAILY_LIMIT_PHRASE = "per day"
RATE_LIMIT_PHRASE = "rate limit reached"


def _names_a_daily_limit(response: httpx.Response) -> bool:
    """Whether a 429 is about a daily allowance rather than a per-minute rate.

    Groq says which limit was hit -- "on tokens per day (TPD)" against "on
    tokens per minute (TPM)" -- and that single distinction decides whether
    waiting is worth anything.

    Read from the `error.message` field rather than from the whole body, and
    matched against fixed markers. The body is provider text that may echo the
    request back, and "per day" is a phrase a system prompt can easily contain:
    a target told "users may ask 5 questions per day" would otherwise turn an
    ordinary per-minute limit into a spent-allowance verdict, stop every
    remaining probe, and tell the user their day was over when it was not.

    Matched to classify and never quoted. A body we cannot read is treated as
    the recoverable case, because retrying a per-minute limit costs a wait
    while refusing a scan that could have run costs the user their scan.
    """
    try:
        body = response.json()
        message = str(body["error"]["message"]).lower()
    except Exception:
        return False

    if any(code in message for code in DAILY_LIMIT_CODES):
        return True
    return DAILY_LIMIT_PHRASE in message and RATE_LIMIT_PHRASE in message


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
        pacing_floor: int = 1500,
        pacing_max_wait: float = 65.0,
    ) -> None:
        if not api_key:
            raise ValueError("GROQ_API_KEY is not set; the worker cannot call a model.")

        # So that a key echoed back in an error body can never reach a log line.
        register_secret(api_key)

        self._max_retries = max_retries
        self._budget = TokenBudget(floor=pacing_floor, max_wait_seconds=pacing_max_wait)
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
            # Stay under the reported allowance rather than discovering it by
            # being refused.
            await self._budget.wait_for_room(model)
            try:
                response = await self._client.post(path, json=payload)
            except httpx.TimeoutException as exc:
                error: ProviderError = ProviderTimeout(reason_code(exc))
            except httpx.HTTPError as exc:
                error = ProviderUnavailable(reason_code(exc))
            else:
                # Observed on success and on refusal alike: a 429 carries the
                # allowance too, and that is exactly when it matters.
                self._budget.observe(model, response.headers)
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
        # It is *matched* against known provider phrasing to tell a per-minute
        # limit from a spent daily allowance, and the reason is then written
        # here from scratch -- no provider text is ever carried through.
        status = response.status_code
        if status == 429:
            retry_after = response.headers.get("retry-after")
            parsed: float | None = None
            if retry_after:
                try:
                    parsed = float(retry_after)
                except ValueError:
                    parsed = None
            if _names_a_daily_limit(response):
                return ProviderQuotaExhausted(
                    "provider daily allowance exhausted", retry_after=parsed
                )
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
        pacing_floor=settings.pacing_floor_tokens,
        # Never longer than the lease: a wait that outlives the claim hands the
        # scan to another worker while this one is still asleep holding it.
        pacing_max_wait=min(settings.pacing_max_wait_seconds, settings.scan_lease_seconds / 2.0),
    )
