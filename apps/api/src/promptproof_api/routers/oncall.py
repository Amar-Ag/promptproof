"""The alert webhook the on-call responder listens on.

Authenticated by HMAC over the exact bytes received. The receiver can change
production state — it reclaims leases and fails abandoned scans — so it has to
be able to tell a real alert from anyone who found the URL.

Not under `/api`: this is machine-to-machine and is not part of the product's
interface.

**The run outlives the request.** The caller gives up after a timeout and
disconnects, which cancels the handler; but by then the responder may already
have committed changes to user-visible rows. If cancellation killed the run, a
remediation would land with no report and no issue, and the alert loop would
record only that delivery failed — so the written history would say the
responder was unreachable at the moment it was changing production. The work
therefore runs in a shielded task with its own database session, and the
request merely waits for it.
"""

import asyncio
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request, status
from promptproof_core.alerts import verify
from promptproof_core.db import session_scope
from promptproof_core.logging import get_logger
from promptproof_core.oncall.agent import OnCallReport, OnCallRunCancelled, respond
from promptproof_core.oncall.issues import file_issue
from promptproof_core.settings import Settings
from promptproof_core.telemetry import set_attributes, span

from ..deps import SettingsDep

router = APIRouter(tags=["on-call"], include_in_schema=False)
log = get_logger(__name__)

# Shorter than the deliverer's patience (alerting.ONCALL_TIMEOUT_SECONDS), so a
# run that is going nowhere ends as a report rather than as a disconnection.
RUN_TIMEOUT_SECONDS = 75.0

# How old a signed alert may be before it is refused.
#
# A valid signature is valid forever otherwise, so anyone who captures one body
# can replay it indefinitely -- and every replay is a full agent run, spending
# tokens from the daily per-model quota that ADR-0006 identifies as this
# responder's scarce resource. A few hundred overnight replays leave the
# responder starved when a real alert fires: the exact failure the ADR says to
# watch for, made reachable on purpose.
#
# `fired_at` is already inside the signed bytes, so it costs nothing to check
# and cannot be adjusted without invalidating the signature.
MAX_ALERT_AGE_SECONDS = 600.0

# One run at a time. The second line of defence against the same exhaustion:
# replays queue behind each other rather than multiplying, and two genuine
# alerts arriving together cannot race on the queue they are both diagnosing.
_run_slot = asyncio.Semaphore(1)


async def _respond_and_record(alert: dict[str, Any], settings: Settings) -> dict[str, Any]:
    """One complete response: diagnose, remediate, file the paperwork.

    Owns its own session because it may outlive the request that started it.
    """
    async with session_scope() as session:
        with span("oncall.respond", attributes={"alert": str(alert.get("alert"))}):
            try:
                async with asyncio.timeout(RUN_TIMEOUT_SECONDS):
                    report, runs = await respond(alert, session, settings)
            except OnCallRunCancelled as cancelled:
                # The run was cancelled after committing something. File the
                # report it carries, then let the cancellation stand.
                report, runs = cancelled.report, cancelled.runs
                log.warning(
                    "oncall.cancelled",
                    alert=alert.get("alert"),
                    tools_used=[r.name for r in runs],
                )
            except Exception as exc:
                # `respond` reports its own interruptions, so reaching here means
                # something outside the agent run failed. Silence would be the
                # worst outcome: someone would assume it had been handled.
                log.error("oncall.failed", alert=alert.get("alert"), reason=type(exc).__name__)
                report = OnCallReport(
                    branch="responder failure",
                    diagnosis=(
                        f"The on-call responder could not complete its run "
                        f"({type(exc).__name__}). Whether anything was changed before "
                        f"it failed could not be determined."
                    ),
                    escalate=True,
                    unresolved="A human needs to work the runbook manually.",
                )
                runs = []

            set_attributes(
                **{
                    "oncall.branch": report.branch,
                    "oncall.actions": len(report.actions),
                    "oncall.escalated": report.escalate,
                }
            )

    issue_url = None
    if settings.oncall_github_token:
        issue_url = await file_issue(
            settings.oncall_repo,
            settings.oncall_github_token,
            alert,
            report,
            runs,
        )
    else:
        # Loud, because the written trail is half the point of the responder.
        log.warning("oncall.issue_skipped", reason="no github token configured")

    return {
        "branch": report.branch,
        "actions": report.actions,
        "escalated": report.escalate,
        "issue": issue_url,
    }


@router.post("/internal/oncall/alert", status_code=status.HTTP_202_ACCEPTED)
async def receive_alert(
    request: Request,
    settings: SettingsDep,
    x_promptproof_signature: str | None = Header(default=None),
) -> dict[str, Any]:
    body = await request.body()

    # No secret configured means no authenticated caller is possible, so the
    # endpoint refuses rather than running unauthenticated. Failing closed is
    # the only safe default for something that can change production state.
    if not settings.oncall_webhook_secret:
        log.error("oncall.refused", reason="no webhook secret configured")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="On-call responder is not configured.",
        )

    if not x_promptproof_signature or not verify(
        body, x_promptproof_signature, settings.oncall_webhook_secret
    ):
        log.warning("oncall.rejected", reason="bad or missing signature")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid signature.")

    try:
        alert: dict[str, Any] = await request.json()
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Body is not JSON."
        ) from exc

    fired_at = alert.get("fired_at")
    if isinstance(fired_at, str):
        try:
            age = (datetime.now(UTC) - datetime.fromisoformat(fired_at)).total_seconds()
        except ValueError:
            log.warning("oncall.rejected", reason="unparseable fired_at")
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="Bad fired_at."
            ) from None
        if age > MAX_ALERT_AGE_SECONDS:
            log.warning("oncall.rejected", reason="stale alert", age_seconds=int(age))
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="Alert is too old."
            )

    # Shielded: if the caller disconnects, the response is lost but the run and
    # its issue are not. A remediation without a record is the thing this
    # endpoint exists to prevent.
    async def run_one() -> dict[str, Any]:
        async with _run_slot:
            return await _respond_and_record(alert, settings)

    task = asyncio.create_task(run_one())
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        log.warning(
            "oncall.caller_disconnected",
            alert=alert.get("alert"),
            detail="run continues; the issue will still be filed",
        )
        raise
