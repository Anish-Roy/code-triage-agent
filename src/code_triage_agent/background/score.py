"""The deterministic label decision table.

Precedence is fixed and must not be reordered:

1. explicit human feedback,
2. fix-PR outcome,
3. rerun result,
4. recurrence.

The table is intentionally small and pure. The outcome collector gathers
signals; this module decides what they mean. No LLM, clock, network, or database
access happens here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from code_triage_agent.models import OutcomeLabel, OutcomeSignals

RECURRENCE_WINDOW = timedelta(days=7)


@dataclass(frozen=True)
class LabelDecision:
    """A label plus the exact rule and evidence that produced it."""

    label: OutcomeLabel
    reason: str
    rule: str
    evidence: dict[str, Any] = field(default_factory=dict)


def normalize_human_feedback(value: str | None) -> str | None:
    """Reduce free-text feedback to a label, or ``None`` when unusable.

    Accepted spellings are deliberately narrow: ``correct``, ``incorrect``, and
    ``unknown``. Operators can write ``triage-label: correct`` in a PR reply.
    """
    if value is None:
        return None
    token = value.strip().lower()
    for candidate in ("incorrect", "correct", "unknown"):
        if token == candidate or token.endswith(f"triage-label: {candidate}"):
            return candidate
    return None


def decide_label(
    signals: OutcomeSignals,
    *,
    predicted_category: str | None,
    now: datetime | None = None,
) -> LabelDecision:
    """Map collected signals to ``correct``/``incorrect``/``unknown``.

    ``now`` is only used to guard the recurrence window when a caller passes
    signals that were gathered earlier. The collector already limits recurrence
    lookups to seven days, but the label table refuses to trust stale evidence.
    """
    checked = now or signals.checked_at

    # 1. Explicit human feedback is authoritative.
    human = normalize_human_feedback(signals.human_feedback)
    if human == "correct":
        return LabelDecision(
            label="correct",
            reason="human feedback marked this run correct",
            rule="human_feedback",
            evidence={"human_feedback": signals.human_feedback},
        )
    if human == "incorrect":
        return LabelDecision(
            label="incorrect",
            reason="human feedback marked this run incorrect",
            rule="human_feedback",
            evidence={"human_feedback": signals.human_feedback},
        )

    # 2. Fix-PR outcome, including recurrence after a claimed fix.
    fix = _fix_pr_decision(signals, checked_at=checked)
    if fix is not None:
        return fix

    # 3. Rerun result. Only decisive for a flaky prediction.
    if signals.rerun_conclusion is not None:
        rerun = _rerun_decision(signals, predicted_category=predicted_category)
        if rerun is not None:
            return rerun

    # 4. Recurrence alone is not enough to call a prediction right or wrong
    # unless a fix was claimed. A recurring flaky test is still flaky.
    if signals.signature_recurred:
        return LabelDecision(
            label="unknown",
            reason=(
                "the signature recurred but no rerun or fix outcome makes the prediction decisive"
            ),
            rule="recurrence_without_fix",
            evidence={
                "signature_recurred": True,
                "recurrence_run_ids": list(signals.recurrence_run_ids),
            },
        )

    return LabelDecision(
        label="unknown",
        reason="no usable outcome signal is available yet",
        rule="no_signal",
        evidence={},
    )


def _fix_pr_decision(signals: OutcomeSignals, *, checked_at: datetime) -> LabelDecision | None:
    if signals.fix_pr_reverted:
        return LabelDecision(
            label="incorrect",
            reason="the fix pull request was reverted",
            rule="fix_pr_reverted",
            evidence=_fix_evidence(signals),
        )

    if signals.fix_pr_merged is True:
        if signals.signature_recurred:
            # The collector only records recurrence inside the seven-day window.
            # Keep the explicit age check so a caller cannot smuggle in stale
            # evidence and silently flip the label.
            recurrence_time = _recurrence_time(signals)
            if recurrence_time is not None and (checked_at - recurrence_time <= RECURRENCE_WINDOW):
                return LabelDecision(
                    label="incorrect",
                    reason=(
                        "the fix pull request merged, but the same signature "
                        "recurred within seven days"
                    ),
                    rule="recurrence_after_fix",
                    evidence={
                        **_fix_evidence(signals),
                        "recurrence_run_ids": list(signals.recurrence_run_ids),
                    },
                )
        return LabelDecision(
            label="correct",
            reason="the fix pull request merged",
            rule="fix_pr_merged",
            evidence=_fix_evidence(signals),
        )

    if signals.fix_pr_state == "closed":
        return LabelDecision(
            label="incorrect",
            reason="the fix pull request was closed without merging",
            rule="fix_pr_closed",
            evidence=_fix_evidence(signals),
        )

    return None


def _rerun_decision(
    signals: OutcomeSignals, *, predicted_category: str | None
) -> LabelDecision | None:
    if predicted_category != "flaky_test":
        # A passing rerun is weak evidence for every other category: a real bug
        # can be masked by a retry. Do not let it override a real-bug label.
        return None

    conclusion = (signals.rerun_conclusion or "").lower()
    if conclusion == "success":
        return LabelDecision(
            label="correct",
            reason="the predicted flaky test passed on rerun",
            rule="flaky_rerun_passed",
            evidence=_rerun_evidence(signals),
        )
    if conclusion in {"failure", "timed_out"}:
        if signals.rerun_signature_matches is False:
            return LabelDecision(
                label="unknown",
                reason=(
                    "the rerun failed with a different signature, so it does not "
                    "confirm or refute the flaky prediction"
                ),
                rule="flaky_rerun_different_signature",
                evidence=_rerun_evidence(signals),
            )
        return LabelDecision(
            label="incorrect",
            reason="the predicted flaky test failed again with the same signature",
            rule="flaky_rerun_failed",
            evidence=_rerun_evidence(signals),
        )
    if conclusion == "cancelled":
        return LabelDecision(
            label="unknown",
            reason="the rerun was cancelled",
            rule="flaky_rerun_cancelled",
            evidence=_rerun_evidence(signals),
        )
    return None


def _fix_evidence(signals: OutcomeSignals) -> dict[str, Any]:
    return {
        "fix_pr_number": signals.fix_pr_number,
        "fix_pr_state": signals.fix_pr_state,
        "fix_pr_merged": signals.fix_pr_merged,
        "fix_pr_reverted": signals.fix_pr_reverted,
    }


def _rerun_evidence(signals: OutcomeSignals) -> dict[str, Any]:
    return {
        "rerun_conclusion": signals.rerun_conclusion,
        "rerun_run_attempt": signals.rerun_run_attempt,
        "rerun_signature_matches": signals.rerun_signature_matches,
    }


def _recurrence_time(signals: OutcomeSignals) -> datetime | None:
    raw = signals.evidence.get("recurrence_at")
    if isinstance(raw, str):
        try:
            return datetime.fromisoformat(raw)
        except ValueError:
            return None
    return None


__all__ = [
    "RECURRENCE_WINDOW",
    "LabelDecision",
    "decide_label",
    "normalize_human_feedback",
]
