"""Configuration assertions that run at startup.

Both of these are failures we would rather have at boot than discover from a
report that is quietly meaningless.
"""

import pytest
from promptproof_core.settings import Settings


def make(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "default_target_model": "qwen/qwen3.8-27b",
        "judge_model": "openai/gpt-oss-120b",
        "environment": "local",
        "ssrf_allow_private": False,
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def test_different_families_are_accepted() -> None:
    make().assert_judge_differs()


def test_identical_model_is_rejected() -> None:
    settings = make(judge_model="qwen/qwen3.8-27b")
    with pytest.raises(ValueError, match="same model family"):
        settings.assert_judge_differs()


def test_same_family_different_size_is_still_rejected() -> None:
    """A model must not grade its own output, and a sibling is not independent."""
    settings = make(default_target_model="qwen/qwen3.8-27b", judge_model="qwen/qwen3-8b")
    with pytest.raises(ValueError, match="same model family"):
        settings.assert_judge_differs()


def test_family_comparison_ignores_case() -> None:
    settings = make(default_target_model="Qwen/Qwen3.6-27b", judge_model="qwen/qwen3-8b")
    with pytest.raises(ValueError, match="same model family"):
        settings.assert_judge_differs()


def test_production_refuses_the_ssrf_escape_hatch() -> None:
    settings = make(environment="production", ssrf_allow_private=True)
    with pytest.raises(ValueError, match="SSRF_ALLOW_PRIVATE"):
        settings.assert_production_safe()


def test_local_may_allow_private_addresses() -> None:
    make(environment="local", ssrf_allow_private=True).assert_production_safe()
