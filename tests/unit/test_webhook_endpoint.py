from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from code_triage_agent.db import Database, list_events
from code_triage_agent.github.webhook import compute_signature
from code_triage_agent.server.app import create_app
from code_triage_agent.settings import Settings

PROJECT_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "github"
SECRET = "test-webhook-secret"


def _settings(tmp_path: Path, *, secret: str | None = SECRET) -> Settings:
    return Settings(
        _env_file=None,
        config_dir=PROJECT_ROOT / "config",
        database_path=tmp_path / "var" / "triage.db",
        snapshot_root=tmp_path / "snapshots",
        event_root=tmp_path / "events",
        github_webhook_secret=secret,
    )


def _body() -> bytes:
    return (FIXTURES / "workflow_run_event.json").read_bytes()


def _headers(
    body: bytes,
    *,
    event: str = "workflow_run",
    delivery: str | None = "delivery-1",
    secret: str = SECRET,
) -> dict[str, str]:
    headers = {
        "X-GitHub-Event": event,
        "X-Hub-Signature-256": compute_signature(secret, body),
        "Content-Type": "application/json",
    }
    if delivery is not None:
        headers["X-GitHub-Delivery"] = delivery
    return headers


def _client(tmp_path: Path, *, secret: str | None = SECRET) -> tuple[TestClient, Settings]:
    settings = _settings(tmp_path, secret=secret)
    return TestClient(create_app(settings)), settings


def _events(settings: Settings) -> list:
    database = Database(settings.database_path)
    with database.connection() as connection:
        return list_events(connection)


def test_accepts_and_persists_failure(tmp_path: Path) -> None:
    client, settings = _client(tmp_path)
    body = _body()

    response = client.post("/webhooks/github", content=body, headers=_headers(body))

    assert response.status_code == 202
    payload = response.json()
    assert payload["status"] == "accepted"
    events = _events(settings)
    assert len(events) == 1
    event = events[0]
    assert event.id == payload["event_id"]
    assert event.status == "pending"
    assert event.repo == "acme/widgets"
    assert event.workflow_run_id == 9001
    assert event.run_attempt == 1
    assert event.conclusion == "failure"
    assert Path(event.payload_path).is_file()
    assert json.loads(Path(event.payload_path).read_text()) == json.loads(body)


def test_duplicate_delivery_is_idempotent(tmp_path: Path) -> None:
    client, settings = _client(tmp_path)
    body = _body()

    first = client.post("/webhooks/github", content=body, headers=_headers(body))
    second = client.post("/webhooks/github", content=body, headers=_headers(body))

    assert first.status_code == 202
    assert second.status_code == 202
    assert second.json()["status"] == "duplicate"
    assert len(_events(settings)) == 1


def test_duplicate_run_with_new_delivery_is_ignored(tmp_path: Path) -> None:
    client, settings = _client(tmp_path)
    body = _body()

    client.post("/webhooks/github", content=body, headers=_headers(body, delivery="d-1"))
    response = client.post("/webhooks/github", content=body, headers=_headers(body, delivery="d-2"))

    assert response.json()["status"] == "duplicate"
    assert len(_events(settings)) == 1


def test_bad_signature_is_rejected(tmp_path: Path) -> None:
    client, settings = _client(tmp_path)
    body = _body()
    headers = _headers(body)
    headers["X-Hub-Signature-256"] = "sha256=" + "0" * 64

    response = client.post("/webhooks/github", content=body, headers=headers)

    assert response.status_code == 401
    assert response.json()["reason"] == "invalid signature"
    assert _events(settings) == []


def test_missing_delivery_header_is_rejected(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    body = _body()

    response = client.post("/webhooks/github", content=body, headers=_headers(body, delivery=None))

    assert response.status_code == 400
    assert "X-GitHub-Delivery" in response.json()["reason"]


def test_success_conclusion_is_ignored(tmp_path: Path) -> None:
    client, settings = _client(tmp_path)
    payload = json.loads(_body())
    payload["workflow_run"]["conclusion"] = "success"
    body = json.dumps(payload).encode()

    response = client.post("/webhooks/github", content=body, headers=_headers(body))

    assert response.status_code == 202
    assert response.json() == {"status": "ignored", "reason": "not a completed failure"}
    assert _events(settings) == []


def test_non_workflow_event_is_ignored(tmp_path: Path) -> None:
    client, settings = _client(tmp_path)
    body = _body()

    response = client.post("/webhooks/github", content=body, headers=_headers(body, event="push"))

    assert response.status_code == 202
    assert response.json() == {"status": "ignored", "reason": "event"}
    assert _events(settings) == []


def test_malformed_payload_is_rejected(tmp_path: Path) -> None:
    client, settings = _client(tmp_path)
    body = b"{not json"

    response = client.post("/webhooks/github", content=body, headers=_headers(body))

    assert response.status_code == 400
    assert "malformed JSON" in response.json()["reason"]
    assert (settings.event_root / "delivery-1").exists() is False
    assert _events(settings) == []


def test_missing_secret_fails_closed(tmp_path: Path) -> None:
    client, settings = _client(tmp_path, secret=None)
    body = _body()
    headers = _headers(body)
    headers.pop("X-Hub-Signature-256")

    response = client.post("/webhooks/github", content=body, headers=headers)

    assert response.status_code == 503
    assert _events(settings) == []
