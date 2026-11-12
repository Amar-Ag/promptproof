"""The on-call responder.

Wakes on an alert, works the matching runbook, applies only the remediations it
is authorised for, and reports what it did.

Its authority comes from its tools, not its prompt. Everything it can do is in
`remediations.py`; there is no general query tool and no shell. If the model
concludes the right fix is something destructive, it has no way to express it —
the only move available is to escalate, which is itself one of the outputs.

The report is produced whether or not anything was changed. A run that found
nothing still files an issue, because "the agent looked and found nothing" and
"the agent never ran" are different facts and only one of them is reassuring.
"""

import asyncio
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field
from pydantic_ai import Agent, RunContext
from pydantic_ai.models.groq import GroqModel
from pydantic_ai.providers.groq import GroqProvider
from sqlalchemy.ext.asyncio import AsyncSession

from ..logging import get_logger
from ..settings import Settings
from . import remediations

log = get_logger(__name__)


@dataclass
class OnCallDeps:
    session: AsyncSession
    settings: Settings
    actions: list[str]
    # One database session, and a model that is free to ask for two tools in a
    # single turn. An AsyncSession cannot serve concurrent operations, so
    # parallel tool calls raise mid-run -- and the failure is worse than it
    # looks, because an interleaved pair could also commit each other's work.
    # The lock makes tool execution serial without constraining the model.
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class OnCallReport(BaseModel):
    """What the responder concluded. Becomes the body of a GitHub issue."""

    branch: str = Field(
        description="Which diagnostic branch of the runbook matched, e.g. 'D2 stale lease'."
    )
    diagnosis: str = Field(description="What is wrong, in one or two sentences.")
    inspected: list[str] = Field(
        default_factory=list, description="What was looked at, and what it showed."
    )
    actions: list[str] = Field(
        default_factory=list,
        description="Remediations applied, with counts. Empty if nothing was changed.",
    )
    escalate: bool = Field(
        default=False,
        description="True when the fix needs a human, because it is outside the authorised set.",
    )
    unresolved: str = Field(
        default="",
        description="What remains, and what a human would need to do. Required when escalating.",
    )


# One runbook per alert, not both.
#
# Correctness first: there is no reason for a queue alert to have the
# failure-rate procedure in front of it, and a model given two procedures can
# pick the wrong one.
#
# Cost second, and it is not theoretical. The free tier caps this model at
# 200,000 tokens per day across the whole service, and an agent run resends its
# entire conversation on every turn, so a multi-turn run is quadratic in its own
# context. The first drills were cut off by that daily cap. Halving the standing
# context does not remove the ceiling, but it buys more incidents under it --
# and an on-call agent that cannot run because a day of scanning spent its quota
# is an on-call agent that is absent exactly when the service is busy.

BASE_PROMPT = """\nYou are the on-call responder for promptproof, a service that runs adversarial
prompt tests. You have been woken by an alert.

Rules you must follow:

- **Inspect before you act.** Never apply a remediation you have not justified
  with something you looked at.
- **Only the tools you have.** You cannot deploy, roll back, restart a process,
  change configuration, alter the schema or delete anything. If the fix needs
  one of those, set escalate and say what a human should do.
- **Say what you actually did.** If you changed nothing, say so plainly and
  leave actions empty. A run that found nothing is a useful report.
- **Never invent numbers.** Every count comes from a tool result.
- **Be brief.** Inspect what the runbook asks for and no more; every extra turn
  costs budget that a real incident may need.
"""

RUNBOOKS = {
    "scan-queue-stalled": """\
Runbook -- **scan-queue-stalled**: scans are queued and not progressing.

  1. Check whether any worker has claimed work recently.
  2. Check the queue depth and how many leases have expired.
  3. If leases have expired, a worker died mid-scan: reclaim them, then mark as
     failed any that are out of attempts so the user sees an honest error.
  4. If no worker has claimed anything and no leases are expired, the worker is
     absent. You cannot start it. Escalate.
""",
    "scan-failure-rate-high": """\
Runbook -- **scan-failure-rate-high**: scans are finishing as failures.

  1. Look at the recent failures grouped by reason.
  2. If they are transient (rate limits, timeouts), requeue those with attempts
     remaining.
  3. If they are concentrated on one target, or look like a bad release, you
     cannot fix it. Escalate.
""",
}

UNKNOWN_ALERT = """\nYou were woken by an alert with no runbook. Inspect the queue and recent
failures, report what you see, change nothing, and escalate.
"""


def prompt_for(alert_name: str | None) -> str:
    """The base rules plus the one runbook that applies."""
    runbook = RUNBOOKS.get(alert_name or "", UNKNOWN_ALERT)
    return BASE_PROMPT + chr(10) + runbook


def build_agent(
    model_id: str, api_key: str, alert_name: str | None = None
) -> Agent[OnCallDeps, OnCallReport]:
    agent: Agent[OnCallDeps, OnCallReport] = Agent(
        GroqModel(model_id, provider=GroqProvider(api_key=api_key)),
        deps_type=OnCallDeps,
        output_type=OnCallReport,
        system_prompt=prompt_for(alert_name),
        # One retry, not two: a retried turn resends the whole conversation
        # and the per-minute budget is what cuts runs short.
        retries=1,
    )

    def record(ctx: RunContext[OnCallDeps], name: str) -> None:
        ctx.deps.actions.append(name)

    @agent.tool
    async def inspect_queue(ctx: RunContext[OnCallDeps]) -> dict[str, Any]:
        """Queue depth, the age of the oldest pending scan, and expired leases."""
        record(ctx, "inspect_queue")
        async with ctx.deps.lock:
            finding = await remediations.inspect_queue(
                ctx.deps.session, ctx.deps.settings.scan_lease_seconds
            )
            return {"summary": finding.summary, **finding.detail}

    @agent.tool
    async def inspect_worker_activity(ctx: RunContext[OnCallDeps]) -> dict[str, Any]:
        """Whether any worker has claimed work recently. Tells an absent worker
        from a present one that is not progressing."""
        record(ctx, "inspect_worker_activity")
        async with ctx.deps.lock:
            finding = await remediations.inspect_worker_activity(ctx.deps.session)
            return {"summary": finding.summary, **finding.detail}

    @agent.tool
    async def inspect_recent_failures(ctx: RunContext[OnCallDeps]) -> dict[str, Any]:
        """Recently failed scans grouped by reason."""
        record(ctx, "inspect_recent_failures")
        async with ctx.deps.lock:
            finding = await remediations.inspect_recent_failures(ctx.deps.session)
            return {"summary": finding.summary, **finding.detail}

    @agent.tool
    async def reclaim_expired_leases(ctx: RunContext[OnCallDeps]) -> dict[str, Any]:
        """Return scans whose worker died back to the queue. Idempotent."""
        record(ctx, "reclaim_expired_leases")
        async with ctx.deps.lock:
            finding = await remediations.reclaim_expired_leases(
                ctx.deps.session,
                ctx.deps.settings.scan_lease_seconds,
                ctx.deps.settings.scan_max_attempts,
            )
            return {"summary": finding.summary, **finding.detail}

    @agent.tool
    async def fail_exhausted_scans(ctx: RunContext[OnCallDeps]) -> dict[str, Any]:
        """Mark scans that are out of attempts as failed, so the user sees an
        honest error rather than waiting forever. Idempotent."""
        record(ctx, "fail_exhausted_scans")
        async with ctx.deps.lock:
            finding = await remediations.fail_exhausted_scans(
                ctx.deps.session,
                ctx.deps.settings.scan_lease_seconds,
                ctx.deps.settings.scan_max_attempts,
            )
            return {"summary": finding.summary, **finding.detail}

    @agent.tool
    async def requeue_transient_failures(ctx: RunContext[OnCallDeps]) -> dict[str, Any]:
        """Retry scans that failed on a rate limit or timeout and still have
        attempts remaining. Idempotent."""
        record(ctx, "requeue_transient_failures")
        async with ctx.deps.lock:
            finding = await remediations.requeue_transient_failures(
                ctx.deps.session, ctx.deps.settings.scan_max_attempts
            )
            return {"summary": finding.summary, **finding.detail}

    return agent


async def respond(
    alert: dict[str, Any], session: AsyncSession, settings: Settings
) -> tuple[OnCallReport, list[str]]:
    """Run the responder against one alert. Returns the report and the tools used."""
    agent = build_agent(settings.oncall_model, settings.groq_api_key, alert.get("alert"))
    deps = OnCallDeps(session=session, settings=settings, actions=[])

    prompt = (
        f"Alert: {alert.get('alert')}\n"
        f"Symptom: {alert.get('symptom')}\n"
        f"Measured value: {alert.get('value')} (threshold {alert.get('threshold')})\n"
        f"Context: {alert.get('context')}\n"
        f"Runbook: {alert.get('runbook')}\n\n"
        "Work the runbook and report what you found and did."
    )

    try:
        result = await agent.run(prompt, deps=deps)
    except Exception as exc:
        # A run can be cut off partway through — a provider rate limit is the
        # common one on a free tier. The tools that already ran have already
        # changed rows, and saying "nothing was remediated" would be a lie in
        # exactly the situation where a human most needs the truth.
        log.error(
            "oncall.run_interrupted",
            alert=alert.get("alert"),
            reason=type(exc).__name__,
            tools_used=deps.actions,
        )
        return _interrupted(exc, deps.actions), deps.actions

    log.info(
        "oncall.responded",
        alert=alert.get("alert"),
        branch=result.output.branch,
        actions=len(result.output.actions),
        escalate=result.output.escalate,
        tools_used=deps.actions,
    )
    return result.output, deps.actions


def _interrupted(exc: Exception, tools_used: list[str]) -> OnCallReport:
    """The report for a run that did not finish.

    It must distinguish a run that only looked from a run that had already
    changed something, because the second leaves the system in a state nobody
    has signed off on.
    """
    writes = [name for name in tools_used if name in remediations.WRITE_ACTIONS]
    if writes:
        changed = (
            "**Changes were already applied before the interruption** — "
            + ", ".join(sorted(set(writes)))
            + ". They are each idempotent and safe to leave, but no diagnosis was "
            "recorded for them, so a human should confirm the queue is in the state "
            "they imply."
        )
    else:
        changed = "Nothing was changed: only read-only inspections had run."

    return OnCallReport(
        branch="run interrupted",
        diagnosis=(
            f"The responder was cut off partway through its run ({type(exc).__name__}) "
            f"and did not reach a conclusion. {changed}"
        ),
        inspected=[f"ran before the interruption: {', '.join(tools_used) or 'nothing'}"],
        actions=sorted(set(writes)),
        escalate=True,
        unresolved=(
            "The alert has not been diagnosed. Work the runbook manually, or re-send "
            "the alert once the provider recovers — every action is idempotent, so a "
            "second run cannot double-apply anything."
        ),
    )
