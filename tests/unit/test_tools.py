from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from code_triage_agent.collector.snapshot import LoadedSnapshot
from code_triage_agent.config.schema import MemoryNote
from code_triage_agent.github.models import GitHubIssueComment
from code_triage_agent.runner.tools import ToolContext, ToolRegistry


def _registry(
    snapshot: LoadedSnapshot,
    allowed: tuple[str, ...] = ("read_file", "git_log", "git_diff", "search_memory"),
    **context: object,
) -> ToolRegistry:
    return ToolRegistry(
        allowed_tools=allowed,
        context=ToolContext(
            snapshot=snapshot,
            run_id=snapshot.metadata.run_id,
            dry_run=True,
            replay=True,
            **context,  # type: ignore[arg-type]
        ),
    )


def test_specs_only_offer_read_tools(make_snapshot: object) -> None:
    snapshot = make_snapshot()  # type: ignore[operator]
    registry = _registry(
        snapshot,
        allowed=(
            "read_file",
            "git_log",
            "git_diff",
            "search_memory",
            "comment_on_pr",
            "open_fix_pr",
            "open_issue",
            "rerun_failed_jobs",
        ),
    )

    names = [spec.name for spec in registry.specs()]

    assert names == ["read_file", "git_log", "git_diff", "search_memory"]
    assert "open_fix_pr" not in names


def test_disallowed_act_tools_are_rejected(make_snapshot: object) -> None:
    snapshot = make_snapshot()  # type: ignore[operator]
    registry = _registry(
        snapshot,
        allowed=("read_file", "open_fix_pr", "open_issue", "rerun_failed_jobs"),
    )

    for name in ("open_fix_pr", "open_issue", "rerun_failed_jobs"):
        result = registry.execute(name, {"body": "x"})
        assert result.ok is False
        assert "not allowed" in (result.error or "")


def test_unknown_tool_is_rejected(make_snapshot: object) -> None:
    registry = _registry(make_snapshot())  # type: ignore[operator]

    result = registry.execute("shell", {"command": "rm -rf /"})

    assert result.ok is False
    assert "unknown tool" in (result.error or "")


def test_read_file_reads_inside_the_snapshot_tree(make_snapshot: object) -> None:
    snapshot = make_snapshot(extra_files={"files/app.py": "print('hi')\n"})  # type: ignore[operator]
    registry = _registry(snapshot)

    result = registry.execute("read_file", {"path": "app.py"})

    assert result.ok is True
    assert result.content == {"path": "app.py", "text": "print('hi')\n"}


def test_read_file_rejects_traversal_and_absolute_paths(make_snapshot: object) -> None:
    snapshot = make_snapshot(extra_files={"files/app.py": "ok", "secret.txt": "secret"})  # type: ignore[operator]
    registry = _registry(snapshot)

    traversal = registry.execute("read_file", {"path": "../secret.txt"})
    absolute = registry.execute("read_file", {"path": "/etc/passwd"})

    assert traversal.ok is False
    assert "traversal" in (traversal.error or "")
    assert absolute.ok is False
    assert "relative" in (absolute.error or "")


def test_read_file_rejects_symlink_escape(make_snapshot: object, tmp_path: Path) -> None:
    snapshot = make_snapshot()  # type: ignore[operator]
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    files = snapshot.directory / "files"
    files.mkdir()
    (files / "link").symlink_to(outside)
    registry = _registry(snapshot)

    result = registry.execute("read_file", {"path": "link"})

    assert result.ok is False
    assert "escapes" in (result.error or "")


def test_read_file_rejects_oversized_files(make_snapshot: object) -> None:
    snapshot = make_snapshot(extra_files={"files/big.txt": "x" * 100_001})  # type: ignore[operator]
    registry = _registry(snapshot)

    result = registry.execute("read_file", {"path": "big.txt"})

    assert result.ok is False
    assert "too large" in (result.error or "")


def test_invalid_arguments_are_reported(make_snapshot: object) -> None:
    registry = _registry(make_snapshot())  # type: ignore[operator]

    result = registry.execute("read_file", {})

    assert result.ok is False
    assert "invalid arguments" in (result.error or "")


def test_git_log_respects_limit_and_missing_history(make_snapshot: object) -> None:
    snapshot = make_snapshot(extra_files={"git_log.txt": "abc one\ndef two\nghi three\n"})  # type: ignore[operator]
    registry = _registry(snapshot)

    limited = registry.execute("git_log", {"limit": 2})
    missing = _registry(make_snapshot(run_id="r_missing")).execute("git_log", {})  # type: ignore[operator]

    assert limited.ok is True
    assert limited.content["commits"] == ["abc one", "def two"]
    assert limited.content["truncated"] is True
    assert missing.ok is False


def test_git_diff_filters_by_path(make_snapshot: object) -> None:
    diff = (
        "diff --git a/app.py b/app.py\n+one\n"
        "diff --git a/tests/test_app.py b/tests/test_app.py\n+two\n"
    )
    snapshot = make_snapshot(diff=diff)  # type: ignore[operator]
    registry = _registry(snapshot)

    result = registry.execute("git_diff", {"path": "tests/test_app.py"})

    assert result.ok is True
    assert "tests/test_app.py" in result.content["diff"]
    assert "app.py\n+one" not in result.content["diff"]


def test_search_memory_matches_signature_and_query(make_snapshot: object) -> None:
    notes = [
        MemoryNote(
            signature="sig_v1:test",
            repo="acme/widgets",
            note="Timeout in integration tests.",
            created_at=datetime(2026, 11, 1, tzinfo=UTC),
            evidence_runs=[],
        ),
        MemoryNote(
            signature="sig_v1:other",
            note="Dependency mirror outage.",
            created_at=datetime(2026, 11, 1, tzinfo=UTC),
            evidence_runs=[],
        ),
    ]
    snapshot = make_snapshot(memory=notes)  # type: ignore[operator]
    registry = _registry(snapshot)

    by_signature = registry.execute("search_memory", {"signature": "sig_v1:test"})
    by_query = registry.execute("search_memory", {"query": "mirror"})
    empty = registry.execute("search_memory", {})

    assert by_signature.content["matches"][0]["signature"] == "sig_v1:test"
    assert by_query.content["matches"][0]["signature"] == "sig_v1:other"
    assert empty.ok is False


def test_comment_is_simulated_in_replay(make_snapshot: object) -> None:
    snapshot = make_snapshot()  # type: ignore[operator]
    registry = ToolRegistry(
        allowed_tools=("comment_on_pr",),
        context=ToolContext(snapshot=snapshot, run_id=snapshot.metadata.run_id, replay=True),
        allow_act_tools=True,
    )

    result = registry.execute("comment_on_pr", {"body": "hello"})

    assert result.ok is True
    assert result.content == {"posted": False, "simulated": True, "pr_number": 7}


def test_comment_posts_when_live_and_pr_exists(make_snapshot: object) -> None:
    snapshot = make_snapshot()  # type: ignore[operator]

    class FakeGitHub:
        def __init__(self) -> None:
            self.posts: list[tuple[str, int, str]] = []

        def list_issue_comments(self, repo: str, number: int) -> list[GitHubIssueComment]:
            return []

        def post_issue_comment(self, repo: str, number: int, body: str) -> int:
            self.posts.append((repo, number, body))
            return 99

    github = FakeGitHub()
    registry = ToolRegistry(
        allowed_tools=("comment_on_pr",),
        context=ToolContext(
            snapshot=snapshot,
            run_id=snapshot.metadata.run_id,
            dry_run=False,
            replay=False,
            github=github,
        ),
        allow_act_tools=True,
    )

    result = registry.execute("comment_on_pr", {"body": "hello"})

    assert result.ok is True
    assert result.content == {"posted": True, "comment_id": 99}
    assert github.posts == [("acme/widgets", 7, "hello")]
