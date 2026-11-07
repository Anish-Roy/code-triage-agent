from __future__ import annotations

from pathlib import Path

from code_triage_agent.collector.trim import JobLogInput, parse_sections, trim_logs

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def _filler(count: int, prefix: str) -> list[str]:
    return [f"{prefix} line {index}" for index in range(count)]


def test_focuses_failed_step_and_keeps_context() -> None:
    lines = [
        *_filler(100, "setup"),
        "##[group]Run pytest -q",
        "collected 42 items",
        "FAILED tests/test_login.py::test_login",
        "E   AssertionError: expected 200 got 500",
        "##[error]Process completed with exit code 1.",
        "##[endgroup]",
        *_filler(100, "cleanup"),
    ]
    error_line = lines.index("##[error]Process completed with exit code 1.") + 1

    result = trim_logs(
        [
            JobLogInput(
                job_id=5001,
                job_name="test (3.13)",
                failed_step="Run pytest -q",
                text="\n".join(lines),
            )
        ]
    )

    job = result.jobs[0]
    assert job.step_name == "Run pytest -q"
    assert job.total_lines == len(lines)
    assert job.kept_lines < len(lines)
    assert job.truncated is False
    assert any(rng.start <= error_line <= rng.end for rng in job.line_ranges)
    assert "expected 200 got 500" in result.text
    assert "cleanup line 99" in result.text  # tail context
    assert "setup line 0" in result.text  # header context
    assert "setup line 50" not in result.text  # middle of setup is dropped


def test_falls_back_to_error_window_without_groups() -> None:
    lines = _filler(100, "line")
    error_index = 50
    lines[error_index] = "ERROR: compilation failed"

    result = trim_logs(
        [JobLogInput(job_id=1, job_name="build", failed_step=None, text="\n".join(lines))],
        before=10,
        after=5,
    )

    job = result.jobs[0]
    assert 1 in {rng.start for rng in job.line_ranges}  # header
    assert any(rng.start <= error_index + 1 <= rng.end for rng in job.line_ranges)
    assert "ERROR: compilation failed" in result.text
    assert len(job.line_ranges) >= 3


def test_caps_total_bytes_and_marks_truncated() -> None:
    lines = [("x" * 180) + f" {index}" for index in range(1000)]
    lines[700] = "##[error] exploded"

    result = trim_logs(
        [JobLogInput(job_id=1, job_name="noisy", failed_step=None, text="\n".join(lines))],
        max_bytes=4096,
    )

    assert result.total_bytes <= 4096
    assert result.jobs[0].truncated is True
    assert result.jobs[0].kept_lines < result.jobs[0].total_lines


def test_caps_number_of_failed_jobs() -> None:
    logs = [
        JobLogInput(job_id=index, job_name=f"job-{index}", failed_step=None, text="##[error] boom")
        for index in range(5)
    ]

    result = trim_logs(logs)

    assert len(result.jobs) == 3
    assert result.dropped_jobs == 2
    assert [job.job_id for job in result.jobs] == [0, 1, 2]


def test_parse_nested_sections() -> None:
    sections = parse_sections(
        ["##[group]outer", "##[group]inner", "body", "##[endgroup]", "##[endgroup]"]
    )

    by_name = {section.name: section for section in sections}
    assert set(by_name) == {"outer", "inner"}
    assert by_name["inner"].start == 2
    assert by_name["inner"].end == 2


def test_strips_ansi_before_trimming() -> None:
    text = "\x1b[31m##[error]red failure\x1b[0m"

    result = trim_logs([JobLogInput(job_id=1, job_name="job", failed_step=None, text=text)])

    assert "\x1b[" not in result.text
    assert "##[error]red failure" in result.text


def test_step_matching_is_normalized() -> None:
    lines = [
        *_filler(50, "setup"),
        "##[group]Run   PYTEST   tests/unit",
        "##[error]assertion failed",
        "##[endgroup]",
        *_filler(50, "cleanup"),
    ]

    result = trim_logs(
        [
            JobLogInput(
                job_id=1,
                job_name="job",
                failed_step="pytest tests/unit",
                text="\n".join(lines),
            )
        ]
    )

    assert "assertion failed" in result.text
    assert result.jobs[0].kept_lines < len(lines)


def test_trims_recorded_fixture_around_failed_step() -> None:
    text = (FIXTURES / "logs" / "failed_job.log").read_text(encoding="utf-8")

    result = trim_logs(
        [
            JobLogInput(
                job_id=5001,
                job_name="test (3.13)",
                failed_step="Run pytest",
                text=text,
            )
        ]
    )

    assert "AssertionError: expected 200 got 500" in result.text
    assert "Process completed with exit code 1" in result.text
    assert result.jobs[0].step_name == "Run pytest"
    assert result.jobs[0].line_ranges
