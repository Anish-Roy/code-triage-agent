"""Schemas for triggers, skills, and memory notes."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

KNOWN_ACTIONS = frozenset(
    {
        "comment_on_pr",
        "rerun_failed_jobs",
        "open_fix_pr",
        "open_issue",
    }
)

KNOWN_TOOLS = frozenset(
    {
        "read_file",
        "git_log",
        "git_diff",
        "search_memory",
        "comment_on_pr",
        "rerun_failed_jobs",
        "open_fix_pr",
        "open_issue",
    }
)

CATEGORIES = frozenset(
    {
        "flaky_test",
        "infra_issue",
        "dependency_or_config",
        "real_bug",
        "unknown",
    }
)

TriggerSource = Literal["pull_request", "push", "schedule", "any"]


class BudgetConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_steps: int = Field(default=12, ge=1, le=50)
    max_cost_usd: float = Field(default=0.50, gt=0)
    max_seconds: int = Field(default=180, ge=1, le=3600)


class WhenConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event: Literal["workflow_run"] = "workflow_run"
    conclusion: list[str] = Field(default_factory=lambda: ["failure", "timed_out"], min_length=1)
    source: TriggerSource = "any"


class TriggerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    enabled: bool = True
    when: WhenConfig
    skill: str
    allowed_actions: list[str] = Field(default_factory=list)
    budget: BudgetConfig = Field(default_factory=BudgetConfig)


class SkillMeta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-z][a-z0-9-]*$")
    version: int = Field(ge=1)
    allowed_tools: list[str] = Field(min_length=1)
    output_schema: str = "submit_triage"
    body: str


class MemoryNote(BaseModel):
    model_config = ConfigDict(extra="forbid")

    signature: str = Field(min_length=1)
    repo: str | None = None
    note: str = Field(min_length=1)
    confidence: Literal["low", "medium", "high"] = "medium"
    evidence_runs: list[str] = Field(default_factory=list)
    created_at: datetime
    source: Literal["human", "agent"] = "human"
