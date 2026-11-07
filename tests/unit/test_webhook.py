from __future__ import annotations

import json
from pathlib import Path

import pytest

from code_triage_agent.github.webhook import (
    WebhookError,
    compute_signature,
    is_workflow_run_event,
    parse_payload,
    parse_workflow_run_event,
    safe_delivery_id,
    verify_signature,
    write_event_payload,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "github"


def _payload_bytes() -> bytes:
    return (FIXTURES / "workflow_run_event.json").read_bytes()


def test_signature_roundtrip_and_tampering() -> None:
    body = b'{"hello": "world"}'
    signature = compute_signature("secret", body)

    assert verify_signature("secret", body, signature) is True
    assert verify_signature("secret", body + b" ", signature) is False
    assert verify_signature("other", body, signature) is False
    assert verify_signature("secret", body, None) is False
    assert verify_signature("secret", body, "sha1=deadbeef") is False


def test_parse_payload_rejects_bad_json() -> None:
    with pytest.raises(WebhookError, match="malformed JSON"):
        parse_payload(b"{not json}")

    with pytest.raises(WebhookError, match="JSON object"):
        parse_payload(b"[]")


def test_parse_workflow_run_event() -> None:
    event = parse_workflow_run_event(parse_payload(_payload_bytes()))

    assert event.repository.full_name == "acme/widgets"
    assert event.workflow_run.id == 9001
    assert event.workflow_run.run_attempt == 1
    assert event.workflow_run.conclusion == "failure"
    assert event.source == "pull_request"
    assert event.is_actionable is True
    assert event.dedupe_key == ("acme/widgets", 9001, 1)


def test_parse_workflow_run_event_rejects_missing_fields() -> None:
    with pytest.raises(WebhookError, match="invalid workflow_run payload"):
        parse_workflow_run_event({"action": "completed"})


def test_non_failure_and_wrong_action_are_not_actionable() -> None:
    payload = parse_payload(_payload_bytes())
    payload["workflow_run"]["conclusion"] = "success"
    success = parse_workflow_run_event(payload)
    assert success.is_actionable is False

    payload = parse_payload(_payload_bytes())
    payload["action"] = "requested"
    assert parse_workflow_run_event(payload).is_actionable is False


def test_source_mapping() -> None:
    push = parse_payload(_payload_bytes())
    push["workflow_run"]["event"] = "push"
    assert parse_workflow_run_event(push).source == "push"

    schedule = parse_payload(_payload_bytes())
    schedule["workflow_run"]["event"] = "schedule"
    assert parse_workflow_run_event(schedule).source == "schedule"

    unknown = parse_payload(_payload_bytes())
    unknown["workflow_run"]["event"] = "workflow_dispatch"
    assert parse_workflow_run_event(unknown).source == "any"


def test_event_name_filter() -> None:
    assert is_workflow_run_event("workflow_run") is True
    assert is_workflow_run_event("push") is False
    assert is_workflow_run_event(None) is False


@pytest.mark.parametrize("value", ["../escape", "a/b", "", "with space"])
def test_safe_delivery_id_rejects_traversal(value: str) -> None:
    with pytest.raises(WebhookError):
        safe_delivery_id(value)


def test_write_event_payload_is_idempotent(tmp_path: Path) -> None:
    body = json.dumps({"ok": True}).encode()

    first = write_event_payload(tmp_path, "delivery-1", body)
    second = write_event_payload(tmp_path, "delivery-1", body)

    assert first == second
    assert first.read_bytes() == body
    assert first == tmp_path / "delivery-1" / "payload.json"
