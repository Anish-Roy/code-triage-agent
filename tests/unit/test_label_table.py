from __future__ import annotations

import ast
import inspect
from datetime import UTC, datetime, timedelta

import pytest

from code_triage_agent.background import score as score_module
from code_triage_agent.background.score import (
    RECURRENCE_WINDOW,
    decide_label,
    normalize_human_feedback,
)
from code_triage_agent.models import OutcomeSignals

NOW = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)


def _signals(**overrides: object) -> OutcomeSignals:
    values: dict[str, object] = {"checked_at": NOW}
    values.update(overrides)
    return OutcomeSignals(**values)  # type: ignore[arg-type]


def test_human_feedback_normalization() -> None:
    assert normalize_human_feedback("correct") == "correct"
    assert normalize_human_feedback("INCORRECT") == "incorrect"
    assert normalize_human_feedback("triage-label: unknown") == "unknown"
    assert normalize_human_feedback("looks correct to me") is None
    assert normalize_human_feedback(None) is None


@pytest.mark.parametrize(
    ("predicted", "signals", "expected", "rule"),
    [
        (
            "flaky_test",
            {"rerun_conclusion": "success"},
            "correct",
            "flaky_rerun_passed",
        ),
        (
            "flaky_test",
            {"rerun_conclusion": "failure", "rerun_signature_matches": True},
            "incorrect",
            "flaky_rerun_failed",
        ),
        (
            "flaky_test",
            {"rerun_conclusion": "timed_out", "rerun_signature_matches": None},
            "incorrect",
            "flaky_rerun_failed",
        ),
        (
            "flaky_test",
            {"rerun_conclusion": "failure", "rerun_signature_matches": False},
            "unknown",
            "flaky_rerun_different_signature",
        ),
        (
            "flaky_test",
            {"rerun_conclusion": "cancelled"},
            "unknown",
            "flaky_rerun_cancelled",
        ),
        ("real_bug", {"rerun_conclusion": "success"}, "unknown", "no_signal"),
        (
            "real_bug",
            {"fix_pr_state": "closed", "fix_pr_merged": True},
            "correct",
            "fix_pr_merged",
        ),
        (
            "real_bug",
            {"fix_pr_state": "closed", "fix_pr_merged": False},
            "incorrect",
            "fix_pr_closed",
        ),
        (
            "real_bug",
            {"fix_pr_merged": True, "fix_pr_reverted": True},
            "incorrect",
            "fix_pr_reverted",
        ),
        (
            "real_bug",
            {
                "fix_pr_merged": True,
                "signature_recurred": True,
                "evidence": {"recurrence_at": NOW.isoformat()},
            },
            "incorrect",
            "recurrence_after_fix",
        ),
        (
            "real_bug",
            {"signature_recurred": True, "recurrence_run_ids": ("r_2",)},
            "unknown",
            "recurrence_without_fix",
        ),
        ("real_bug", {}, "unknown", "no_signal"),
        (
            "real_bug",
            {"fix_pr_state": "open", "fix_pr_merged": False},
            "unknown",
            "no_signal",
        ),
    ],
)
def test_label_decision_table(
    predicted: str, signals: dict[str, object], expected: str, rule: str
) -> None:
    decision = decide_label(_signals(**signals), predicted_category=predicted, now=NOW)

    assert decision.label == expected
    assert decision.rule == rule


def test_human_feedback_beats_every_other_signal() -> None:
    decision = decide_label(
        _signals(
            human_feedback="triage-label: correct",
            fix_pr_reverted=True,
            fix_pr_merged=True,
            rerun_conclusion="failure",
            signature_recurred=True,
        ),
        predicted_category="flaky_test",
        now=NOW,
    )

    assert decision.label == "correct"
    assert decision.rule == "human_feedback"


def test_human_feedback_can_flip_a_merged_fix() -> None:
    decision = decide_label(
        _signals(
            human_feedback="triage-label: incorrect",
            fix_pr_state="closed",
            fix_pr_merged=True,
        ),
        predicted_category="real_bug",
        now=NOW,
    )

    assert decision.label == "incorrect"
    assert decision.rule == "human_feedback"


def test_fix_pr_precedence_beats_a_failed_rerun() -> None:
    decision = decide_label(
        _signals(
            fix_pr_state="closed",
            fix_pr_merged=True,
            rerun_conclusion="failure",
            rerun_signature_matches=True,
        ),
        predicted_category="flaky_test",
        now=NOW,
    )

    assert decision.label == "correct"
    assert decision.rule == "fix_pr_merged"


def test_stale_recurrence_outside_the_window_does_not_flip_a_merged_fix() -> None:
    stale = NOW - RECURRENCE_WINDOW - timedelta(minutes=1)
    decision = decide_label(
        _signals(
            fix_pr_merged=True,
            signature_recurred=True,
            evidence={"recurrence_at": stale.isoformat()},
        ),
        predicted_category="real_bug",
        now=NOW,
    )

    assert decision.label == "correct"
    assert decision.rule == "fix_pr_merged"


def test_unparseable_recurrence_time_is_treated_as_stale() -> None:
    decision = decide_label(
        _signals(
            fix_pr_merged=True,
            signature_recurred=True,
            evidence={"recurrence_at": "not-a-date"},
        ),
        predicted_category="real_bug",
        now=NOW,
    )

    assert decision.label == "correct"


def test_label_decision_carries_evidence() -> None:
    decision = decide_label(
        _signals(rerun_conclusion="success", rerun_run_attempt=2),
        predicted_category="flaky_test",
        now=NOW,
    )

    assert decision.evidence["rerun_conclusion"] == "success"
    assert decision.evidence["rerun_run_attempt"] == 2


def test_label_table_has_no_llm_import() -> None:
    tree = ast.parse(inspect.getsource(score_module))
    imports: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.append(node.module)
    assert not any("llm" in name for name in imports)
