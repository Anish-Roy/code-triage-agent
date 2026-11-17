from __future__ import annotations

from dataclasses import replace

from code_triage_agent.collector.snapshot import LoadedSnapshot
from code_triage_agent.config.loader import LoadedConfig
from code_triage_agent.config.schema import TriggerConfig, WhenConfig
from code_triage_agent.runner.router import Router


def _route(config: LoadedConfig, snapshot: LoadedSnapshot):
    return Router(config).route(snapshot.metadata)


def test_routes_each_configured_source(project_config: LoadedConfig, make_snapshot: object) -> None:
    cases = [
        ("pull_request", "pr_failure", "triage-pr-failure"),
        ("push", "main_failure", "find-culprit"),
        ("schedule", "nightly_failure", "triage-nightly"),
    ]
    for source, trigger_id, skill_id in cases:
        snapshot = make_snapshot(source=source)  # type: ignore[operator]
        route = _route(project_config, snapshot)
        assert route.trigger_id == trigger_id
        assert route.skill_id == skill_id
        assert route.fallback is False
        assert route.allowed_actions == ("comment_on_pr",)
        assert route.skill is not None


def test_unmatched_conclusion_falls_back_to_unknown(
    project_config: LoadedConfig, make_snapshot: object
) -> None:
    snapshot = make_snapshot(conclusion="success")  # type: ignore[operator]

    route = _route(project_config, snapshot)

    assert route.fallback is True
    assert route.skill is None
    assert route.allowed_actions == ("comment_on_pr",)
    assert "no enabled trigger" in route.reason


def test_first_matching_trigger_wins(project_config: LoadedConfig, make_snapshot: object) -> None:
    broad = TriggerConfig(
        id="broad",
        when=WhenConfig(source="any"),
        skill="triage-nightly",
        allowed_actions=["comment_on_pr"],
    )
    narrow = TriggerConfig(
        id="narrow",
        when=WhenConfig(source="pull_request"),
        skill="triage-pr-failure",
        allowed_actions=["comment_on_pr"],
    )
    config = replace(project_config, triggers=(broad, narrow))
    snapshot = make_snapshot(source="pull_request")  # type: ignore[operator]

    route = _route(config, snapshot)

    assert route.trigger_id == "broad"


def test_disabled_trigger_is_skipped(project_config: LoadedConfig, make_snapshot: object) -> None:
    disabled = TriggerConfig(
        id="disabled",
        enabled=False,
        when=WhenConfig(source="any"),
        skill="triage-nightly",
        allowed_actions=["comment_on_pr"],
    )
    config = replace(project_config, triggers=(disabled,))
    snapshot = make_snapshot(source="pull_request")  # type: ignore[operator]

    route = _route(config, snapshot)

    assert route.fallback is True


def test_actions_are_intersected_with_the_phase2_allowlist(
    project_config: LoadedConfig, make_snapshot: object
) -> None:
    trigger = TriggerConfig(
        id="risky",
        when=WhenConfig(source="any"),
        skill="triage-pr-failure",
        allowed_actions=["comment_on_pr", "rerun_failed_jobs", "open_issue", "open_fix_pr"],
    )
    config = replace(project_config, triggers=(trigger,))
    snapshot = make_snapshot()  # type: ignore[operator]

    route = _route(config, snapshot)

    assert route.allowed_actions == ("comment_on_pr",)
    assert route.can_comment is True


def test_missing_skill_falls_back_safely(
    project_config: LoadedConfig, make_snapshot: object
) -> None:
    trigger = TriggerConfig(
        id="broken",
        when=WhenConfig(source="any"),
        skill="does-not-exist",
        allowed_actions=["comment_on_pr"],
    )
    config = replace(project_config, triggers=(trigger,))
    snapshot = make_snapshot()  # type: ignore[operator]

    route = _route(config, snapshot)

    assert route.fallback is True
    assert route.skill is None
    assert "invalid" in route.reason
