"""Render and post the single Phase 2 action: an idempotent PR comment."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from code_triage_agent.models import RecommendedAction, TriageDecision

COMMENT_TOOL = "comment_on_pr"
PHASE2_ALLOWED_ACTIONS = frozenset({COMMENT_TOOL})
MAX_SUMMARY_CHARS = 700
MAX_QUOTE_CHARS = 200
MAX_EVIDENCE_ITEMS = 4


class ActionNotAllowed(RuntimeError):
    """Raised when a trigger tries to perform an action outside Phase 2."""


class CommentClient(Protocol):
    def list_issue_comments(self, repo: str, number: int) -> list[Any]: ...

    def post_issue_comment(self, repo: str, number: int, body: str) -> int: ...


@dataclass(frozen=True)
class CommentResult:
    posted: bool
    body: str
    comment_id: int | None = None
    skipped_reason: str | None = None


def run_marker(run_id: str) -> str:
    return f"<!-- code-triage-agent:run_id={run_id} -->"


def render_comment(
    decision: TriageDecision,
    *,
    run_id: str,
    workflow_name: str | None = None,
    run_url: str | None = None,
) -> str:
    """Render a short, evidence-first comment carrying the run marker."""
    title = f"## CI triage: `{decision.category}` ({decision.confidence:.0%} confidence)"
    if workflow_name:
        title = f"## CI triage: `{decision.category}` ({decision.confidence:.0%} confidence)\n"
        title += f"Workflow: {_inline(workflow_name, 120)}"

    lines = [
        title,
        "",
        _inline(decision.summary, MAX_SUMMARY_CHARS),
    ]

    if decision.evidence:
        lines.extend(["", "**Evidence**"])
        for item in decision.evidence[:MAX_EVIDENCE_ITEMS]:
            quote = _inline(item.quote, MAX_QUOTE_CHARS).replace("`", "'")
            source = _inline(item.source, 20)
            why = _inline(item.why, 240)
            lines.append(f"- `{quote}` ({source}) — {why}")

    lines.extend(["", f"**Next step:** {_next_step(decision.recommended_action)}"])
    if decision.needs_human:
        lines.append("")
        lines.append("A human should review this diagnosis before acting on it.")
    if run_url:
        lines.extend(["", f"[Workflow run]({run_url})"])

    lines.extend(["", run_marker(run_id)])
    return "\n".join(lines).strip() + "\n"


class CommentAction:
    """Posts at most one comment per run, enforced by the HTML marker."""

    def __init__(
        self,
        client: CommentClient | None,
        *,
        dry_run: bool = True,
        replay: bool = False,
    ) -> None:
        self._client = client
        self._dry_run = dry_run
        self._replay = replay

    def post(
        self,
        *,
        repo: str,
        pr_number: int | None,
        decision: TriageDecision,
        run_id: str,
        allowed_actions: tuple[str, ...] | frozenset[str],
        workflow_name: str | None = None,
        run_url: str | None = None,
    ) -> CommentResult:
        if COMMENT_TOOL not in allowed_actions:
            raise ActionNotAllowed(f"{COMMENT_TOOL} is not allowed for this run")
        body = render_comment(
            decision,
            run_id=run_id,
            workflow_name=workflow_name,
            run_url=run_url,
        )
        if pr_number is None:
            return CommentResult(
                posted=False,
                body=body,
                skipped_reason="no pull request associated with this run",
            )

        if self._dry_run or self._replay:
            return CommentResult(posted=False, body=body, skipped_reason="dry_run/replay")

        if self._client is None:
            raise ActionNotAllowed("a GitHub client is required to post a comment")

        marker = run_marker(run_id)
        for comment in self._client.list_issue_comments(repo, pr_number):
            existing = getattr(comment, "body", "")
            if isinstance(existing, str) and marker in existing:
                return CommentResult(
                    posted=False,
                    body=body,
                    comment_id=getattr(comment, "id", None),
                    skipped_reason="comment with this run marker already exists",
                )

        comment_id = self._client.post_issue_comment(repo, pr_number, body)
        return CommentResult(posted=True, body=body, comment_id=comment_id)


def _next_step(action: RecommendedAction) -> str:
    params: dict[str, Any] = action.params
    explicit = params.get("next_step")
    if isinstance(explicit, str) and explicit.strip():
        return _inline(explicit, 300)

    defaults = {
        "comment": "Review the evidence above and decide whether to rerun or fix.",
        "rerun": "Rerun the failed jobs once and compare the failure signature.",
        "fix_pr": "Open a minimal fix pull request for human review.",
        "issue": "Open an issue with the evidence attached.",
        "none": "No action recommended.",
    }
    return defaults.get(action.type, defaults["none"])


def _inline(text: str, limit: int) -> str:
    collapsed = " ".join(str(text).split()).replace("<!--", "&lt;!--")
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 1] + "…"
