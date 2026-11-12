"""Evaluating the alert rules and delivering what fires.

Runs in the API, not the worker, and that placement is the whole point.

The stalled-queue rule exists to notice that *the worker has stopped picking
work up*. An evaluator living inside the worker cannot report that: the
condition it watches for is its own death. Putting it there would produce a
system that alerts on everything except the failure it was built for — and
would look fine in every test that did not kill the worker.

The API is the component that stays up, already runs continuously, and can
reach the same queue with two queries.

Delivery is best-effort by design. An alert that cannot be delivered is logged
loudly, but a webhook being down must never stop scans running — the system
being partly broken is not a reason to break the rest of it.
"""

import asyncio
from datetime import UTC, datetime

import httpx
from promptproof_core.alerts import Alert, Evaluator, encode, evaluate_all, sign
from promptproof_core.db import get_sessionmaker
from promptproof_core.logging import get_logger
from promptproof_core.settings import Settings

log = get_logger(__name__)

# A human channel is a chat webhook and answers immediately. The on-call
# endpoint runs an agent that inspects the queue and works a runbook before it
# replies, which takes tens of seconds. One timeout for both means either the
# agent is cut off mid-run or a dead chat webhook holds the loop open.
DELIVERY_TIMEOUT_SECONDS = 10.0
ONCALL_TIMEOUT_SECONDS = 90.0


async def deliver(alert: Alert, settings: Settings) -> None:
    """Send a firing alert to the on-call agent and to a human channel.

    Both, not either. The agent acts; the human needs to know an agent is
    acting, and needs to be the one who notices when it does not.
    """
    payload = alert.payload()
    targets: list[tuple[str, str, dict[str, str], float]] = []

    if settings.alert_oncall_webhook_url:
        headers = {"Content-Type": "application/json"}
        if settings.oncall_webhook_secret:
            # The receiver can change production state, so it must be able to
            # tell a real alert from anyone who found the URL.
            headers["X-Promptproof-Signature"] = sign(payload, settings.oncall_webhook_secret)
        targets.append(
            ("oncall", settings.alert_oncall_webhook_url, headers, ONCALL_TIMEOUT_SECONDS)
        )

    if settings.alert_human_webhook_url:
        targets.append(
            (
                "human",
                settings.alert_human_webhook_url,
                {"Content-Type": "application/json"},
                DELIVERY_TIMEOUT_SECONDS,
            )
        )

    if not targets:
        log.warning(
            "alert.undelivered",
            alert=alert.rule.name,
            reason="no webhook configured",
            value=payload["value"],
            runbook=alert.rule.runbook_url,
        )
        return

    # Serialised once, here, so the bytes that are signed are the bytes that
    # are sent. Handing the payload to the client and letting it serialise
    # separately produces a signature over something nobody receives.
    body = encode(payload)

    # Human first. The on-call call blocks for as long as the agent takes to
    # work the runbook, and a person should not hear about an incident only
    # after a machine has finished thinking about it.
    targets.sort(key=lambda t: t[0] != "human")

    async with httpx.AsyncClient() as client:
        for name, url, headers, timeout in targets:
            try:
                response = await client.post(url, content=body, headers=headers, timeout=timeout)
                response.raise_for_status()
                log.info("alert.delivered", alert=alert.rule.name, target=name)
            except Exception as exc:
                # Loud, because an alert nobody received is worse than no alert:
                # it leaves everyone believing they would have been told.
                log.error(
                    "alert.delivery_failed",
                    alert=alert.rule.name,
                    target=name,
                    reason=type(exc).__name__,
                )


async def run_alert_loop(settings: Settings, stop: asyncio.Event) -> None:
    """Measure the rules on a schedule until asked to stop."""
    evaluator = Evaluator()
    sessionmaker = get_sessionmaker()
    log.info(
        "alerting.start",
        interval_seconds=settings.alert_eval_interval_seconds,
        oncall_configured=bool(settings.alert_oncall_webhook_url),
        human_configured=bool(settings.alert_human_webhook_url),
    )

    while not stop.is_set():
        try:
            async with sessionmaker() as session:
                for alert in await evaluate_all(session, evaluator, datetime.now(UTC)):
                    log.error(
                        "alert.firing",
                        alert=alert.rule.name,
                        value=round(alert.value, 3),
                        threshold=alert.rule.threshold,
                        runbook=alert.rule.runbook_url,
                        **alert.context,
                    )
                    await deliver(alert, settings)
        except Exception as exc:
            # The alerting loop must not take the worker down with it. An
            # alerter that crashes on a transient database error is an alerter
            # that is silent exactly when things are going wrong.
            log.error("alerting.evaluation_failed", reason=type(exc).__name__)

        try:
            await asyncio.wait_for(stop.wait(), timeout=settings.alert_eval_interval_seconds)
        except TimeoutError:
            continue
