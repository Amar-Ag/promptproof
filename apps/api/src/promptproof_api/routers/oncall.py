"""The alert webhook the on-call responder listens on.

Authenticated by HMAC over the exact bytes received. The receiver can change
production state — it reclaims leases and requeues scans — so it has to be able
to tell a real alert from anyone who found the URL.

Not under `/api`: this is machine-to-machine and is not part of the product's
interface.
"""

from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request, status
from promptproof_core.alerts import verify
from promptproof_core.logging import get_logger
from promptproof_core.oncall.agent import OnCallReport, respond
from promptproof_core.oncall.issues import file_issue
from promptproof_core.telemetry import set_attributes, span

from ..deps import SessionDep, SettingsDep

router = APIRouter(tags=["on-call"], include_in_schema=False)
log = get_logger(__name__)


@router.post("/internal/oncall/alert", status_code=status.HTTP_202_ACCEPTED)
async def receive_alert(
    request: Request,
    session: SessionDep,
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

    with span("oncall.respond", attributes={"alert": str(alert.get("alert"))}):
        try:
            report, tools_used = await respond(alert, session, settings)
        except Exception as exc:
            # The responder failing is itself an incident, and silence would be
            # the worst outcome: someone would assume it handled things.
            log.error("oncall.failed", alert=alert.get("alert"), reason=type(exc).__name__)
            report = OnCallReport(
                branch="responder failure",
                diagnosis=(
                    f"The on-call responder could not complete its run "
                    f"({type(exc).__name__}). Nothing was remediated."
                ),
                escalate=True,
                unresolved="A human needs to work the runbook manually.",
            )
            tools_used = []

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
            tools_used,
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
