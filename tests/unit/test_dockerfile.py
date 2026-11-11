"""The Dockerfile's stage order is load-bearing.

Render builds a Dockerfile's final stage; it has no `--target`. So `serve`
being last is not tidiness, it is what makes the deployment build the right
image. This project has already shipped one defect from exactly this — adding
a `test` stage at the end silently made the services build it — so the
invariant is asserted rather than written in a comment.
"""

import re
from pathlib import Path

DOCKERFILE = Path(__file__).resolve().parents[2] / "Dockerfile"

STAGE = re.compile(r"^FROM\s+\S+(?:\s+AS\s+(?P<name>\S+))?\s*$", re.MULTILINE | re.IGNORECASE)


def stages() -> list[str]:
    return [m.group("name") for m in STAGE.finditer(DOCKERFILE.read_text(encoding="utf-8"))]


def test_every_stage_is_named() -> None:
    """An unnamed stage cannot be targeted, and makes the order harder to read."""
    assert all(stages()), "every FROM should carry an AS <name>"


def test_serve_is_the_final_stage() -> None:
    """Render builds the last stage. If this moves, deployment builds the wrong image."""
    assert stages()[-1] == "serve", (
        f"last stage is {stages()[-1]!r}; Render would deploy that instead of 'serve'"
    )


def test_the_expected_stages_exist() -> None:
    assert set(stages()) == {"runtime", "test", "web-build", "serve"}
