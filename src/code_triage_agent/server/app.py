"""FastAPI application factory."""

from __future__ import annotations

import logging
import sqlite3

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from code_triage_agent import __version__
from code_triage_agent.config import ConfigError, load_config
from code_triage_agent.db import Database, insert_event
from code_triage_agent.github.webhook import (
    DELIVERY_HEADER,
    EVENT_HEADER,
    SIGNATURE_HEADER,
    WebhookError,
    is_workflow_run_event,
    parse_payload,
    parse_workflow_run_event,
    safe_delivery_id,
    verify_signature,
    write_event_payload,
)
from code_triage_agent.logging import bind_context
from code_triage_agent.settings import Settings

logger = logging.getLogger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    app = FastAPI(title="Code Triage Agent", version=__version__)
    app.state.settings = settings
    app.state.database = Database(settings.database_path)

    try:
        settings.ensure_runtime_dirs()
        app.state.database.migrate()
    except (OSError, sqlite3.Error, RuntimeError):
        logger.exception("database initialization failed")

    @app.get("/healthz")
    def healthz() -> JSONResponse:
        checks = {
            "config": _check_config(settings),
            "database": _check_database(settings),
        }
        healthy = all(bool(check.get("ok")) for check in checks.values())
        return JSONResponse(
            {"status": "ok" if healthy else "error", "checks": checks},
            status_code=200 if healthy else 503,
        )

    @app.post("/webhooks/github", status_code=202)
    async def github_webhook(request: Request) -> JSONResponse:
        secret = settings.github_webhook_secret
        if secret is None:
            logger.error("rejecting webhook: CTA_GITHUB_WEBHOOK_SECRET is not set")
            return JSONResponse(
                {"status": "error", "reason": "webhook secret not configured"},
                status_code=503,
            )

        body = await request.body()
        if not verify_signature(
            secret.get_secret_value(), body, request.headers.get(SIGNATURE_HEADER)
        ):
            logger.warning("rejecting webhook: invalid signature")
            return JSONResponse(
                {"status": "error", "reason": "invalid signature"},
                status_code=401,
            )

        if not is_workflow_run_event(request.headers.get(EVENT_HEADER)):
            return JSONResponse({"status": "ignored", "reason": "event"}, status_code=202)

        try:
            delivery_id = safe_delivery_id(request.headers.get(DELIVERY_HEADER))
            wrapped = parse_workflow_run_event(parse_payload(body))
        except WebhookError as exc:
            return JSONResponse(
                {"status": "error", "reason": str(exc)},
                status_code=400,
            )

        if not wrapped.is_actionable:
            return JSONResponse(
                {"status": "ignored", "reason": "not a completed failure"},
                status_code=202,
            )

        with bind_context(delivery_id=delivery_id):
            payload_path = write_event_payload(settings.event_root, delivery_id, body)
            run = wrapped.workflow_run
            with app.state.database.connection() as connection:
                event = insert_event(
                    connection,
                    delivery_id=delivery_id,
                    repo=wrapped.repository.full_name,
                    workflow_run_id=run.id,
                    run_attempt=run.run_attempt,
                    conclusion=str(run.conclusion),
                    payload_path=str(payload_path),
                )
            if event is None:
                logger.info("duplicate webhook delivery ignored")
                return JSONResponse({"status": "duplicate"}, status_code=202)

            logger.info(
                "queued workflow failure",
                extra={"event_id": event.id, "repo": event.repo},
            )
            return JSONResponse({"status": "accepted", "event_id": event.id}, status_code=202)

    if not settings.webhook_configured():
        logger.warning("CTA_GITHUB_WEBHOOK_SECRET is not set; webhook will reject deliveries")

    return app


def _check_config(settings: Settings) -> dict[str, object]:
    try:
        config = load_config(settings.config_dir)
    except ConfigError as exc:
        return {"ok": False, "error": str(exc)}
    return {
        "ok": True,
        "triggers": len(config.triggers),
        "skills": len(config.skills),
        "memory_notes": len(config.memory_notes),
        "config_sha": config.config_sha,
    }


def _check_database(settings: Settings) -> dict[str, object]:
    database = Database(settings.database_path)
    try:
        with database.connection() as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            connection.execute("SELECT 1 FROM events LIMIT 1")
    except (OSError, sqlite3.Error) as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "path": str(settings.database_path), "schema_version": int(version)}


app = create_app()
