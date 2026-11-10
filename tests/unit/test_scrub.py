"""The telemetry scrubber.

The hard rule is that secrets and user content never reach a span, a log field
or a metric label. These tests cover the backstop; they do not excuse a call
site from simply not passing content.
"""

import pytest
from promptproof_core.scrub import (
    REDACTED,
    clear_secrets,
    excerpt,
    reason_code,
    redact,
    register_secret,
    summarise,
)


@pytest.fixture(autouse=True)
def _isolate_registry() -> None:
    clear_secrets()


class TestRegisteredSecrets:
    def test_a_registered_secret_is_removed(self) -> None:
        register_secret("super-secret-value-123")
        assert redact("key=super-secret-value-123 failed") == f"key={REDACTED} failed"

    def test_a_short_value_is_ignored(self) -> None:
        """Registering 'abc' would redact every third word of every log line."""
        register_secret("abc")
        assert redact("abc appears here") == "abc appears here"

    def test_overlapping_secrets_are_fully_removed(self) -> None:
        register_secret("alpha-secret-value")
        register_secret("alpha-secret-value-extended")
        assert "alpha" not in redact("token alpha-secret-value-extended end")

    def test_empty_input_is_returned_unchanged(self) -> None:
        assert redact("") == ""


class TestPatterns:
    @pytest.mark.parametrize(
        "text",
        [
            "gsk_abcdefghijklmnopqrstuvwxyz0123456789",
            "sk-abcdefghijklmnopqrstuvwxyz0123",
            "Authorization: Bearer abcdefghijklmnopqrst",
            "ghp_abcdefghijklmnopqrstuvwxyz0123",
            "postgresql://user:hunter2@db:5432/x",
        ],
    )
    def test_credential_shapes_are_removed_even_if_never_registered(self, text: str) -> None:
        assert REDACTED in redact(text)

    def test_a_trace_id_survives(self) -> None:
        """The regression this guards against.

        A generic 'long opaque string' rule would redact a 32-hex trace ID — the
        exact identifier telemetry exists to correlate on. Logs would look
        scrubbed and be useless.
        """
        trace_id = "4bf92f3577b34da6a3ce929d0e0e4736"
        assert redact(f"trace_id={trace_id}") == f"trace_id={trace_id}"

    def test_a_scan_uuid_survives(self) -> None:
        scan_id = "79020bbd-771f-4174-83c7-f6aac94116c9"
        assert redact(f"scan_id={scan_id}") == f"scan_id={scan_id}"

    def test_a_corpus_digest_survives(self) -> None:
        assert redact("corpus=fed6edda8635") == "corpus=fed6edda8635"


class TestReasonCode:
    def test_leads_with_the_exception_type(self) -> None:
        assert reason_code(TimeoutError("took too long")).startswith("TimeoutError:")

    def test_a_type_with_no_message_is_still_useful(self) -> None:
        assert reason_code(ValueError()) == "ValueError"

    def test_a_secret_in_the_message_is_removed(self) -> None:
        register_secret("gsk_live_key_abcdefghijklmnop")
        produced = reason_code(RuntimeError("auth failed for gsk_live_key_abcdefghijklmnop"))
        assert "gsk_live_key" not in produced

    def test_newlines_are_flattened(self) -> None:
        produced = reason_code(ValueError("line one\nline two\r\nline three"))
        assert "\n" not in produced and "\r" not in produced

    def test_a_long_message_is_truncated(self) -> None:
        produced = reason_code(ValueError("x" * 5_000))
        assert len(produced) <= 200

    def test_a_prompt_echoed_into_an_exception_is_bounded(self) -> None:
        """Content in an exception message is the usual way a leak happens.

        It cannot be removed in general — it is not credential-shaped — so the
        defence is that it is bounded rather than unbounded.
        """
        produced = reason_code(ValueError("rejected input: " + "secret prompt text " * 200))
        assert len(produced) <= 200


class TestHelpers:
    def test_summarise_reports_only_a_length(self) -> None:
        assert summarise("hello") == {"length": 5}
        assert summarise(None) == {"length": 0}

    def test_excerpt_truncates_with_a_marker(self) -> None:
        produced = excerpt("abcdefghij", 5)
        assert produced is not None
        assert len(produced) == 5
        assert produced.endswith("…")

    def test_excerpt_leaves_short_text_alone(self) -> None:
        assert excerpt("abc", 10) == "abc"

    def test_excerpt_passes_none_through(self) -> None:
        assert excerpt(None, 10) is None
