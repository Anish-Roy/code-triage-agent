"""Deterministic trigger routing.

Triggers are evaluated in file order; the first enabled match wins. Phase 2
intersects configured actions with the comment-only allowlist, and an unmatched
or invalid configuration falls back to a safe ``unknown`` run with comment-only
permissions.
"""

from __future__ import annotations

from dataclasses import dataclass

from code_triage_agent.config.loader import LoadedConfig
from code_triage_agent.config.schema import BudgetConfig, TriggerConfig
from code_triage_agent.models import SnapshotMetadata
from code_triage_agent.runner.prompt import LoadedSkill, SkillError, load_skill
from code_triage_agent.runner.tools import PHASE2_ACT_TOOLS

FALLBACK_ACTIONS = ("comment_on_pr",)


@dataclass(frozen=True)
class Route:
    trigger_id: str | None
    skill_id: str | None
    skill: LoadedSkill | None
    allowed_actions: tuple[str, ...]
    budget: BudgetConfig
    fallback: bool = False
    reason: str = ""

    @property
    def can_comment(self) -> bool:
        return "comment_on_pr" in self.allowed_actions


class Router:
    def __init__(self, config: LoadedConfig) -> None:
        self._config = config

    def route(self, metadata: SnapshotMetadata) -> Route:
        for trigger in self._config.triggers:
            if not self._matches(trigger, metadata):
                continue
            return self._route_for(trigger)

        return Route(
            trigger_id=None,
            skill_id=None,
            skill=None,
            allowed_actions=FALLBACK_ACTIONS,
            budget=BudgetConfig(),
            fallback=True,
            reason=(
                f"no enabled trigger matched source={metadata.source!r} "
                f"conclusion={metadata.conclusion!r}"
            ),
        )

    def _route_for(self, trigger: TriggerConfig) -> Route:
        try:
            skill = load_skill(self._config, trigger.skill)
        except SkillError as exc:
            return Route(
                trigger_id=trigger.id,
                skill_id=None,
                skill=None,
                allowed_actions=FALLBACK_ACTIONS,
                budget=trigger.budget,
                fallback=True,
                reason=f"trigger {trigger.id!r} is invalid: {exc}",
            )

        allowed = tuple(action for action in trigger.allowed_actions if action in PHASE2_ACT_TOOLS)
        return Route(
            trigger_id=trigger.id,
            skill_id=skill.id,
            skill=skill,
            allowed_actions=allowed,
            budget=trigger.budget,
            reason=f"matched trigger {trigger.id!r}",
        )

    @staticmethod
    def _matches(trigger: TriggerConfig, metadata: SnapshotMetadata) -> bool:
        if not trigger.enabled:
            return False
        if trigger.when.event != "workflow_run":
            return False
        if metadata.conclusion not in trigger.when.conclusion:
            return False
        return trigger.when.source == "any" or trigger.when.source == metadata.source
