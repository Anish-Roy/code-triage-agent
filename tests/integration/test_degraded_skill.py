"""Prove the harness notices a deliberately weakened skill rule.

The policy model is deterministic but it is not scripted per case: it reads the
skill rules in the system prompt and changes its answer when the flaky-test rule
is removed. That is what a recorded ``FakeLLM`` cannot prove.
"""

from __future__ import annotations

import json
import re
import shutil
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from code_triage_agent.background.replay import run_replay
from code_triage_agent.models import LLMMessage, LLMResponse, ToolCall, ToolSpec
from code_triage_agent.settings import Settings

PROJECT_ROOT = Path(__file__).resolve().parents[2]
FROZEN = PROJECT_ROOT / "evals" / "frozen"
CASE_ID = "pr-flaky-retry-pass"
FLAKY_RULE = (
    "Never call a failure `flaky_test` unless there is evidence it passed on the "
    "same commit or the same signature has a documented flaky history."
)
FLAKY_RULE_PATTERN = re.compile(
    r"Never call a failure `flaky_test` unless there is evidence it passed on the\s+"
    r"same commit or the same signature has a documented flaky history\."
)
RETRY_QUOTE = "PASSED on retry at commit abc1234"
FAILURE_QUOTE = "FAILED tests/test_cache.py::test_expiry - assert 0 > 5"


class RuleFollowingLLM:
    """A tiny deterministic policy that obeys the flaky-test rule it is given."""

    model = "rule-policy-v1"

    def complete(
        self,
        messages: Sequence[LLMMessage],
        tools: Sequence[ToolSpec],
        *,
        temperature: float = 0.0,
        timeout_seconds: float = 60.0,
    ) -> LLMResponse:
        system = "\n".join(message.content for message in messages if message.role == "system")
        user = "\n".join(message.content for message in messages if message.role == "user")
        rule_present = FLAKY_RULE in " ".join(system.split())
        retry_evidence = RETRY_QUOTE in user

        if rule_present and retry_evidence:
            decision: dict[str, Any] = {
                "category": "flaky_test",
                "confidence": 0.8,
                "summary": "The same commit passed on retry and the flaky rule permits this label.",
                "evidence": [
                    {
                        "source": "log",
                        "quote": RETRY_QUOTE,
                        "why": "the same commit passed on retry",
                    }
                ],
                "recommended_action": {"type": "comment", "params": {}},
            }
        else:
            decision = {
                "category": "real_bug",
                "confidence": 0.8,
                "summary": (
                    "Without the flaky rule this policy treats the failure as a product bug."
                ),
                "evidence": [
                    {
                        "source": "log",
                        "quote": FAILURE_QUOTE,
                        "why": "the test failed",
                    }
                ],
                "recommended_action": {"type": "comment", "params": {}},
            }

        return LLMResponse(
            tool_calls=[ToolCall(id="submit", name="submit_triage", arguments=decision)],
            usage={"input_tokens": 500, "output_tokens": 100},  # type: ignore[arg-type]
            model=self.model,
        )


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        config_dir=PROJECT_ROOT / "config",
        database_path=tmp_path / "var" / "triage.db",
        snapshot_root=tmp_path / "snapshots",
        event_root=tmp_path / "events",
        eval_frozen_dir=FROZEN,
        eval_reports_dir=tmp_path / "reports",
        eval_cache_dir=tmp_path / "cache",
    )


def _factory(*_args: object, **_kwargs: object) -> RuleFollowingLLM:
    return RuleFollowingLLM()


def _degraded_config(tmp_path: Path) -> Path:
    target = tmp_path / "degraded-config"
    shutil.copytree(PROJECT_ROOT / "config", target)
    skill = target / "skills" / "triage-pr-failure.md"
    text = skill.read_text(encoding="utf-8")
    assert FLAKY_RULE_PATTERN.search(text)
    skill.write_text(
        FLAKY_RULE_PATTERN.sub("Use your judgement when choosing a category", text),
        encoding="utf-8",
    )
    return target


def test_intact_skill_passes_and_weakened_rule_fails_the_gates(tmp_path: Path) -> None:
    settings = _settings(tmp_path)

    intact = run_replay(
        set_dir=FROZEN,
        settings=settings,
        case_ids=[CASE_ID],
        report_dir=tmp_path / "reports",
        llm_factory=_factory,
        project_root=PROJECT_ROOT,
    )
    assert intact.report.metrics.accuracy == 1.0
    assert intact.report.cases[0].predicted_category == "flaky_test"
    assert intact.report.gates is not None and intact.report.gates.passed

    baseline_path = tmp_path / "baseline.json"
    baseline_path.write_text(
        json.dumps(intact.report.model_dump(mode="json"), indent=2, sort_keys=True),
        encoding="utf-8",
    )

    degraded = run_replay(
        set_dir=FROZEN,
        settings=settings,
        case_ids=[CASE_ID],
        report_dir=tmp_path / "reports",
        llm_factory=_factory,
        config_dir=_degraded_config(tmp_path),
        baseline_path=baseline_path,
        project_root=PROJECT_ROOT,
    )

    case = degraded.report.cases[0]
    assert case.predicted_category == "real_bug"
    assert case.label == "incorrect"
    assert degraded.report.metrics.accuracy == 0.0
    assert degraded.report.gates is not None
    assert degraded.report.gates.passed is False
    assert any("accuracy regressed" in failure for failure in degraded.report.gates.failures)


def test_degradation_is_not_caused_by_a_scripted_decision(tmp_path: Path) -> None:
    settings = _settings(tmp_path)

    intact = run_replay(
        set_dir=FROZEN,
        settings=settings,
        case_ids=[CASE_ID],
        report_dir=tmp_path / "reports",
        llm_factory=_factory,
        project_root=PROJECT_ROOT,
    )
    degraded = run_replay(
        set_dir=FROZEN,
        settings=settings,
        case_ids=[CASE_ID],
        report_dir=tmp_path / "reports",
        llm_factory=_factory,
        config_dir=_degraded_config(tmp_path),
        project_root=PROJECT_ROOT,
    )

    assert intact.report.cases[0].predicted_category != degraded.report.cases[0].predicted_category
    assert intact.report.config_sha != degraded.report.config_sha


def test_policy_llm_sanity() -> None:
    """The policy itself must be able to produce both answers."""
    llm = RuleFollowingLLM()
    system = LLMMessage(role="system", content=FLAKY_RULE)
    user = LLMMessage(role="user", content=RETRY_QUOTE)
    response = llm.complete([system, user], [])
    assert response.tool_calls[0].arguments["category"] == "flaky_test"

    response = llm.complete([LLMMessage(role="system", content="no rule"), user], [])
    assert response.tool_calls[0].arguments["category"] == "real_bug"


@pytest.mark.parametrize("quote", [RETRY_QUOTE, FAILURE_QUOTE])
def test_policy_quotes_exist_in_the_case_snapshot(quote: str) -> None:
    logs = (FROZEN / CASE_ID / "snapshot" / "logs.txt").read_text(encoding="utf-8")
    assert quote in logs
