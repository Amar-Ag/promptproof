"""Alerting on user impact.

Two rules, both describing something a person is experiencing rather than
something a machine is doing. There is deliberately no alert on CPU, memory or
disk: those tell you a resource is busy, not that anyone is worse off, and an
inbox full of them is how real alerts get ignored.

Each rule carries the user-visible symptom and the runbook that resolves it,
because an alert without a runbook is a notification, and a notification at 3am
is just interrupted sleep.

**Why this is in the application rather than in a dashboard.** The signals are
already here, the conditions are plain SQL, and keeping the rules in the
repository means they are reviewed, tested and versioned like everything else.
It also means the evaluator can be exercised by the test suite, which a
dashboard rule cannot be.
"""

import hashlib
import hmac
import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from .logging import get_logger

log = get_logger(__name__)

RUNBOOK_BASE = "https://github.com/Amar-Ag/promptproof/blob/main/docs/runbooks"


@dataclass(frozen=True, slots=True)
class Rule:
    """One alert: what is measured, what counts as bad, and who fixes it."""

    name: str
    symptom: str
    runbook: str
    threshold: float
    # How long the condition must hold before firing. A single bad sample is
    # noise; a sustained one is a problem. Scans legitimately queue for a few
    # seconds, so without this the queue rule would fire constantly.
    sustain_seconds: int
    # For a rule whose value is already an average over a period, how long that
    # period is.
    #
    # These two must not be conflated. A rate measured over ten minutes is
    # already smoothed, so requiring it to *also* hold for ten minutes means
    # twenty minutes of sustained failure — and the evidence ages out of the
    # measurement window while the sustain window is still counting, so the
    # alert can never fire at all. Found by running the drill: a real 71%
    # failure rate would have gone unreported.
    window_seconds: int | None = None

    @property
    def runbook_url(self) -> str:
        return f"{RUNBOOK_BASE}/{self.runbook}"


QUEUE_STALLED = Rule(
    name="scan-queue-stalled",
    symptom="Scans are sitting in the queue. Someone submitted one and is watching it not start.",
    runbook="stuck-queue.md",
    threshold=120.0,
    sustain_seconds=300,
)

FAILURE_RATE_HIGH = Rule(
    name="scan-failure-rate-high",
    symptom=(
        "Scans are finishing as failures instead of reports. "
        "Users are getting errors, not verdicts."
    ),
    runbook="scan-failures.md",
    threshold=0.20,
    # Short, because window_seconds below does the smoothing.
    sustain_seconds=60,
    window_seconds=600,
)

RULES: tuple[Rule, ...] = (QUEUE_STALLED, FAILURE_RATE_HIGH)


@dataclass
class RuleState:
    """Where a rule is in its lifecycle. Held in memory by the evaluator."""

    breaching_since: datetime | None = None
    firing: bool = False


@dataclass(frozen=True, slots=True)
class Alert:
    rule: Rule
    value: float
    fired_at: datetime
    context: dict[str, Any] = field(default_factory=dict)

    def payload(self) -> dict[str, Any]:
        """What a receiver gets. Identifiers and numbers, never scan content."""
        return {
            "alert": self.rule.name,
            "symptom": self.rule.symptom,
            "runbook": self.rule.runbook_url,
            "value": round(self.value, 3),
            "threshold": self.rule.threshold,
            "fired_at": self.fired_at.isoformat(),
            "context": self.context,
        }


# ── Measuring ────────────────────────────────────────────────────────────────

_OLDEST_PENDING_SQL = """
SELECT coalesce(max(extract(epoch FROM now() - queued_at)), 0) AS oldest,
       count(*)                                                AS waiting
  FROM scans
 WHERE status = 'pending'
"""

# Only finished scans count. Including in-flight ones would make the rate swing
# wildly as a batch completes.
_FAILURE_RATE_SQL = """
SELECT count(*) FILTER (WHERE status = 'failed')  AS failed,
       count(*)                                   AS total
  FROM scans
 WHERE finished_at > now() - (:window_seconds * interval '1 second')
"""


async def measure_queue_age(session: AsyncSession) -> tuple[float, dict[str, Any]]:
    row = (await session.execute(text(_OLDEST_PENDING_SQL))).mappings().one()
    return float(row["oldest"]), {"waiting": int(row["waiting"])}


async def measure_failure_rate(
    session: AsyncSession, window_seconds: int = 600
) -> tuple[float, dict[str, Any]]:
    row = (
        (await session.execute(text(_FAILURE_RATE_SQL).bindparams(window_seconds=window_seconds)))
        .mappings()
        .one()
    )
    total = int(row["total"])
    failed = int(row["failed"])
    # With nothing finished there is no rate. Reporting 0 keeps a quiet system
    # quiet; reporting 1.0 would alert every night.
    rate = (failed / total) if total else 0.0
    return rate, {"failed": failed, "total": total, "window_seconds": window_seconds}


# ── Evaluating ───────────────────────────────────────────────────────────────


class Evaluator:
    """Tracks each rule's state and decides when to fire.

    Fires on the transition into a sustained breach, not on every evaluation:
    re-sending the same alert every thirty seconds is how a channel becomes
    something people mute.
    """

    def __init__(self, rules: Sequence[Rule] = RULES) -> None:
        self._state: dict[str, RuleState] = {r.name: RuleState() for r in rules}

    def state(self, rule: Rule) -> RuleState:
        return self._state.setdefault(rule.name, RuleState())

    def observe(
        self,
        rule: Rule,
        value: float,
        now: datetime,
        context: dict[str, Any] | None = None,
    ) -> Alert | None:
        """Feed one measurement in. Returns an Alert only on the firing edge."""
        state = self.state(rule)
        breaching = value > rule.threshold

        if not breaching:
            if state.firing:
                log.info("alert.resolved", alert=rule.name, value=round(value, 3))
            state.breaching_since = None
            state.firing = False
            return None

        if state.breaching_since is None:
            state.breaching_since = now
            return None

        held_for = (now - state.breaching_since).total_seconds()
        if held_for < rule.sustain_seconds or state.firing:
            return None

        state.firing = True
        return Alert(rule=rule, value=value, fired_at=now, context=context or {})


async def evaluate_all(
    session: AsyncSession, evaluator: Evaluator, now: datetime | None = None
) -> list[Alert]:
    """Measure every rule and return whichever have just started firing."""
    moment = now or datetime.now(UTC)
    fired: list[Alert] = []

    age, age_context = await measure_queue_age(session)
    if alert := evaluator.observe(QUEUE_STALLED, age, moment, age_context):
        fired.append(alert)

    rate, rate_context = await measure_failure_rate(
        session, FAILURE_RATE_HIGH.window_seconds or 600
    )
    if alert := evaluator.observe(FAILURE_RATE_HIGH, rate, moment, rate_context):
        fired.append(alert)

    return fired


# ── Delivering ───────────────────────────────────────────────────────────────


def sign(payload: dict[str, Any], secret: str) -> str:
    """HMAC over the exact bytes sent.

    The receiver is a thing that can change production state, so it has to be
    able to tell a real alert from anyone who found the URL.
    """
    body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def verify(body: bytes, signature: str, secret: str) -> bool:
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)
