from __future__ import annotations

from datetime import UTC, datetime

from code_triage_agent.background.metrics import (
    compute_metrics,
    evaluate_gates,
    render_markdown,
)
from code_triage_agent.models import CaseReport, ReplayMetrics, ReplayReport


def _case(
    case_id: str,
    *,
    expected: str,
    predicted: str,
    action: str = "comment",
    action_correct: bool = True,
    cost: float = 0.1,
    skill: str = "triage-pr-failure",
    label: str | None = None,
) -> CaseReport:
    return CaseReport(
        case_id=case_id,
        skill_id=skill,
        expected_category=expected,
        predicted_category=predicted,
        expected_action="comment",
        predicted_action=action,
        label=label or ("correct" if expected == predicted else "incorrect"),
        action_correct=action_correct,
        cost_usd=cost,
        runtime_ms=10,
        status="succeeded",
        failure_reason=None if expected == predicted else "mismatch",
    )


def _report(cases: list[CaseReport], *, budget: float = 1.0) -> ReplayReport:
    metrics = compute_metrics(cases, budget_usd=budget)
    return ReplayReport(
        run_at=datetime(2026, 11, 18, tzinfo=UTC),
        config_sha="a" * 64,
        set_name="frozen",
        model="test",
        cases=cases,
        metrics=metrics,
        skill_versions={"triage-pr-failure": "b" * 64},
        budget_usd=budget,
    )


def test_compute_metrics_includes_confusion_and_false_flaky() -> None:
    cases = [
        _case("a", expected="real_bug", predicted="real_bug", cost=0.1),
        _case("b", expected="flaky_test", predicted="flaky_test", cost=0.2),
        _case("c", expected="real_bug", predicted="flaky_test", cost=0.3),
        _case("d", expected="infra_issue", predicted="infra_issue", cost=0.4),
    ]

    metrics = compute_metrics(cases, budget_usd=1.0)

    assert metrics.total_cases == 4
    assert metrics.correct_cases == 3
    assert metrics.accuracy == 0.75
    assert metrics.confusion_matrix["real_bug"]["real_bug"] == 1
    assert metrics.confusion_matrix["real_bug"]["flaky_test"] == 1
    assert metrics.false_flaky_count == 1
    assert metrics.non_flaky_cases == 3
    assert metrics.false_flaky_rate == 0.333333
    assert metrics.actions_attempted == 4
    assert metrics.actions_succeeded == 4
    assert metrics.action_precision == 1.0
    assert metrics.median_cost_usd == 0.25
    assert metrics.total_cost_usd == 1.0
    assert metrics.cost_efficiency == 1.0
    assert 0.0 < metrics.composite_score < 1.0


def test_action_precision_counts_failed_actions() -> None:
    cases = [
        _case("a", expected="real_bug", predicted="real_bug"),
        _case("b", expected="real_bug", predicted="real_bug", action="none", action_correct=False),
    ]

    metrics = compute_metrics(cases, budget_usd=1.0)

    assert metrics.actions_attempted == 1
    assert metrics.actions_succeeded == 1
    assert metrics.action_precision == 1.0


def test_cost_efficiency_uses_the_budget() -> None:
    cases = [_case("a", expected="real_bug", predicted="real_bug", cost=2.0)]

    metrics = compute_metrics(cases, budget_usd=1.0)

    assert metrics.cost_efficiency == 0.5


def test_zero_budget_or_cost_is_safe() -> None:
    cases = [_case("a", expected="real_bug", predicted="real_bug", cost=0.0)]

    metrics = compute_metrics(cases, budget_usd=0.0)

    assert metrics.cost_efficiency == 1.0


def test_gates_pass_without_a_baseline_and_within_budget() -> None:
    report = _report([_case("a", expected="real_bug", predicted="real_bug", cost=0.1)])
    gates = evaluate_gates(report, baseline=None, budget_usd=1.0)

    assert gates.passed is True
    assert gates.failures == []


def test_gate_fails_on_new_false_flaky() -> None:
    baseline = ReplayMetrics(false_flaky_count=0, accuracy=0.75, total_cost_usd=1.0)
    report = _report(
        [
            _case("a", expected="real_bug", predicted="flaky_test", cost=0.1),
            _case("b", expected="real_bug", predicted="real_bug", cost=0.1),
        ]
    )

    gates = evaluate_gates(report, baseline=baseline, budget_usd=1.0)

    assert gates.passed is False
    assert any("false-flaky" in failure for failure in gates.failures)


def test_gate_fails_on_accuracy_regression() -> None:
    baseline = ReplayMetrics(false_flaky_count=1, accuracy=0.9, total_cost_usd=1.0)
    report = _report([_case("a", expected="real_bug", predicted="unknown", cost=0.1)])

    gates = evaluate_gates(report, baseline=baseline, budget_usd=1.0)

    assert gates.checks["no_accuracy_regression"] is False
    assert any("accuracy regressed" in failure for failure in gates.failures)


def test_gate_fails_on_cost_increase_over_ten_percent() -> None:
    baseline = ReplayMetrics(false_flaky_count=0, accuracy=0.0, total_cost_usd=1.0)
    report = _report([_case("a", expected="real_bug", predicted="real_bug", cost=1.2)])

    gates = evaluate_gates(report, baseline=baseline, budget_usd=10.0)

    assert gates.checks["cost_within_10_percent"] is False
    assert any("cost increased beyond 10%" in failure for failure in gates.failures)


def test_gate_fails_on_budget_and_config_guard() -> None:
    report = _report([_case("a", expected="real_bug", predicted="real_bug", cost=2.0)])
    gates = evaluate_gates(report, baseline=None, budget_usd=1.0, config_guard_passed=False)

    assert gates.checks["within_budget"] is False
    assert gates.checks["config_guard"] is False
    assert gates.passed is False


def test_confusion_matrix_is_mandatory_in_markdown() -> None:
    report = _report(
        [
            _case("a", expected="real_bug", predicted="real_bug"),
            _case("b", expected="real_bug", predicted="flaky_test"),
        ]
    )
    report.gates = evaluate_gates(report, baseline=None, budget_usd=1.0)

    markdown = render_markdown(report)

    assert "## Confusion matrix" in markdown
    assert "| `real_bug` | 1 | 1 |" in markdown
    assert "## Mismatches" in markdown
    assert "`b`" in markdown
    assert "## Hard gates" in markdown
