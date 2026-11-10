"""The models a target may be run against.

This is the chat-capable subset of the Groq catalogue, verified against a live
account. Groq's catalogue changes, and it differs between accounts, so a model
disappearing here shows up as a 404 at scan time. Refresh the list from
`GET https://api.groq.com/openai/v1/models` when that happens.

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
    ModelChoice(id="qwen/qwen3.8-27b", label="Qwen 3.8 27B", family="qwen"),
    ModelChoice(id="openai/gpt-oss-120b", label="GPT-OSS 120B", family="openai"),
    ModelChoice(id="openai/gpt-oss-20b", label="GPT-OSS 20B", family="openai"),
)

TARGET_MODEL_IDS: tuple[str, ...] = tuple(m.id for m in TARGET_MODELS)
MODELS_BY_ID: dict[str, ModelChoice] = {m.id: m for m in TARGET_MODELS}
