"""The models a target may be run against.

An allow-list rather than free text: an unrecognised model would otherwise fail
deep inside the worker, minutes after the user submitted the scan, instead of at
the point they typed it.
"""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ModelChoice:
    id: str
    label: str
    family: str


TARGET_MODELS: tuple[ModelChoice, ...] = (
    ModelChoice(id="qwen/qwen3.6-27b", label="Qwen 3.6 27B", family="qwen"),
    ModelChoice(id="llama-3.3-70b-versatile", label="Llama 3.3 70B", family="llama"),
    ModelChoice(id="llama-3.1-8b-instant", label="Llama 3.1 8B Instant", family="llama"),
    ModelChoice(id="openai/gpt-oss-120b", label="GPT-OSS 120B", family="openai"),
    ModelChoice(id="openai/gpt-oss-20b", label="GPT-OSS 20B", family="openai"),
)

TARGET_MODEL_IDS: tuple[str, ...] = tuple(m.id for m in TARGET_MODELS)
MODELS_BY_ID: dict[str, ModelChoice] = {m.id: m for m in TARGET_MODELS}
