from __future__ import annotations

from code_triage_agent.collector.signature import build_signature, normalize_line


def _log(
    *, timestamp: str, duration: str, path: str, port: int, worker: int, ansi: bool = False
) -> str:
    color = "\x1b[31m" if ansi else ""
    reset = "\x1b[0m" if ansi else ""
    return "\n".join(
        [
            f"{timestamp} ##[group]Run pytest",
            f"{timestamp} running tests in {path} took {duration}",
            f"{timestamp} server listening on 127.0.0.1:{port} (worker-{worker})",
            f"{timestamp} {color}##[error]AssertionError: expected 200 got 500{reset}",
            f"{timestamp} ##[endgroup]",
        ]
    )


def test_same_timeout_variants_share_a_signature() -> None:
    first = build_signature(
        job_name="test (3.13)",
        step_name="Run pytest",
        text=_log(
            timestamp="2026-11-05T10:00:00.1234567Z",
            duration="1.2s",
            path="/tmp/pytest-abc123/tests",
            port=34567,
            worker=12,
        ),
    )
    second = build_signature(
        job_name="test (3.13)",
        step_name="Run pytest",
        text=_log(
            timestamp="2026-11-06T18:42:11.9876543Z",
            duration="4.5s",
            path="/home/runner/work/acme/widgets/tests",
            port=40123,
            worker=99,
        ),
    )
    third = build_signature(
        job_name="test (3.13)",
        step_name="Run pytest",
        text=_log(
            timestamp="2026-11-07T01:02:03Z",
            duration="0.3s",
            path="/var/folders/tmp/tests",
            port=8080,
            worker=7,
            ansi=True,
        ),
    )

    assert first.value == second.value == third.value
    assert first.value.startswith("sig_v1:")
    assert len(first.value) == len("sig_v1:") + 64


def test_different_failure_modes_have_different_signatures() -> None:
    timeout = _log(
        timestamp="2026-11-05T10:00:00Z",
        duration="30s",
        path="/tmp/a/tests",
        port=1000,
        worker=1,
    )
    assertion = timeout.replace(
        "##[error]AssertionError: expected 200 got 500",
        "##[error]AssertionError: expected 200 got 500",
    )
    compile_error = "\n".join(
        [
            "##[error]src/widgets.py:10: error: Incompatible return value type",
            "##[error]Found 1 error in 1 file",
        ]
    )

    timeout_sig = build_signature(job_name="test", step_name="Run pytest", text=timeout)
    assertion_sig = build_signature(job_name="test", step_name="Run pytest", text=assertion)
    compile_sig = build_signature(job_name="build", step_name="Run mypy", text=compile_error)

    assert timeout_sig.value == assertion_sig.value
    assert compile_sig.value != timeout_sig.value


def test_job_and_step_are_part_of_the_signature() -> None:
    text = "##[error]same failure text"
    one = build_signature(job_name="test", step_name="pytest", text=text)
    two = build_signature(job_name="build", step_name="pytest", text=text)
    three = build_signature(job_name="test", step_name="mypy", text=text)

    assert len({one.value, two.value, three.value}) == 3


def test_exemplar_uses_error_lines() -> None:
    result = build_signature(
        job_name="test",
        step_name="pytest",
        text="setup noise\n##[error]AssertionError: boom\ncleanup noise",
    )

    assert "AssertionError: boom" in result.exemplar
    assert "setup noise" not in result.exemplar


def test_normalize_line_removes_volatile_values() -> None:
    normalized = normalize_line(
        "2026-11-05T10:00:00.123Z /tmp/pytest-abc123/test_x.py:42 "
        "took 1.25s on 127.0.0.1:34567 worker-12 sha abc1234"
    )

    assert "<ts>" in normalized
    assert "<path>" in normalized
    assert "<duration>" in normalized
    assert "<host>" in normalized
    assert "<worker>" in normalized
    assert "<hex>" in normalized
    assert "34567" not in normalized
    assert "abc1234" not in normalized
