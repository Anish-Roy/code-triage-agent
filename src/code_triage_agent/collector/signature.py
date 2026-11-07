"""Failure signatures: normalize the error tail, then hash it.

A signature identifies "the same failure" across runs. The algorithm is
versioned in the prefix (``sig_v1:``) so a change to normalization never
silently matches old records.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence

from code_triage_agent.collector.text import strip_ansi
from code_triage_agent.collector.trim import ERROR_KEYWORDS
from code_triage_agent.models import SignatureResult

SIGNATURE_PREFIX = "sig_v1:"
MAX_SIGNATURE_LINES = 80
MAX_EXEMPLAR_CHARS = 1000

_URL = re.compile(r"https?://[^\s\"'<>]+")
_TIMESTAMP = re.compile(r"\b\d{4}-\d{2}-\d{2}[t ]\d{2}:\d{2}:\d{2}(?:\.\d+)?z?\b")
_MEMORY_ADDRESS = re.compile(r"\b0x[0-9a-f]+\b")
_DURATION = re.compile(
    r"\b\d+(?:\.\d+)?\s?(?:ms|s|secs?|seconds?|m|mins?|minutes?|h|hrs?|hours?)\b"
)
_WINDOWS_PATH = re.compile(r"\b[a-z]:\\[^\s]+")
_UNIX_PATH = re.compile(r"(?:/[\w.@%+\-]+){2,}")
_WORKER_ID = re.compile(r"\b(?:worker|pid|process|runner|thread)[-_ ]?(?:id)?[-_ ]?\d+\b")
_HEX = re.compile(r"\b[0-9a-f]{7,40}\b")
_LOCAL_ADDRESS = re.compile(r"\b(?:localhost|127\.0\.0\.1|0\.0\.0\.0):\d{2,5}\b")
_PORT = re.compile(r"(?<![\d:]):\d{2,5}\b")
_NUMBER = re.compile(r"\b\d+\b")

_REPLACEMENTS: tuple[tuple[re.Pattern[str], str], ...] = (
    (_URL, "<url>"),
    (_TIMESTAMP, "<ts>"),
    (_MEMORY_ADDRESS, "<addr>"),
    (_DURATION, "<duration>"),
    (_WINDOWS_PATH, "<path>"),
    (_UNIX_PATH, "<path>"),
    (_WORKER_ID, "<worker>"),
    (_HEX, "<hex>"),
    (_LOCAL_ADDRESS, "<host>"),
    (_PORT, ":<port>"),
    (_NUMBER, "<n>"),
)


def normalize_line(line: str) -> str:
    normalized = strip_ansi(line).strip().lower()
    for pattern, replacement in _REPLACEMENTS:
        normalized = pattern.sub(replacement, normalized)
    return re.sub(r"\s+", " ", normalized).strip()


def build_signature(*, job_name: str, step_name: str | None, text: str) -> SignatureResult:
    """Hash the normalized tail of the error output for one failed job."""
    lines = [normalize_line(line) for line in strip_ansi(text).splitlines()]
    non_empty = [line for line in lines if line]
    errors = [line for line in non_empty if ERROR_KEYWORDS.search(line)]
    selected = errors[-MAX_SIGNATURE_LINES:] if errors else non_empty[-MAX_SIGNATURE_LINES:]
    payload = "\n".join([normalize_line(job_name), normalize_line(step_name or ""), *selected])
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return SignatureResult(value=f"{SIGNATURE_PREFIX}{digest}", exemplar=_exemplar(text))


def _exemplar(text: str) -> str:
    lines = [line.strip() for line in strip_ansi(text).splitlines() if line.strip()]
    errors = [line for line in lines if ERROR_KEYWORDS.search(line)]
    chosen = errors[-5:] if errors else lines[-5:]
    exemplar = "\n".join(chosen)
    if len(exemplar) > MAX_EXEMPLAR_CHARS:
        exemplar = exemplar[-MAX_EXEMPLAR_CHARS:]
    return exemplar


def normalize_lines(lines: Sequence[str]) -> list[str]:
    """Exposed for tests and for callers that already split the log."""
    return [normalize_line(line) for line in lines]
