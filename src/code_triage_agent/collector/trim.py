"""Trim noisy CI logs down to the failed step and its error window."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from code_triage_agent.collector.text import strip_ansi
from code_triage_agent.models import JobLogTrim, LineRange, TrimResult

MAX_TRIM_BYTES = 200 * 1024
MAX_FAILED_JOBS = 3
DEFAULT_BEFORE = 300
DEFAULT_AFTER = 50
HEADER_LINES = 20
TAIL_LINES = 10
MIN_JOB_BUDGET = 512

ERROR_KEYWORDS = re.compile(
    r"(?i)(?:##\[error\]|\berror\b|\bfailed\b|\bfailure\b|\bfatal\b|"
    r"\bexception\b|\btraceback\b|\bpanic\b|\bassert(?:ion)?\b|"
    r"\btimed? out\b|\btimeout\b)"
)
GROUP_START = re.compile(r"##\[group\](.*)$")
GROUP_END = re.compile(r"##\[endgroup\]\s*$")


@dataclass(frozen=True)
class JobLogInput:
    job_id: int
    job_name: str
    failed_step: str | None
    text: str


@dataclass(frozen=True)
class _Section:
    name: str
    start: int  # 0-based, inclusive, marker lines excluded
    end: int  # 0-based, inclusive


def parse_sections(lines: Sequence[str]) -> list[_Section]:
    """Parse ``##[group]`` markers, supporting nesting inside a step block."""
    sections: list[_Section] = []
    stack: list[tuple[str, int]] = []
    for index, line in enumerate(lines):
        start = GROUP_START.search(line)
        if start is not None:
            stack.append((start.group(1).strip(), index))
            continue
        if GROUP_END.search(line) and stack:
            name, opened_at = stack.pop()
            sections.append(_Section(name=name, start=opened_at + 1, end=index - 1))
    for name, opened_at in stack:
        sections.append(_Section(name=name, start=opened_at + 1, end=len(lines) - 1))
    return sections


def trim_logs(
    logs: Sequence[JobLogInput],
    *,
    max_bytes: int = MAX_TRIM_BYTES,
    max_jobs: int = MAX_FAILED_JOBS,
    before: int = DEFAULT_BEFORE,
    after: int = DEFAULT_AFTER,
) -> TrimResult:
    """Return a size-capped, structured trim across up to ``max_jobs`` logs."""
    inputs = list(logs)
    selected = inputs[:max_jobs]
    dropped = len(inputs) - len(selected)

    per_job = max(max_bytes // max(len(selected), 1), MIN_JOB_BUDGET)
    job_results: list[JobLogTrim] = []
    chunks: list[str] = []
    used = 0

    for index, log in enumerate(selected):
        separator = f"\n===== job: {log.job_name} =====\n" if chunks else ""
        remaining = max_bytes - used - len(separator.encode("utf-8"))
        if remaining < MIN_JOB_BUDGET:
            dropped += len(selected) - index
            break

        budget = remaining if index == len(selected) - 1 else min(per_job, remaining)
        trimmed, text = _trim_one(
            log,
            max_bytes=budget,
            before=before,
            after=after,
        )
        if not text:
            job_results.append(trimmed)
            continue

        chunk = separator + text
        chunks.append(chunk)
        used += len(chunk.encode("utf-8"))
        job_results.append(trimmed)

    combined = "".join(chunks)
    return TrimResult(
        text=combined,
        jobs=job_results,
        dropped_jobs=dropped,
        total_bytes=len(combined.encode("utf-8")),
    )


def _trim_one(
    log: JobLogInput,
    *,
    max_bytes: int,
    before: int,
    after: int,
) -> tuple[JobLogTrim, str]:
    lines = strip_ansi(log.text).splitlines()
    total_lines = len(lines)
    if total_lines == 0:
        return (
            JobLogTrim(
                job_id=log.job_id,
                job_name=log.job_name,
                step_name=log.failed_step,
                total_lines=0,
            ),
            "",
        )

    sections = parse_sections(lines)
    error_index = _first_error_index(lines)
    section = _focus_section(sections, log.failed_step, error_index)

    if section is not None:
        if error_index is not None and section.start <= error_index <= section.end:
            start = max(section.start, error_index - before)
            end = min(section.end, error_index + after)
        else:
            start = section.start
            end = min(section.end, section.start + before + after)
    elif error_index is not None:
        start = max(0, error_index - before)
        end = min(total_lines - 1, error_index + after)
    else:
        start = max(0, total_lines - (before + after))
        end = total_lines - 1

    window = list(range(max(start, 0), min(end, total_lines - 1) + 1))
    header = list(range(min(HEADER_LINES, total_lines)))
    tail = list(range(max(0, total_lines - TAIL_LINES), total_lines))

    ordered = _unique(window + header + tail)
    selected: list[int] = []
    size = 0
    truncated = False
    for line_index in ordered:
        line_bytes = len(lines[line_index].encode("utf-8")) + 1
        if size + line_bytes > max_bytes:
            truncated = True
            break
        selected.append(line_index)
        size += line_bytes

    selected.sort()
    text = "\n".join(lines[index] for index in selected)
    result = JobLogTrim(
        job_id=log.job_id,
        job_name=log.job_name,
        step_name=log.failed_step,
        line_ranges=_to_ranges(selected),
        kept_lines=len(selected),
        total_lines=total_lines,
        truncated=truncated,
    )
    return result, text


def _first_error_index(lines: Sequence[str]) -> int | None:
    for index, line in enumerate(lines):
        if "##[error]" in line:
            return index
    for index, line in enumerate(lines):
        if ERROR_KEYWORDS.search(line):
            return index
    return None


def _focus_section(
    sections: Sequence[_Section],
    failed_step: str | None,
    error_index: int | None,
) -> _Section | None:
    if failed_step:
        target = _normalize_name(failed_step)
        matches = [
            section
            for section in sections
            if target
            and (target in _normalize_name(section.name) or _normalize_name(section.name) in target)
        ]
        for section in matches:
            if error_index is not None and section.start <= error_index <= section.end:
                return section
        if matches:
            return matches[0]
    if error_index is not None:
        for section in sections:
            if section.start <= error_index <= section.end:
                return section
    return None


def _normalize_name(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().lower()


def _unique(indices: Sequence[int]) -> list[int]:
    seen: set[int] = set()
    ordered: list[int] = []
    for index in indices:
        if index not in seen:
            seen.add(index)
            ordered.append(index)
    return ordered


def _to_ranges(indices: Sequence[int]) -> list[LineRange]:
    if not indices:
        return []
    ranges: list[LineRange] = []
    start = previous = indices[0]
    for index in indices[1:]:
        if index == previous + 1:
            previous = index
            continue
        ranges.append(LineRange(start=start + 1, end=previous + 1))
        start = previous = index
    ranges.append(LineRange(start=start + 1, end=previous + 1))
    return ranges
