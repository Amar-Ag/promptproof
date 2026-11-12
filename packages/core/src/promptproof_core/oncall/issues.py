"""Filing the record of a response.

Every run files an issue, including runs that changed nothing. "The agent
looked and found nothing" and "the agent never ran" are different facts, and
only one of them should be reassuring — an on-call system that is silent when
it is healthy is indistinguishable from one that is broken.

The token this uses needs `issues: write` and nothing else. It cannot deploy,
cannot merge, cannot touch code.
"""

from typing import Any

import httpx

from ..logging import get_logger
from .agent import OnCallReport

log = get_logger(__name__)

GITHUB_API = "https://api.github.com"
TIMEOUT_SECONDS = 20.0


def render(alert: dict[str, Any], report: OnCallReport, tools_used: list[str]) -> str:
    """The issue body. Numbers and names only — never scan content."""
    lines = [
        f"Woken by **`{alert.get('alert')}`** at {alert.get('fired_at')}.",
        "",
        f"> {alert.get('symptom')}",
        "",
        f"Measured **{alert.get('value')}** against a threshold of {alert.get('threshold')}.",
        f"Runbook: {alert.get('runbook')}",
        "",
        "## Diagnosis",
        "",
        f"**Branch matched:** {report.branch}",
        "",
        report.diagnosis,
        "",
        "## What I inspected",
        "",
    ]
    lines += [f"- {item}" for item in report.inspected] or ["- _nothing recorded_"]
    lines += ["", "## What I changed", ""]
    if report.actions:
        lines += [f"- {item}" for item in report.actions]
    else:
        lines += [
            "**Nothing.** The condition did not call for any remediation I am authorised to apply."
        ]

    if report.escalate:
        lines += [
            "",
            "## Escalating",
            "",
            "This needs a human. The fix is outside the set of actions I can take:",
            "",
            report.unresolved or "_no detail given_",
        ]
    elif report.unresolved:
        lines += ["", "## Still outstanding", "", report.unresolved]

    lines += [
        "",
        "---",
        "",
        f"Tools used: {', '.join(tools_used) if tools_used else 'none'}",
        "",
        "I can inspect the queue and recent failures, reclaim expired leases, fail exhausted "
        "scans, and requeue transient failures. I cannot deploy, roll back, restart a process, "
        "change configuration, alter the schema or delete anything.",
    ]
    return "\n".join(lines)


async def file_issue(
    repo: str,
    token: str,
    alert: dict[str, Any],
    report: OnCallReport,
    tools_used: list[str],
) -> str | None:
    """Open the issue. Returns its URL, or None if it could not be filed.

    A failure here is logged loudly but never raised: the remediation has
    already happened, and losing the paperwork must not look like losing the fix.
    """
    title = f"[on-call] {alert.get('alert')}: {report.branch}"
    labels = ["on-call"] + (["needs-human"] if report.escalate else [])

    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
            response = await client.post(
                f"{GITHUB_API}/repos/{repo}/issues",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.github+json",
                },
                json={
                    "title": title[:250],
                    "body": render(alert, report, tools_used),
                    "labels": labels,
                },
            )
            response.raise_for_status()
            url = str(response.json().get("html_url"))
            log.info("oncall.issue_filed", url=url, escalate=report.escalate)
            return url
    except Exception as exc:
        log.error("oncall.issue_failed", reason=type(exc).__name__)
        return None
