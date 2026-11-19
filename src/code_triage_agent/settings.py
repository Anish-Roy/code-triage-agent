"""Application settings loaded from the environment."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration.

    All variables use the ``CTA_`` prefix, for example ``CTA_DRY_RUN=false``.
    The webhook server fails closed when the webhook secret is missing; it never
    accepts unsigned deliveries.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="CTA_",
        extra="ignore",
        case_sensitive=False,
    )

    github_app_id: int | None = None
    github_app_private_key: SecretStr | None = None
    github_installation_id: int | None = None
    github_webhook_secret: SecretStr | None = None

    llm_provider: str = "anthropic"
    llm_model: str = ""
    llm_api_key: SecretStr | None = None
    # Pricing is expressed in USD per million tokens so cost can be audited
    # per run without asking the provider for a bill.
    llm_input_cost_per_mtok: float = Field(default=3.0, ge=0.0)
    llm_output_cost_per_mtok: float = Field(default=15.0, ge=0.0)
    llm_timeout_seconds: float = Field(default=60.0, gt=0, le=600)

    config_dir: Path = Path("config")
    database_path: Path = Path("var/triage.db")
    snapshot_root: Path = Path("var/snapshots")
    event_root: Path = Path("var/events")

    # Phase 3 eval paths and cost ceiling. Reports and the provider cache are
    # writable; evals/frozen is read-only to the harness.
    eval_frozen_dir: Path = Path("evals/frozen")
    eval_reports_dir: Path = Path("evals/reports")
    eval_cache_dir: Path = Path("evals/cache")
    eval_budget_usd: float = Field(default=15.0, gt=0)
    eval_smoke_cases: int = Field(default=5, ge=1, le=20)

    dry_run: bool = True
    replay_mode: bool = False
    auto_merge_memory: bool = False

    max_steps: int = Field(default=12, ge=1, le=50)
    max_cost_usd: float = Field(default=0.50, gt=0)
    max_seconds: int = Field(default=180, ge=1, le=3600)

    log_level: str = "INFO"

    redaction_patterns: list[str] = Field(default_factory=list)

    @field_validator("github_app_private_key", mode="before")
    @classmethod
    def _expand_private_key_newlines(cls, value: object) -> object:
        # Environment variables often carry the PEM as one line with literal \n.
        if isinstance(value, str):
            return value.replace("\\n", "\n")
        return value

    @field_validator("log_level")
    @classmethod
    def _normalize_log_level(cls, value: str) -> str:
        return value.upper()

    def webhook_configured(self) -> bool:
        return self.github_webhook_secret is not None

    def github_app_configured(self) -> bool:
        return (
            self.github_app_id is not None
            and self.github_app_private_key is not None
            and self.github_installation_id is not None
        )

    def ensure_runtime_dirs(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.snapshot_root.mkdir(parents=True, exist_ok=True)
        self.event_root.mkdir(parents=True, exist_ok=True)
