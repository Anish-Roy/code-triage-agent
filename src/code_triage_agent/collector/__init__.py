"""Collector package."""

from code_triage_agent.collector.collect import CollectedRun, Collector, CollectorError
from code_triage_agent.collector.redact import RedactionResult, Redactor
from code_triage_agent.collector.signature import build_signature
from code_triage_agent.collector.snapshot import SnapshotPaths, load_snapshot, write_snapshot

__all__ = [
    "CollectedRun",
    "Collector",
    "CollectorError",
    "RedactionResult",
    "Redactor",
    "SnapshotPaths",
    "build_signature",
    "load_snapshot",
    "write_snapshot",
]
