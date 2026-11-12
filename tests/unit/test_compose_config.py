"""That the API and the worker agree about the queue.

The on-call agent runs inside the API and decides whether a lease has expired
and whether a scan has attempts left. The worker enforces those same two
numbers. Nothing couples them except configuration, and in the first on-call
drill they were not coupled: the API had never been given `SCAN_LEASE_SECONDS`
or `SCAN_MAX_ATTEMPTS`, so the agent reasoned with the defaults while the worker
ran with something else.

The visible result was an agent that reclaimed a scan the worker would never
pick up again, left it `pending` forever, and reported that it had fixed the
problem. A silently wrong remediation that reports success is the worst outcome
available to an on-call system, so the agreement is asserted rather than
commented.
"""

import re
from pathlib import Path

import pytest

COMPOSE = Path(__file__).resolve().parents[2] / "compose.yaml"

# Settings that both processes must read identically, and why.
SHARED_QUEUE_SETTINGS = {
    "SCAN_LEASE_SECONDS": "the agent and the worker must expire a lease at the same moment",
    "SCAN_MAX_ATTEMPTS": "the agent must not requeue work the worker considers exhausted",
}


def service_env(name: str) -> dict[str, str]:
    """The environment block of one compose service, without a YAML dependency."""
    text = COMPOSE.read_text(encoding="utf-8")
    match = re.search(rf"^  {name}:$(.*?)(?=^  \S|\Z)", text, re.M | re.S)
    assert match, f"no service named {name} in compose.yaml"
    body = match.group(1)

    env_match = re.search(r"^    environment:$(.*?)(?=^    \S|\Z)", body, re.M | re.S)
    if not env_match:
        return {}
    pairs = re.findall(r"^      ([A-Z0-9_]+):\s*(.*)$", env_match.group(1), re.M)
    return dict(pairs)


@pytest.mark.parametrize(("setting", "reason"), SHARED_QUEUE_SETTINGS.items())
def test_api_and_worker_read_the_same_queue_settings(setting: str, reason: str) -> None:
    api = service_env("api")
    worker = service_env("worker")

    assert setting in worker, f"the worker is missing {setting}"
    assert setting in api, (
        f"the API does not declare {setting}, but the on-call agent runs there — {reason}"
    )
    assert api[setting] == worker[setting], f"{setting} differs between api and worker — {reason}"
