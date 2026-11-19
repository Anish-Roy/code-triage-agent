"""End-to-end replay/live pipeline for one snapshot.

This is the seam the CLI and (later) the worker share: load snapshot, route,
render prompt, run the bounded loop, post at most one comment, persist a
complete record.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from code_triage_agent.collector.snapshot import LoadedSnapshot, load_snapshot
from code_triage_agent.config.loader import LoadedConfig
from code_triage_agent.config.schema import BudgetConfig
from code_triage_agent.models import LLMMessage, TriageDecision
from code_triage_agent.runner.actions import CommentAction, CommentResult
from code_triage_agent.runner.llm import LLMClient
from code_triage_agent.runner.loop import LoopResult, run_loop, safe_unknown_decision
from code_triage_agent.runner.prompt import TriagePrompt, build_prompt, load_skill
from code_triage_agent.runner.record import RunArtifact, RunRecorder
from code_triage_agent.runner.router import FALLBACK_ACTIONS, Route, Router
from code_triage_agent.runner.tools import ToolContext, ToolRegistry
from code_triage_agent.settings import Settings


@dataclass(frozen=True)
class TriageResult:
    route: Route
    artifact: RunArtifact
    prompt: TriagePrompt | None = None

    @property
    def decision(self) -> TriageDecision:
        return self.artifact.decision


def triage_snapshot(
    *,
    snapshot_dir: Path,
    settings: Settings,
    config: LoadedConfig,
    llm: LLMClient | None = None,
    github: Any = None,
    persist: bool = True,
    skill_override: str | None = None,
    temperature: float = 0.2,
    clock: Callable[[], datetime] | None = None,
) -> TriageResult:
    loaded = load_snapshot(snapshot_dir)
    route = Router(config).route(loaded.metadata)
    if skill_override is not None:
        route = _override_skill(config, route, skill_override)

    database = None
    if persist:
        from code_triage_agent.db import Database

        database = Database(settings.database_path)
        database.migrate()

    recorder = RunRecorder(
        database=database,
        metadata=loaded.metadata,
        trigger_id=route.trigger_id,
        skill_id=route.skill_id,
        skill_version=route.skill.version if route.skill else None,
        snapshot_path=loaded.directory,
        config_sha=config.config_sha,
        clock=clock,
    )
    recorder.start()

    prompt: TriagePrompt | None = None
    try:
        if route.skill is None or llm is None:
            reason = route.reason or "no skill or model is available for this run"
            result = LoopResult(
                decision=safe_unknown_decision(reason),
                status="succeeded",
                error=reason,
            )
        else:
            prompt = build_prompt(loaded, route.skill)
            registry = ToolRegistry(
                allowed_tools=route.skill.allowed_tools,
                context=ToolContext(
                    snapshot=loaded,
                    run_id=loaded.metadata.run_id,
                    dry_run=settings.dry_run,
                    replay=settings.replay_mode or github is None,
                    github=github,
                ),
            )
            result = run_loop(
                llm=llm,
                registry=registry,
                snapshot=loaded,
                messages=[
                    LLMMessage(role="system", content=prompt.system),
                    LLMMessage(role="user", content=prompt.user),
                ],
                budget=_clamp_budget(route.budget, settings),
                timeout_seconds=settings.llm_timeout_seconds,
                temperature=temperature,
            )
    except Exception as exc:  # keep the audit trail even on unexpected failure
        artifact = recorder.finish_failure(f"{type(exc).__name__}: {exc}")
        return TriageResult(route=route, artifact=artifact, prompt=prompt)

    recorder.add_steps(result.steps)
    actions, comment = _run_actions(
        loaded,
        route,
        result,
        settings=settings,
        github=github,
    )
    artifact = recorder.finish(result, actions=actions, comment=comment)
    return TriageResult(route=route, artifact=artifact, prompt=prompt)


def _run_actions(
    loaded: LoadedSnapshot,
    route: Route,
    result: LoopResult,
    *,
    settings: Settings,
    github: Any,
) -> tuple[list[str], CommentResult | None]:
    if not route.can_comment:
        return [], None
    if result.decision.recommended_action.type != "comment":
        return [], None

    action = CommentAction(
        github,
        dry_run=settings.dry_run,
        replay=settings.replay_mode or github is None,
    )
    try:
        comment = action.post(
            repo=loaded.metadata.repo,
            pr_number=loaded.metadata.pr_number,
            decision=result.decision,
            run_id=loaded.metadata.run_id,
            allowed_actions=route.allowed_actions,
            workflow_name=loaded.metadata.workflow_name,
        )
    except Exception as exc:  # an action failure must not lose the run record
        comment = CommentResult(
            posted=False,
            body="",
            skipped_reason=f"comment failed: {type(exc).__name__}: {exc}",
        )
    return ["comment_on_pr"], comment


def _clamp_budget(budget: BudgetConfig, settings: Settings) -> BudgetConfig:
    return BudgetConfig(
        max_steps=min(budget.max_steps, settings.max_steps),
        max_cost_usd=min(budget.max_cost_usd, settings.max_cost_usd),
        max_seconds=min(budget.max_seconds, settings.max_seconds),
    )


def _override_skill(config: LoadedConfig, route: Route, skill_id: str) -> Route:
    skill = load_skill(config, skill_id)
    return Route(
        trigger_id=route.trigger_id,
        skill_id=skill.id,
        skill=skill,
        allowed_actions=route.allowed_actions or FALLBACK_ACTIONS,
        budget=route.budget,
        fallback=False,
        reason=f"skill override: {skill.id}",
    )


__all__ = ["TriageResult", "triage_snapshot"]
