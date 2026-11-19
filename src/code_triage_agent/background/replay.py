"""Replay frozen cases through the real Phase 2 runner and write reports.

The harness is read-only with respect to the eval set. Reports go to a writable
directory (``evals/reports`` by default), and a case's frozen directory is never
opened for writing.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from code_triage_agent.background.metrics import compute_metrics, evaluate_gates, render_markdown
from code_triage_agent.config.loader import LoadedConfig, load_config
from code_triage_agent.evals.cases import (
    CaseError,
    assert_not_frozen,
    case_snapshot_dir,
    load_case_set,
)
from code_triage_agent.models import CaseReport, EvalCase, ReplayReport
from code_triage_agent.runner.llm import LLMClient, Pricing, load_script
from code_triage_agent.runner.pipeline import triage_snapshot
from code_triage_agent.runner.provider import build_llm
from code_triage_agent.settings import Settings

CaseLLMFactory = Callable[[Path, EvalCase, Settings], LLMClient | None]


@dataclass(frozen=True)
class ReplayOutcome:
    report: ReplayReport
    json_path: Path
    markdown_path: Path


def run_replay(
    *,
    set_dir: Path,
    settings: Settings,
    config_dir: Path | None = None,
    report_dir: Path | None = None,
    case_ids: Sequence[str] = (),
    smoke: bool = False,
    allow_live: bool = False,
    llm_factory: CaseLLMFactory | None = None,
    baseline_path: Path | None = None,
    project_root: Path | None = None,
    config_guard_passed: bool = True,
    now: datetime | None = None,
) -> ReplayOutcome:
    """Run a set of cases and persist JSON and Markdown reports.

    ``llm_factory`` is the test seam for a deterministic policy model. The
    default factory uses ``<case>/llm_script.json`` when present, otherwise the
    live provider (only when ``allow_live`` and credentials exist).
    """
    started = time.monotonic()
    run_at = now or datetime.now().astimezone()
    set_dir = set_dir.resolve()
    report_root = (report_dir or settings.eval_reports_dir).resolve()
    frozen_root = settings.eval_frozen_dir.resolve()
    assert_not_frozen(report_root, frozen_root)
    if report_root == set_dir:
        raise CaseError("report directory must not be the eval set directory")

    config_path = (config_dir or settings.config_dir).resolve()
    config = load_config(config_path)
    cases = _select_cases(
        set_dir,
        case_ids=case_ids,
        smoke=smoke,
        limit=settings.eval_smoke_cases,
        project_root=project_root,
    )
    if not cases:
        raise CaseError(f"no cases selected from {set_dir}")

    live_llm = None
    if allow_live:
        live_llm = build_llm(settings, cache_dir=settings.eval_cache_dir)
        if live_llm is None:
            raise CaseError("--live requires CTA_LLM_API_KEY")
    factory = llm_factory or _default_factory(live_llm, settings)

    case_reports: list[CaseReport] = []
    models: set[str] = set()
    for case_dir, case in cases:
        case_report, model = _run_case(
            case_dir,
            case,
            settings=settings,
            config=config,
            factory=factory,
            project_root=project_root,
        )
        case_reports.append(case_report)
        if model:
            models.add(model)

    metrics = compute_metrics(case_reports, budget_usd=settings.eval_budget_usd)
    baseline_metrics = None
    if baseline_path is not None and baseline_path.is_file():
        baseline = ReplayReport.model_validate_json(baseline_path.read_text(encoding="utf-8"))
        baseline_metrics = baseline.metrics

    report = ReplayReport(
        run_at=run_at,
        config_sha=config.config_sha,
        set_name=set_dir.name,
        model=",".join(sorted(models)) if models else "scripted",
        cases=case_reports,
        metrics=metrics,
        skill_versions=_skill_versions(config),
        budget_usd=settings.eval_budget_usd,
        runtime_ms=max(int((time.monotonic() - started) * 1000), 0),
        baseline=baseline_path.name if baseline_path is not None else None,
    )
    report.gates = evaluate_gates(
        report,
        baseline=baseline_metrics,
        budget_usd=settings.eval_budget_usd,
        config_guard_passed=config_guard_passed,
    )

    report_root.mkdir(parents=True, exist_ok=True)
    stamp = run_at.strftime("%Y%m%dT%H%M%S")
    json_path = report_root / f"replay-{report.set_name}-{stamp}.json"
    markdown_path = report_root / f"replay-{report.set_name}-{stamp}.md"
    json_path.write_text(
        json.dumps(report.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    markdown_path.write_text(render_markdown(report), encoding="utf-8")
    return ReplayOutcome(report=report, json_path=json_path, markdown_path=markdown_path)


def _select_cases(
    set_dir: Path,
    *,
    case_ids: Sequence[str],
    smoke: bool,
    limit: int,
    project_root: Path | None,
) -> list[tuple[Path, EvalCase]]:
    loaded = load_case_set(set_dir, project_root=project_root)
    if case_ids:
        wanted = set(case_ids)
        selected = [(path, case) for path, case in loaded if case.case_id in wanted]
        missing = wanted - {case.case_id for _, case in selected}
        if missing:
            raise CaseError(f"unknown case id(s): {', '.join(sorted(missing))}")
        return selected
    if smoke:
        tagged = [(path, case) for path, case in loaded if "smoke" in case.tags]
        return (tagged or loaded)[:limit]
    return loaded


def _run_case(
    case_dir: Path,
    case: EvalCase,
    *,
    settings: Settings,
    config: LoadedConfig,
    factory: CaseLLMFactory,
    project_root: Path | None,
) -> tuple[CaseReport, str | None]:
    started = time.monotonic()
    try:
        snapshot_dir = case_snapshot_dir(case_dir, case, project_root=project_root)
    except CaseError as exc:
        return (
            CaseReport(
                case_id=case.case_id,
                expected_category=case.expected_category,
                predicted_category="error",
                expected_action=case.expected_action,
                predicted_action="none",
                label="error",
                failure_reason=str(exc),
            ),
            None,
        )

    llm = factory(case_dir, case, settings)
    if llm is None:
        return (
            CaseReport(
                case_id=case.case_id,
                expected_category=case.expected_category,
                predicted_category="error",
                expected_action=case.expected_action,
                predicted_action="none",
                label="error",
                failure_reason=(
                    "no LLM available: add llm_script.json to the case or configure "
                    "CTA_LLM_API_KEY and pass --live"
                ),
            ),
            None,
        )

    replay_settings = settings.model_copy(update={"replay_mode": True, "dry_run": True})
    try:
        result = triage_snapshot(
            snapshot_dir=snapshot_dir,
            settings=replay_settings,
            config=config,
            llm=llm,
            persist=False,
            skill_override=case.expected_skill,
            temperature=0.0,
        )
    except Exception as exc:  # a broken case becomes an error row, not a crash
        return (
            CaseReport(
                case_id=case.case_id,
                expected_category=case.expected_category,
                predicted_category="error",
                expected_action=case.expected_action,
                predicted_action="none",
                label="error",
                failure_reason=f"{type(exc).__name__}: {exc}",
                runtime_ms=max(int((time.monotonic() - started) * 1000), 0),
            ),
            getattr(llm, "model", None),
        )

    decision = result.artifact.decision
    predicted_category = decision.category
    predicted_action = decision.recommended_action.type
    category_correct = predicted_category == case.expected_category
    action_correct = predicted_action == case.expected_action
    failure_reason = None
    if not category_correct:
        failure_reason = f"expected category {case.expected_category!r}, got {predicted_category!r}"
    elif not action_correct:
        failure_reason = f"expected action {case.expected_action!r}, got {predicted_action!r}"

    return (
        CaseReport(
            case_id=case.case_id,
            skill_id=result.route.skill_id,
            expected_category=case.expected_category,
            predicted_category=predicted_category,
            expected_action=case.expected_action,
            predicted_action=predicted_action,
            label="correct" if category_correct else "incorrect",
            action_correct=action_correct,
            cost_usd=round(result.artifact.cost_usd, 6),
            runtime_ms=max(int((time.monotonic() - started) * 1000), 0),
            status=result.artifact.status,
            failure_reason=failure_reason,
            evidence=[item.model_dump(mode="json") for item in decision.evidence],
        ),
        getattr(llm, "model", None),
    )


def _default_factory(live_llm: LLMClient | None, settings: Settings) -> CaseLLMFactory:
    def factory(case_dir: Path, case: EvalCase, _settings: Settings) -> LLMClient | None:
        # An explicit live run wins so a prompt change can be observed even when
        # the case also ships a scripted turn.
        if live_llm is not None:
            return live_llm
        script = case_dir / "llm_script.json"
        if script.is_file():
            return load_script(
                script,
                pricing=Pricing(
                    input_per_mtok=settings.llm_input_cost_per_mtok,
                    output_per_mtok=settings.llm_output_cost_per_mtok,
                ),
            )
        return None

    return factory


def _skill_versions(config: LoadedConfig) -> dict[str, str]:
    versions: dict[str, str] = {}
    for skill_id in sorted(config.skills):
        path = config.config_dir / "skills" / f"{skill_id}.md"
        if path.is_file():
            versions[skill_id] = hashlib.sha256(path.read_bytes()).hexdigest()
    return versions


def load_baseline(path: Path) -> ReplayReport:
    return ReplayReport.model_validate_json(path.read_text(encoding="utf-8"))


def summarize(report: ReplayReport) -> dict[str, Any]:
    """Compact payload for the CLI's ``--json`` mode."""
    return {
        "set_name": report.set_name,
        "config_sha": report.config_sha,
        "model": report.model,
        "accuracy": report.metrics.accuracy,
        "composite_score": report.metrics.composite_score,
        "false_flaky_rate": report.metrics.false_flaky_rate,
        "action_precision": report.metrics.action_precision,
        "median_cost_usd": report.metrics.median_cost_usd,
        "gates": report.gates.model_dump(mode="json") if report.gates else None,
    }


__all__ = [
    "CaseLLMFactory",
    "ReplayOutcome",
    "load_baseline",
    "run_replay",
    "summarize",
]
