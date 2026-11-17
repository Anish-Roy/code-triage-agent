from __future__ import annotations

from code_triage_agent.collector.snapshot import LoadedSnapshot
from code_triage_agent.config.schema import BudgetConfig
from code_triage_agent.models import LLMResponse, ToolCall
from code_triage_agent.runner.llm import FakeLLM, LLMError
from code_triage_agent.runner.loop import run_loop
from code_triage_agent.runner.tools import ToolContext, ToolRegistry


def _registry(snapshot: LoadedSnapshot, allowed: tuple[str, ...] = ("read_file",)) -> ToolRegistry:
    return ToolRegistry(
        allowed_tools=allowed,
        context=ToolContext(snapshot=snapshot, run_id=snapshot.metadata.run_id, replay=True),
    )


def _submit(arguments: dict[str, object]) -> LLMResponse:
    return LLMResponse(
        tool_calls=[ToolCall(id="submit", name="submit_triage", arguments=arguments)],
        usage={"input_tokens": 10, "output_tokens": 5},  # type: ignore[arg-type]
    )


def _decision(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "category": "real_bug",
        "confidence": 0.9,
        "summary": "The login endpoint returns 500.",
        "evidence": [
            {
                "source": "log",
                "quote": "AssertionError: expected 200 got 500",
                "why": "test assertion",
            }
        ],
        "recommended_action": {"type": "comment", "params": {}},
    }
    values.update(overrides)
    return values


def test_valid_submission_succeeds(make_snapshot: object) -> None:
    snapshot = make_snapshot()  # type: ignore[operator]
    llm = FakeLLM([_submit(_decision())])

    result = run_loop(
        llm=llm,
        registry=_registry(snapshot),
        snapshot=snapshot,
        messages=[],
        budget=BudgetConfig(),
    )

    assert result.status == "succeeded"
    assert result.decision.category == "real_bug"
    assert result.steps[-1].tool == "submit_triage"
    assert result.cost_usd > 0


def test_tool_call_is_executed_then_submission_succeeds(make_snapshot: object) -> None:
    snapshot = make_snapshot(extra_files={"files/app.py": "print('hi')\n"})  # type: ignore[operator]
    llm = FakeLLM(
        [
            LLMResponse(
                tool_calls=[ToolCall(id="t1", name="read_file", arguments={"path": "app.py"})]
            ),
            _submit(
                _decision(
                    evidence=[
                        {
                            "source": "code",
                            "quote": "print('hi')",
                            "why": "the added line",
                        }
                    ]
                )
            ),
        ]
    )

    result = run_loop(
        llm=llm,
        registry=_registry(snapshot),
        snapshot=snapshot,
        messages=[],
        budget=BudgetConfig(),
    )

    assert result.status == "succeeded"
    tool_steps = [step for step in result.steps if step.kind == "tool"]
    assert tool_steps[0].tool == "read_file"
    assert tool_steps[0].is_error is False


def test_invalid_evidence_retries_once_at_lower_temperature(make_snapshot: object) -> None:
    snapshot = make_snapshot()  # type: ignore[operator]
    bad = _decision(
        evidence=[{"source": "log", "quote": "fabricated quote", "why": "not in snapshot"}]
    )
    llm = FakeLLM([_submit(bad), _submit(_decision())])

    result = run_loop(
        llm=llm,
        registry=_registry(snapshot),
        snapshot=snapshot,
        messages=[],
        budget=BudgetConfig(),
    )

    assert result.status == "succeeded"
    assert [call["temperature"] for call in llm.calls] == [0.2, 0.0]
    assert any(step.kind == "action" and step.is_error for step in result.steps)


def test_second_invalid_submission_falls_back_to_unknown(make_snapshot: object) -> None:
    snapshot = make_snapshot()  # type: ignore[operator]
    bad = _decision(
        evidence=[{"source": "log", "quote": "fabricated quote", "why": "not in snapshot"}]
    )
    llm = FakeLLM([_submit(bad), _submit(bad)])

    result = run_loop(
        llm=llm,
        registry=_registry(snapshot),
        snapshot=snapshot,
        messages=[],
        budget=BudgetConfig(),
    )

    assert result.status == "failed"
    assert result.decision.category == "unknown"
    assert result.decision.needs_human is True
    assert result.invalid_output is True
    assert "after retry" in (result.error or "")


def test_unknown_decision_does_not_require_evidence(make_snapshot: object) -> None:
    snapshot = make_snapshot()  # type: ignore[operator]
    llm = FakeLLM(
        [
            _submit(
                {
                    "category": "unknown",
                    "confidence": 0.2,
                    "summary": "Not enough evidence.",
                    "evidence": [],
                    "needs_human": True,
                }
            )
        ]
    )

    result = run_loop(
        llm=llm,
        registry=_registry(snapshot),
        snapshot=snapshot,
        messages=[],
        budget=BudgetConfig(),
    )

    assert result.status == "succeeded"
    assert result.decision.category == "unknown"


def test_step_budget_exhaustion_returns_a_valid_record(make_snapshot: object) -> None:
    snapshot = make_snapshot()  # type: ignore[operator]
    llm = FakeLLM([LLMResponse(content="thinking"), LLMResponse(content="still thinking")])

    result = run_loop(
        llm=llm,
        registry=_registry(snapshot),
        snapshot=snapshot,
        messages=[],
        budget=BudgetConfig(max_steps=2),
    )

    assert result.status == "budget_exceeded"
    assert result.decision.category == "unknown"
    assert result.decision.needs_human is True
    assert "step budget" in (result.error or "")


def test_cost_budget_stops_before_the_next_turn(make_snapshot: object) -> None:
    snapshot = make_snapshot()  # type: ignore[operator]
    llm = FakeLLM([LLMResponse(content="expensive", cost_usd=0.75), LLMResponse(content="never")])

    result = run_loop(
        llm=llm,
        registry=_registry(snapshot),
        snapshot=snapshot,
        messages=[],
        budget=BudgetConfig(max_cost_usd=0.5),
    )

    assert result.status == "budget_exceeded"
    assert llm.remaining == 1
    assert "cost budget" in (result.error or "")


def test_time_budget_is_checked_before_calling_the_model(make_snapshot: object) -> None:
    snapshot = make_snapshot()  # type: ignore[operator]
    llm = FakeLLM([LLMResponse(content="never")])
    ticks = iter([0.0, 5.0, 5.0, 5.0])

    result = run_loop(
        llm=llm,
        registry=_registry(snapshot),
        snapshot=snapshot,
        messages=[],
        budget=BudgetConfig(max_seconds=1),
        clock=lambda: next(ticks),
    )

    assert result.status == "budget_exceeded"
    assert llm.remaining == 1
    assert "time budget" in (result.error or "")


def test_model_error_still_returns_a_safe_decision(make_snapshot: object) -> None:
    snapshot = make_snapshot()  # type: ignore[operator]
    llm = FakeLLM([LLMError("bad request")])

    result = run_loop(
        llm=llm,
        registry=_registry(snapshot),
        snapshot=snapshot,
        messages=[],
        budget=BudgetConfig(),
    )

    assert result.status == "failed"
    assert result.decision.category == "unknown"
    assert result.decision.needs_human is True
    assert any(step.kind == "llm" and step.is_error for step in result.steps)


def test_disallowed_tool_call_is_reported_and_loop_continues(make_snapshot: object) -> None:
    snapshot = make_snapshot()  # type: ignore[operator]
    llm = FakeLLM(
        [
            LLMResponse(
                tool_calls=[ToolCall(id="evil", name="open_fix_pr", arguments={"branch": "main"})]
            ),
            _submit(_decision()),
        ]
    )

    result = run_loop(
        llm=llm,
        registry=_registry(snapshot),
        snapshot=snapshot,
        messages=[],
        budget=BudgetConfig(),
    )

    tool_step = next(step for step in result.steps if step.kind == "tool")
    assert tool_step.tool == "open_fix_pr"
    assert tool_step.is_error is True
    assert "not allowed" in (tool_step.result["error"])
    assert result.status == "succeeded"
