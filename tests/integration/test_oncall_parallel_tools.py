"""That the responder survives a model asking for two tools at once.

A model is free to return several tool calls in one response, and PydanticAI
runs them concurrently. The responder's tools all touch one `AsyncSession`,
which cannot serve concurrent operations — so a parallelising model raises
`concurrent operations are not permitted` partway through a run, *after*
earlier writes have committed. The run both fails and leaves the queue in a
state nobody signed off on.

This was found live, not in testing, because the first model tried happened to
emit one tool call at a time. Two earlier attempts to pin it did not:

- one built its own `asyncio.Lock` and wrapped the remediation functions by
  hand, so it passed with the lock deleted from the agent entirely;
- one grepped `agent.py` for the string `async with ctx.deps.lock:`, so it
  asserted the implementation's text rather than its behaviour.

This drives the real agent, with real tools, against a real session, using a
model that returns two tool calls in one response — which is the actual
condition. Remove the lock and it fails.
"""

from typing import Any

from promptproof_core.models import Scan, Target, TargetFamily
from promptproof_core.oncall.agent import OnCallDeps, build_agent
from promptproof_core.settings import Settings
from pydantic_ai.messages import (
    ModelMessage,
    ModelResponse,
    TextPart,
    ToolCallPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


def parallel_then_finish(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    """Ask for two tools at once, then return the report.

    The first response carries two tool calls, which is what makes them run
    concurrently. The second produces the structured output.
    """
    if len(messages) == 1:
        return ModelResponse(
            parts=[
                ToolCallPart("inspect_queue", {}),
                ToolCallPart("inspect_worker_activity", {}),
            ]
        )
    if not any(
        isinstance(part, ToolCallPart) and part.tool_name == "final_result"
        for message in messages
        for part in getattr(message, "parts", [])
    ):
        return ModelResponse(
            parts=[
                ToolCallPart(
                    "final_result",
                    {
                        "branch": "D2 stale lease",
                        "diagnosis": "A worker died mid-scan.",
                        "inspected": ["queue", "worker activity"],
                        "actions": [],
                        "escalate": False,
                        "unresolved": "",
                    },
                )
            ]
        )
    return ModelResponse(parts=[TextPart("done")])


def settings_for(session_url: str) -> Settings:
    return Settings(
        groq_api_key="test-key",
        database_url=session_url,
        scan_lease_seconds=300,
        scan_max_attempts=3,
    )


async def seed_stale_scan(session: AsyncSession) -> None:
    family = TargetFamily(name="Parallel tools target")
    session.add(family)
    await session.flush()
    target = Target(
        family_id=family.id,
        version=1,
        kind="prompt",
        model="qwen/qwen3.8-27b",
        system_prompt="You only discuss cheese.",
    )
    session.add(target)
    await session.flush()
    scan = Scan(
        target_id=target.id, suites=["tool-abuse"], status="running", probe_count=1, attempts=1
    )
    session.add(scan)
    await session.flush()
    await session.execute(
        text(
            "UPDATE scans SET locked_at = now() - interval '600 seconds', "
            "locked_by = 'dead-worker' WHERE id = :i"
        ).bindparams(i=scan.id)
    )
    await session.commit()


class TestParallelToolCalls:
    async def test_two_tools_in_one_turn_do_not_break_the_session(
        self, session: AsyncSession
    ) -> None:
        await seed_stale_scan(session)

        settings = settings_for("postgresql+asyncpg://u:p@localhost:5432/unused")
        agent = build_agent("stub", "stub-key", "scan-queue-stalled")
        deps = OnCallDeps(session=session, settings=settings, actions=[])

        model = FunctionModel(parallel_then_finish)
        with agent.override(model=model):
            result = await agent.run("Alert: scan-queue-stalled", deps=deps)

        assert result.output.branch == "D2 stale lease"
        assert set(deps.actions) == {"inspect_queue", "inspect_worker_activity"}

    async def test_the_session_is_still_usable_afterwards(self, session: AsyncSession) -> None:
        """An overlap can leave the session in a state where later work fails,
        which is how this would surface in production: not at the overlap, but
        at the next thing the responder tried to do."""
        await seed_stale_scan(session)

        settings = settings_for("postgresql+asyncpg://u:p@localhost:5432/unused")
        agent = build_agent("stub", "stub-key", "scan-queue-stalled")
        deps = OnCallDeps(session=session, settings=settings, actions=[])

        with agent.override(model=FunctionModel(parallel_then_finish)):
            await agent.run("Alert: scan-queue-stalled", deps=deps)

        rows: Any = (await session.execute(text("SELECT count(*) FROM scans"))).scalar()
        assert rows >= 1
