"""Alert rules and the firing state machine.

The failure modes worth testing here are the ones that make people mute a
channel: firing on a single noisy sample, re-firing the same alert every
evaluation, and alerting on an idle system at night.
"""

from datetime import UTC, datetime, timedelta

import pytest
from promptproof_core.alerts import (
    FAILURE_RATE_HIGH,
    QUEUE_STALLED,
    RULES,
    Alert,
    Evaluator,
    sign,
    verify,
)

T0 = datetime(2026, 11, 12, 9, 0, tzinfo=UTC)


def at(seconds: int) -> datetime:
    return T0 + timedelta(seconds=seconds)


class TestRules:
    def test_both_rules_describe_a_person_not_a_machine(self) -> None:
        """An alert is for someone affected, not for a resource being busy."""
        for rule in RULES:
            assert rule.symptom
            # Resource words would mean we are alerting on the wrong thing.
            assert not any(word in rule.symptom.lower() for word in ("cpu", "memory", "disk"))

    def test_every_rule_links_a_runbook(self) -> None:
        """An alert without a runbook is a notification, and gets muted."""
        for rule in RULES:
            assert rule.runbook.endswith(".md")
            assert rule.runbook_url.startswith("https://")

    def test_thresholds_match_the_specification(self) -> None:
        assert QUEUE_STALLED.threshold == 120.0
        assert QUEUE_STALLED.sustain_seconds == 300
        assert FAILURE_RATE_HIGH.threshold == 0.20
        assert FAILURE_RATE_HIGH.sustain_seconds == 600


class TestFiring:
    def test_a_single_bad_sample_does_not_fire(self) -> None:
        """Scans queue for a few seconds legitimately. Without a sustain window
        this rule would fire on ordinary traffic and teach everyone to ignore it."""
        e = Evaluator()
        assert e.observe(QUEUE_STALLED, 500.0, at(0)) is None

    def test_a_sustained_breach_fires_once_the_window_passes(self) -> None:
        e = Evaluator()
        assert e.observe(QUEUE_STALLED, 500.0, at(0)) is None
        assert e.observe(QUEUE_STALLED, 500.0, at(299)) is None

        alert = e.observe(QUEUE_STALLED, 500.0, at(301))
        assert alert is not None
        assert alert.rule is QUEUE_STALLED
        assert alert.value == 500.0

    def test_it_does_not_fire_again_while_still_firing(self) -> None:
        """Re-sending every thirty seconds is how a channel becomes muted."""
        e = Evaluator()
        e.observe(QUEUE_STALLED, 500.0, at(0))
        assert e.observe(QUEUE_STALLED, 500.0, at(301)) is not None

        for offset in (331, 361, 3600):
            assert e.observe(QUEUE_STALLED, 500.0, at(offset)) is None

    def test_recovery_clears_the_state(self) -> None:
        e = Evaluator()
        e.observe(QUEUE_STALLED, 500.0, at(0))
        assert e.observe(QUEUE_STALLED, 500.0, at(301)) is not None

        assert e.observe(QUEUE_STALLED, 5.0, at(400)) is None
        assert e.state(QUEUE_STALLED).firing is False
        assert e.state(QUEUE_STALLED).breaching_since is None

    def test_it_can_fire_again_after_recovering(self) -> None:
        """A problem that comes back is a new problem, and must be announced."""
        e = Evaluator()
        e.observe(QUEUE_STALLED, 500.0, at(0))
        e.observe(QUEUE_STALLED, 500.0, at(301))
        e.observe(QUEUE_STALLED, 5.0, at(400))

        assert e.observe(QUEUE_STALLED, 500.0, at(500)) is None
        assert e.observe(QUEUE_STALLED, 500.0, at(801)) is not None

    def test_a_flapping_signal_restarts_the_window(self) -> None:
        """Brief recovery resets the clock, so flapping never accumulates into a fire."""
        e = Evaluator()
        for offset in range(0, 900, 60):
            assert e.observe(QUEUE_STALLED, 500.0, at(offset)) is None
            assert e.observe(QUEUE_STALLED, 1.0, at(offset + 30)) is None

    def test_exactly_at_the_threshold_is_not_a_breach(self) -> None:
        e = Evaluator()
        e.observe(QUEUE_STALLED, 120.0, at(0))
        assert e.observe(QUEUE_STALLED, 120.0, at(301)) is None

    def test_rules_are_tracked_independently(self) -> None:
        e = Evaluator()
        e.observe(QUEUE_STALLED, 500.0, at(0))
        e.observe(FAILURE_RATE_HIGH, 0.9, at(0))

        assert e.observe(QUEUE_STALLED, 500.0, at(301)) is not None
        # The failure rule has a longer window and must not fire with it.
        assert e.observe(FAILURE_RATE_HIGH, 0.9, at(301)) is None
        assert e.observe(FAILURE_RATE_HIGH, 0.9, at(601)) is not None


class TestPayload:
    def test_it_carries_the_symptom_and_the_runbook(self) -> None:
        alert = Alert(rule=QUEUE_STALLED, value=321.5, fired_at=T0, context={"waiting": 4})
        payload = alert.payload()

        assert payload["alert"] == "scan-queue-stalled"
        assert "watching it not start" in payload["symptom"]
        assert payload["runbook"].endswith("stuck-queue.md")
        assert payload["value"] == 321.5
        assert payload["threshold"] == 120.0
        assert payload["context"]["waiting"] == 4

    def test_every_dynamic_value_is_a_number(self) -> None:
        """A webhook body leaves the system, so nothing dynamic may be free text.

        Substring checks do not work here — the runbook URL contains the word
        "prompt" because the project is called promptproof, which is exactly
        the kind of false positive that teaches people to weaken an assertion.
        So this checks structure instead: the only strings are static fields
        defined in code, and everything measured is numeric.
        """
        alert = Alert(
            rule=QUEUE_STALLED, value=300.0, fired_at=T0, context={"waiting": 2, "oldest": 301.2}
        )
        payload = alert.payload()

        static = {"alert", "symptom", "runbook", "fired_at"}
        assert set(payload) == static | {"value", "threshold", "context"}

        # The static fields come from the Rule definition, not from any scan.
        assert payload["alert"] == QUEUE_STALLED.name
        assert payload["symptom"] == QUEUE_STALLED.symptom
        assert payload["runbook"] == QUEUE_STALLED.runbook_url

        assert isinstance(payload["value"], (int, float))
        assert isinstance(payload["threshold"], (int, float))
        for key, value in payload["context"].items():
            assert isinstance(value, (int, float)), (
                f"context[{key!r}] is {type(value).__name__}; only measurements may be sent"
            )


class TestSigning:
    SECRET = "a-shared-secret-value"

    def test_a_signature_round_trips(self) -> None:
        import json

        payload = {"alert": "scan-queue-stalled", "value": 300}
        signature = sign(payload, self.SECRET)
        body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        assert verify(body, signature, self.SECRET)

    def test_a_tampered_body_fails(self) -> None:
        payload = {"alert": "scan-queue-stalled", "value": 300}
        signature = sign(payload, self.SECRET)
        assert not verify(b'{"alert":"something-else"}', signature, self.SECRET)

    def test_the_wrong_secret_fails(self) -> None:
        import json

        payload = {"alert": "scan-queue-stalled"}
        signature = sign(payload, self.SECRET)
        body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        assert not verify(body, signature, "a-different-secret")

    @pytest.mark.parametrize("secret", ["", "x"])
    def test_signing_still_produces_a_digest_for_odd_secrets(self, secret: str) -> None:
        # Whether such a secret should be allowed is the receiver's business;
        # signing must not crash on it.
        assert len(sign({"a": 1}, secret)) == 64
