"""Replay metrics, the composite score, and the separate hard gates."""

from __future__ import annotations

import statistics
from collections import Counter
from collections.abc import Sequence

from code_triage_agent.models import CaseReport, GateResult, ReplayMetrics, ReplayReport

WEIGHTS = {
    "accuracy": 0.45,
    "false_flaky": 0.25,
    "action_precision": 0.20,
    "cost_efficiency": 0.10,
}
COST_GATE_MULTIPLIER = 1.10


def compute_metrics(cases: Sequence[CaseReport], *, budget_usd: float) -> ReplayMetrics:
    total = len(cases)
    correct = sum(1 for case in cases if case.label == "correct")
    accuracy = correct / total if total else 0.0

    per_skill: dict[str, list[bool]] = {}
    per_category: dict[str, list[bool]] = {}
    confusion: dict[str, dict[str, int]] = {}
    false_flaky = 0
    non_flaky = 0
    actions_attempted = 0
    actions_succeeded = 0
    costs: list[float] = []
    reasons: Counter[str] = Counter()

    for case in cases:
        skill = case.skill_id or "unrouted"
        per_skill.setdefault(skill, []).append(case.label == "correct")
        per_category.setdefault(case.expected_category, []).append(case.label == "correct")
        row = confusion.setdefault(case.expected_category, {})
        row[case.predicted_category] = row.get(case.predicted_category, 0) + 1

        if case.expected_category != "flaky_test":
            non_flaky += 1
            if case.predicted_category == "flaky_test":
                false_flaky += 1

        if case.predicted_action != "none":
            actions_attempted += 1
            if case.action_correct and case.status == "succeeded":
                actions_succeeded += 1

        costs.append(case.cost_usd)
        if case.failure_reason:
            reasons[case.failure_reason] += 1

    false_flaky_rate = false_flaky / non_flaky if non_flaky else 0.0
    action_precision = actions_succeeded / actions_attempted if actions_attempted else 1.0
    total_cost = sum(costs)
    median_cost = statistics.median(costs) if costs else 0.0
    cost_efficiency = _cost_efficiency(budget_usd, total_cost)

    composite = (
        WEIGHTS["accuracy"] * accuracy
        + WEIGHTS["false_flaky"] * (1.0 - false_flaky_rate)
        + WEIGHTS["action_precision"] * action_precision
        + WEIGHTS["cost_efficiency"] * cost_efficiency
    )

    return ReplayMetrics(
        total_cases=total,
        correct_cases=correct,
        accuracy=round(accuracy, 6),
        per_skill_accuracy={
            key: round(sum(values) / len(values), 6)
            for key, values in sorted(per_skill.items())
            if values
        },
        per_category_accuracy={
            key: round(sum(values) / len(values), 6)
            for key, values in sorted(per_category.items())
            if values
        },
        confusion_matrix={key: dict(sorted(row.items())) for key, row in sorted(confusion.items())},
        false_flaky_count=false_flaky,
        non_flaky_cases=non_flaky,
        false_flaky_rate=round(false_flaky_rate, 6),
        actions_attempted=actions_attempted,
        actions_succeeded=actions_succeeded,
        action_precision=round(action_precision, 6),
        median_cost_usd=round(median_cost, 6),
        total_cost_usd=round(total_cost, 6),
        cost_efficiency=round(cost_efficiency, 6),
        composite_score=round(composite, 6),
        failure_reasons=dict(sorted(reasons.items())),
    )


def evaluate_gates(
    report: ReplayReport,
    *,
    baseline: ReplayMetrics | None,
    budget_usd: float,
    config_guard_passed: bool = True,
) -> GateResult:
    """Evaluate hard gates separately from the composite score.

    A good average must never hide a new false-flaky case or an accuracy
    regression, so these checks are not weighted.
    """
    checks: dict[str, bool] = {}
    failures: list[str] = []
    current = report.metrics

    if baseline is None:
        checks["baseline_available"] = True
        checks["no_new_false_flaky"] = True
        checks["no_accuracy_regression"] = True
        checks["cost_within_10_percent"] = True
    else:
        checks["baseline_available"] = True
        checks["no_new_false_flaky"] = current.false_flaky_count <= baseline.false_flaky_count
        if not checks["no_new_false_flaky"]:
            failures.append(
                "new false-flaky cases: "
                f"{current.false_flaky_count} > baseline {baseline.false_flaky_count}"
            )

        checks["no_accuracy_regression"] = current.accuracy + 1e-9 >= baseline.accuracy
        if not checks["no_accuracy_regression"]:
            failures.append(
                f"accuracy regressed: {current.accuracy:.4f} < baseline {baseline.accuracy:.4f}"
            )

        if baseline.total_cost_usd > 0:
            limit = baseline.total_cost_usd * COST_GATE_MULTIPLIER
            checks["cost_within_10_percent"] = current.total_cost_usd <= limit + 1e-9
        else:
            checks["cost_within_10_percent"] = current.total_cost_usd <= max(budget_usd, 0.0)
        if not checks["cost_within_10_percent"]:
            failures.append(
                f"cost increased beyond 10%: {current.total_cost_usd:.4f} > "
                f"{baseline.total_cost_usd * COST_GATE_MULTIPLIER:.4f}"
            )

    checks["within_budget"] = current.total_cost_usd <= budget_usd + 1e-9
    if not checks["within_budget"]:
        failures.append(f"eval cost {current.total_cost_usd:.4f} exceeds budget {budget_usd:.4f}")

    checks["config_guard"] = config_guard_passed
    if not config_guard_passed:
        failures.append("config guard rejected the evaluated config")

    return GateResult(passed=all(checks.values()), failures=failures, checks=checks)


def render_markdown(report: ReplayReport) -> str:
    metrics = report.metrics
    lines = [
        f"# Replay report: `{report.set_name}`",
        "",
        f"- Run at: {report.run_at.isoformat()}",
        f"- Config SHA: `{report.config_sha}`",
        f"- Model: `{report.model}`",
        f"- Cases: {metrics.total_cases}",
        f"- Composite score: **{metrics.composite_score:.4f}**",
        "",
        "## Metrics",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| Accuracy | {metrics.accuracy:.2%} ({metrics.correct_cases}/{metrics.total_cases}) |",
        f"| False-flaky rate | {metrics.false_flaky_rate:.2%} "
        f"({metrics.false_flaky_count}/{metrics.non_flaky_cases} non-flaky cases) |",
        f"| Action precision | {metrics.action_precision:.2%} "
        f"({metrics.actions_succeeded}/{metrics.actions_attempted}) |",
        f"| Median cost | ${metrics.median_cost_usd:.4f} |",
        f"| Total cost | ${metrics.total_cost_usd:.4f} |",
        f"| Cost efficiency | {metrics.cost_efficiency:.2%} |",
        "",
        "### Accuracy by skill",
        "",
    ]
    if metrics.per_skill_accuracy:
        lines.extend(["| Skill | Accuracy |", "|---|---|"])
        lines.extend(
            f"| `{skill}` | {value:.2%} |" for skill, value in metrics.per_skill_accuracy.items()
        )
    else:
        lines.append("_No skill data._")

    lines.extend(["", "### Accuracy by expected category", ""])
    if metrics.per_category_accuracy:
        lines.extend(["| Category | Accuracy |", "|---|---|"])
        lines.extend(
            f"| `{category}` | {value:.2%} |"
            for category, value in metrics.per_category_accuracy.items()
        )
    else:
        lines.append("_No category data._")

    lines.extend(["", "## Confusion matrix", ""])
    lines.extend(_confusion_table(report))

    lines.extend(["", "## Mismatches", ""])
    mismatches = [case for case in report.cases if case.label != "correct"]
    if not mismatches:
        lines.append("_Every case matched its expected category._")
    for case in mismatches:
        lines.extend(
            [
                f"### `{case.case_id}`",
                "",
                f"- Expected: `{case.expected_category}` / `{case.expected_action}`",
                f"- Predicted: `{case.predicted_category}` / `{case.predicted_action}`",
                f"- Label: `{case.label}`",
                f"- Status: `{case.status}`",
                f"- Failure reason: {case.failure_reason or '(none recorded)'}",
            ]
        )
        if case.evidence:
            lines.extend(["", "Evidence:"])
            for item in case.evidence:
                lines.append(f"- `{item.get('quote', '')}` ({item.get('source', '')})")
        lines.append("")

    lines.extend(["", "## Hard gates", ""])
    if report.gates is None:
        lines.append("_Gates were not evaluated._")
    else:
        lines.append(f"**Passed:** {'yes' if report.gates.passed else 'no'}")
        for name, passed in report.gates.checks.items():
            lines.append(f"- `{name}`: {'pass' if passed else 'FAIL'}")
        for failure in report.gates.failures:
            lines.append(f"- ❌ {failure}")

    return "\n".join(lines).strip() + "\n"


def _confusion_table(report: ReplayReport) -> list[str]:
    expected = sorted(report.metrics.confusion_matrix)
    predicted = sorted(
        {category for row in report.metrics.confusion_matrix.values() for category in row}
    )
    if not expected or not predicted:
        return ["_No confusion data._"]
    header = "| expected \\ predicted | " + " | ".join(f"`{item}`" for item in predicted) + " |"
    separator = "|---|" + "---|" * len(predicted)
    rows = [header, separator]
    for category in expected:
        values = report.metrics.confusion_matrix.get(category, {})
        rows.append(
            f"| `{category}` | " + " | ".join(str(values.get(item, 0)) for item in predicted) + " |"
        )
    return rows


def _cost_efficiency(budget_usd: float, total_cost: float) -> float:
    if total_cost <= 0:
        return 1.0
    if budget_usd <= 0:
        return 0.0
    return min(1.0, budget_usd / total_cost)


__all__ = [
    "COST_GATE_MULTIPLIER",
    "WEIGHTS",
    "compute_metrics",
    "evaluate_gates",
    "render_markdown",
]
