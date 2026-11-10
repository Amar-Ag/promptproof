"""Test doubles.

No test in this repository may call a real model provider or touch the network.
`StubModelClient` is what stands in for one, and it records enough to assert on
concurrency and call routing.
"""

import asyncio
from collections.abc import Callable

from promptproof_core.provider import Completion, ProviderError


class StubModelClient:
    """A model client that answers from a script.

    `responses` maps a model id, or a `(model, marker)` the caller chooses, to
    either text or an exception to raise. `default` covers everything else.
    """

    def __init__(
        self,
        *,
        default: str = "I can only help with what I was set up for.",
        responder: Callable[[str, str, str], str] | None = None,
        latency_ms: int = 7,
    ) -> None:
        self.default = default
        self.responder = responder
        self.latency_ms = latency_ms

        self.calls: list[dict[str, str]] = []
        self.in_flight = 0
        self.peak_in_flight = 0
        self._lock = asyncio.Lock()

    async def complete(
        self,
        *,
        model: str,
        system: str,
        user: str,
        temperature: float = 0.0,
        json_object: bool = False,
    ) -> Completion:
        async with self._lock:
            self.in_flight += 1
            self.peak_in_flight = max(self.peak_in_flight, self.in_flight)

        try:
            self.calls.append({"model": model, "system": system, "user": user})
            # Yield, so concurrent callers genuinely overlap and the peak count
            # measures something real.
            await asyncio.sleep(0)

            # A responder that wants to simulate a provider failure raises.
            result = self.responder(model, system, user) if self.responder else self.default
            return Completion(text=result, latency_ms=self.latency_ms, model=model)
        finally:
            async with self._lock:
                self.in_flight -= 1


class FailingModelClient:
    """Always raises, for the paths where the provider is simply down."""

    def __init__(self, error: ProviderError) -> None:
        self.error = error
        self.call_count = 0

    async def complete(self, **_: object) -> Completion:
        self.call_count += 1
        raise self.error


def verdict_json(exploited: bool, rationale: str = "because", confidence: float = 0.9) -> str:
    flag = "true" if exploited else "false"
    return f'{{"exploited": {flag}, "rationale": "{rationale}", "confidence": {confidence}}}'
