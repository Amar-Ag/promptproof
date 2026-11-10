"""The probe suites.

The suite list is fixed and known at build time; the probes inside them are
loaded from `probes/` by the corpus loader (Phase 2). Keeping the suite registry
separate means the API can validate a scan request's suites without loading the
corpus.
"""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Suite:
    key: str
    title: str
    description: str


SUITES: tuple[Suite, ...] = (
    Suite(
        key="direct-injection",
        title="Direct injection",
        description=(
            "Instruction override, 'ignore previous instructions', delimiter and context escape."
        ),
    ),
    Suite(
        key="jailbreak",
        title="Jailbreak",
        description=(
            "Roleplay and persona framing, hypothetical scenarios, encoded or obfuscated payloads."
        ),
    ),
    Suite(
        key="system-prompt-leak",
        title="System prompt leak",
        description=(
            "Coaxing the target into reproducing its own instructions, verbatim or translated."
        ),
    ),
    Suite(
        key="data-exfiltration",
        title="Data exfiltration",
        description="Smuggling context out through rendered markdown images and links.",
    ),
    Suite(
        key="indirect-injection",
        title="Indirect injection",
        description=(
            "Attacker instructions hidden inside content the application treats as retrieved data."
        ),
    ),
    Suite(
        key="tool-abuse",
        title="Tool abuse",
        description=(
            "Persuading the target to invoke tools or capabilities outside its granted scope."
        ),
    ),
)

SUITE_KEYS: frozenset[str] = frozenset(s.key for s in SUITES)
SUITES_BY_KEY: dict[str, Suite] = {s.key: s for s in SUITES}


def unknown_suites(requested: list[str]) -> list[str]:
    """Which of the requested suite keys do not exist. Order preserved."""
    return [s for s in requested if s not in SUITE_KEYS]
