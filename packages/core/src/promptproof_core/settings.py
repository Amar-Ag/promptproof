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
    # Measured, not guessed. On the free tier each model allows 8,000 tokens
    # per minute, and a probe costs two calls against two different models:
    # about 140 tokens to the target and about 410 to the judge, the judge
    # being the more expensive because it reasons about the exchange.
    #
    # The judge is therefore the binding constraint at roughly 8000/410 ~= 19
    # probes per minute. Two probes in flight, each taking a few seconds, sits
    # under that; the previous default of 4 put up to eight calls in the air at
    # once and overran the per-minute allowance in bursts, which is how a third
    # of a scan's probes came back errored (#32).
    #
    # This is a floor rather than the whole answer. `TokenBudget` in
    # `provider.py` paces against the allowance the provider reports on every
    # response, so the right rate is discovered at run time for whatever
    # account, model and corpus are actually in use. The constant only has to
    # be low enough not to overshoot before the first response arrives.
    scan_concurrency: int = Field(default=2, ge=1, le=64)
    scan_lease_seconds: int = Field(default=300, ge=30)
    # Pacing bounds. Both were constants in `provider.py`, which sat badly with
    # the argument for pacing in the first place: the right numbers depend on
    # the account, and an account is configuration.
    #
    # The floor is roughly three judge calls, so a call is held back while
    # there is still room for the ones already in flight -- they are not yet
    # counted in the number the provider gave us.
    pacing_floor_tokens: int = Field(default=1500, ge=0)
    # Capped below the lease on purpose. A provider reporting an eight-hour
    # reset must not park a worker for eight hours, and a wait longer than the
    # lease would hand the scan to someone else mid-sleep.
    pacing_max_wait_seconds: float = Field(default=65.0, gt=0)

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

    # ── On-call responder ─────────────────────────────────────────────────────
    # The model it reasons with, kept separate from the judge so changing one is
    # never a silent change to the other.
    # Chosen by what survives a full tool loop, not by size.
    #
    # The responder calls tools and must then return a typed report.
    # `openai/gpt-oss-20b` fails that last step -- Groq rejects the run with
    # `output_parse_failed` after the tool calls -- so it is unusable here
    # despite being the only model nothing else in this service touches.
    # `openai/gpt-oss-120b` works but is the judge's model, and judging is the
    # heaviest consumer of a per-model daily quota. This one completes the
    # drill and leaves the judge's budget alone.
    #
    # Known weakness: on a free tier there is no model this service does not
    # already use, so the responder still shares a daily quota with the
    # workload it responds to. A day busy enough to cause an incident is a day
    # that can starve the responder. Mitigated by sending one runbook rather
    # than two and allowing a single retry; not solved. Override with
    # ONCALL_MODEL if the two ever need separating properly.
    oncall_model: str = "qwen/qwen3.8-27b"
    oncall_repo: str = "Amar-Ag/promptproof"
    # Needs issues:write and nothing else. It cannot deploy, merge or touch code.
    oncall_github_token: str = ""

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
