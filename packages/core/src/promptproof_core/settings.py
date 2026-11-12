"""Configuration, read once from the environment.

Every tunable in this project lives here. Modules take a `Settings` instance; they
do not read `os.environ` themselves, and they do not hard-code timeouts or limits.
"""

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

Environment = Literal["local", "test", "dev", "production"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
    )

    environment: Environment = "local"

    # ── Database ──────────────────────────────────────────────────────────────
    database_url: str = "postgresql+asyncpg://promptproof:promptproof@db:5432/promptproof"
    db_pool_size: int = Field(default=5, ge=1, le=50)
    db_pool_max_overflow: int = Field(default=5, ge=0, le=50)

    # ── Models ────────────────────────────────────────────────────────────────
    groq_api_key: str = ""
    default_target_model: str = "qwen/qwen3.8-27b"
    judge_model: str = "openai/gpt-oss-120b"
    model_timeout_seconds: float = Field(default=60.0, gt=0)
    model_max_retries: int = Field(default=3, ge=0, le=10)

    # ── Scan execution ────────────────────────────────────────────────────────
    scan_concurrency: int = Field(default=4, ge=1, le=64)
    scan_lease_seconds: int = Field(default=300, ge=30)
    scan_max_attempts: int = Field(default=3, ge=1, le=10)
    worker_poll_seconds: float = Field(default=2.0, gt=0)
    worker_id: str = "worker-1"

    # ── Telemetry ─────────────────────────────────────────────────────────────
    logfire_token: str = ""
    otel_exporter_otlp_endpoint: str = ""
    otel_service_name: str = "promptproof"
    log_level: str = "INFO"

    # ── Alerting ──────────────────────────────────────────────────────────────
    # How often the rules are measured. The sustain windows do the smoothing,
    # so this only needs to be frequent enough not to blur them.
    alert_eval_interval_seconds: float = Field(default=30.0, gt=0)

    # Where a firing alert goes. Empty means evaluate but do not deliver, which
    # is what local development and the tests run as.
    alert_oncall_webhook_url: str = ""
    alert_human_webhook_url: str = ""

    # ── Security ──────────────────────────────────────────────────────────────
    ssrf_allow_private: bool = False
    oncall_webhook_secret: str = ""

    # ── API ───────────────────────────────────────────────────────────────────
    cors_allow_origins: list[str] = ["http://localhost:5173"]

    # Where the built frontend lives, when the API serves it from its own origin
    # (ADR-0005). Empty under Compose, where the frontend is its own container.
    frontend_dir: str = ""

    def assert_judge_differs(self) -> None:
        """A model must never grade its own output.

        Compares model *families* (the part before the first `/`), because
        `qwen/qwen3.8-27b` judging `qwen/qwen3-8b` is still self-evaluation.
        """
        target_family = self.default_target_model.split("/", 1)[0].lower()
        judge_family = self.judge_model.split("/", 1)[0].lower()
        if target_family == judge_family:
            raise ValueError(
                f"JUDGE_MODEL ({self.judge_model}) and DEFAULT_TARGET_MODEL "
                f"({self.default_target_model}) are the same model family "
                f"({target_family!r}). A model must not grade its own output."
            )

    def assert_production_safe(self) -> None:
        """Refuse to start in production with a development-only escape hatch on."""
        if self.environment == "production" and self.ssrf_allow_private:
            raise ValueError("SSRF_ALLOW_PRIVATE must be false in production.")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
