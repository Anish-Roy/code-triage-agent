"""GitHub webhook signature verification and payload parsing."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from code_triage_agent.models import WorkflowRunEvent

SIGNATURE_HEADER = "X-Hub-Signature-256"
EVENT_HEADER = "X-GitHub-Event"
DELIVERY_HEADER = "X-GitHub-Delivery"
WORKFLOW_RUN_EVENT = "workflow_run"
_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


class WebhookError(ValueError):
    """Raised when a webhook body cannot be trusted or parsed."""


def compute_signature(secret: str, body: bytes) -> str:
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def verify_signature(secret: str, body: bytes, header: str | None) -> bool:
    """Constant-time verification of ``X-Hub-Signature-256``."""
    if not secret or not header:
        return False
    expected = compute_signature(secret, body)
    return hmac.compare_digest(expected, header)


def parse_payload(body: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise WebhookError(f"malformed JSON payload: {exc}") from exc
    if not isinstance(payload, dict):
        raise WebhookError("payload must be a JSON object")
    return payload


def parse_workflow_run_event(payload: dict[str, Any]) -> WorkflowRunEvent:
    try:
        return WorkflowRunEvent.model_validate(payload)
    except ValidationError as exc:
        raise WebhookError(f"invalid workflow_run payload: {exc}") from exc


def is_workflow_run_event(event_name: str | None) -> bool:
    return event_name == WORKFLOW_RUN_EVENT


def safe_delivery_id(delivery_id: str | None) -> str:
    if delivery_id and _SAFE_ID.fullmatch(delivery_id):
        return delivery_id
    raise WebhookError("missing or malformed X-GitHub-Delivery header")


def write_event_payload(event_root: Path, delivery_id: str, body: bytes) -> Path:
    """Persist the raw delivery atomically before any work is queued."""
    if not _SAFE_ID.fullmatch(delivery_id):
        raise WebhookError(f"unsafe delivery id: {delivery_id!r}")
    directory = event_root / delivery_id
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "payload.json"
    _atomic_write(path, body)
    return path


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
