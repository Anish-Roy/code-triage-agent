"""Secret redaction.

Redaction runs before a snapshot is written and before text can reach a prompt.
Patterns are intentionally conservative: when in doubt, redact.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field


@dataclass(frozen=True)
class RedactionPattern:
    name: str
    pattern: re.Pattern[str]
    secret_group: int | None = None


@dataclass(frozen=True)
class RedactionResult:
    text: str
    counts: dict[str, int] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return sum(self.counts.values())


_URL_CREDENTIALS = re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://[^/\s:@]+:)([^@\s/]+)(@)")

DEFAULT_PATTERNS: tuple[RedactionPattern, ...] = (
    RedactionPattern(
        "private_key",
        re.compile(
            r"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----.*?"
            r"-----END (?:[A-Z0-9 ]+ )?PRIVATE KEY-----",
            re.DOTALL,
        ),
    ),
    RedactionPattern(
        "github_token",
        re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"),
    ),
    RedactionPattern(
        "jwt",
        re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
    ),
    RedactionPattern(
        "authorization_header",
        re.compile(r"(?i)(\bauthorization\s*[:=]\s*(?:bearer|token|basic)\s+)([^\s\"',;]+)"),
        secret_group=2,
    ),
    RedactionPattern(
        "bearer_token",
        re.compile(r"(?i)\b(bearer\s+)([A-Za-z0-9._~+/=\-]{16,})"),
        secret_group=2,
    ),
    RedactionPattern(
        "env_secret",
        re.compile(
            r"(?im)\b((?:export[ \t]+)?[A-Za-z0-9_\-]*"
            r"(?:TOKEN|PASSWORD|PASSWD|SECRET|API_?KEY|ACCESS_KEY|PRIVATE_KEY)"
            r"[A-Za-z0-9_\-]*[ \t]*[=:][ \t]*)([\"']?)([^\s\"'\r\n]+)\2"
        ),
        secret_group=3,
    ),
    RedactionPattern(
        "json_secret",
        re.compile(
            r"(?i)(\"[A-Za-z0-9_\-]*"
            r"(?:token|password|secret|api_?key|access_key)[A-Za-z0-9_\-]*\"\s*:\s*\")([^\"]+)(\")"
        ),
        secret_group=2,
    ),
    RedactionPattern(
        "api_key_header",
        re.compile(r"(?im)((?:x[-_])?api[-_]key[ \t]*:[ \t]*)([^\s\"']+)"),
        secret_group=2,
    ),
    RedactionPattern(
        "aws_access_key",
        re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    ),
    RedactionPattern(
        "aws_secret_key",
        re.compile(r"(?i)(aws_secret_access_key\s*[=:]\s*)([A-Za-z0-9/+=]{40})"),
        secret_group=2,
    ),
    RedactionPattern(
        "gcp_api_key",
        re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"),
    ),
    RedactionPattern(
        "slack_token",
        re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}\b"),
    ),
    RedactionPattern(
        "npm_token",
        re.compile(r"\bnpm_[A-Za-z0-9]{36}\b"),
    ),
    RedactionPattern(
        "url_credentials",
        _URL_CREDENTIALS,
        secret_group=2,
    ),
)


class Redactor:
    """Applies the default patterns plus caller-supplied regexes."""

    def __init__(self, extra_patterns: Sequence[str] = ()) -> None:
        patterns = list(DEFAULT_PATTERNS)
        for index, expression in enumerate(extra_patterns):
            try:
                compiled = re.compile(expression)
            except re.error as exc:
                raise ValueError(f"invalid extra redaction pattern {expression!r}: {exc}") from exc
            patterns.append(RedactionPattern(f"custom_{index}", compiled))
        self._patterns = tuple(patterns)

    def redact(self, text: str) -> RedactionResult:
        counts: Counter[str] = Counter()
        redacted = text
        for rule in self._patterns:
            redacted, hits = _apply(redacted, rule)
            if hits:
                counts[rule.name] += hits
        return RedactionResult(text=redacted, counts=dict(counts))

    @property
    def pattern_names(self) -> tuple[str, ...]:
        return tuple(rule.name for rule in self._patterns)


def _apply(text: str, rule: RedactionPattern) -> tuple[str, int]:
    marker = f"[REDACTED:{rule.name}]"
    changes = 0

    def replace(match: re.Match[str]) -> str:
        nonlocal changes
        whole = match.group(0)
        if rule.secret_group is not None:
            secret = match.group(rule.secret_group)
            if secret.startswith("[REDACTED:"):
                return whole
            start = match.start(rule.secret_group) - match.start(0)
            end = match.end(rule.secret_group) - match.start(0)
            changes += 1
            return f"{whole[:start]}{marker}{whole[end:]}"
        changes += 1
        return marker

    redacted = rule.pattern.sub(replace, text)
    return redacted, changes
