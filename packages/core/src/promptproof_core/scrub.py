"""Keeping secrets and user content out of telemetry.

The rule in docs/observability.md is that spans, logs and metric labels carry
identifiers, lengths, counts, latencies, outcomes, model names and reason codes —
never prompt text, response text, rationales, auth headers or API keys.

Most of that is enforced by call sites simply not passing content. This module is
the backstop for the case call sites cannot control: a secret arriving inside
something else, typically an exception message or an upstream error body that
echoes a key back.

Nothing here makes it safe to log content deliberately. It makes it hard for a
secret to escape by accident.
"""

import re
from collections.abc import Iterable

REDACTED = "[redacted]"

# A short value would match everywhere and turn every log line into noise, so
# registered secrets below this length are ignored.
MIN_SECRET_LENGTH = 12

# Bounded, so a provider returning a wall of HTML cannot become a log line.
MAX_REASON_LENGTH = 200

# Shapes that are a credential wherever they appear, whether or not this process
# ever held that particular one.
SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bgsk_[A-Za-z0-9]{20,}"),  # Groq
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}"),  # OpenAI-style
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{12,}", re.IGNORECASE),
    re.compile(r"\bghp_[A-Za-z0-9]{20,}"),  # GitHub
    # Deliberately no generic "long opaque string" pattern. One would match a
    # 32-hex trace ID, a corpus digest and a scan UUID, redacting precisely the
    # identifiers telemetry exists to correlate on. Registered values plus these
    # provider-specific shapes cover the real risk without that cost.
    #
    # A connection string carries the password in the netloc.
    re.compile(r"\b[a-z+]+://[^\s:/@]+:[^\s:/@]+@", re.IGNORECASE),
)

_registered: set[str] = set()


def register_secret(value: str | None) -> None:
    """Register a known secret so it is removed wherever it later appears.

    Called at startup for the API key, and whenever an endpoint target's auth
    header is read. Values shorter than `MIN_SECRET_LENGTH` are ignored.
    """
    if value and len(value) >= MIN_SECRET_LENGTH:
        _registered.add(value)


def register_secrets(values: Iterable[str | None]) -> None:
    for value in values:
        register_secret(value)


def clear_secrets() -> None:
    """Only for tests. Production registers once at startup and never forgets."""
    _registered.clear()


def redact(text: str) -> str:
    """Remove registered secrets and anything credential-shaped."""
    if not text:
        return text

    # Longest first, so a secret containing another is not partly revealed.
    for secret in sorted(_registered, key=len, reverse=True):
        if secret in text:
            text = text.replace(secret, REDACTED)

    for pattern in SECRET_PATTERNS:
        text = pattern.sub(REDACTED, text)

    return text


def reason_code(exc: BaseException, *, limit: int = MAX_REASON_LENGTH) -> str:
    """A short, safe description of a failure, for a log field or a finding.

    Exception messages are the most common way a secret or a chunk of user
    content escapes into telemetry: an HTTP client puts the URL in the message,
    a parser puts the offending input in it. So the message is redacted,
    stripped of newlines and truncated, and the exception type — which is always
    safe and usually the most useful part — leads.
    """
    kind = type(exc).__name__
    message = redact(str(exc)).replace("\n", " ").replace("\r", " ").strip()
    if not message:
        return kind

    combined = f"{kind}: {message}"
    if len(combined) <= limit:
        return combined
    return combined[: limit - 1].rstrip() + "…"


def summarise(text: str | None) -> dict[str, int]:
    """What may be said about a piece of content: how much of it there was."""
    return {"length": len(text or "")}


def excerpt(text: str | None, limit: int) -> str | None:
    """Truncate evidence for storage.

    This is for the database, which is allowed to hold evidence, never for
    telemetry. Kept here so the limit lives next to the rules it serves.
    """
    if text is None:
        return None
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"
