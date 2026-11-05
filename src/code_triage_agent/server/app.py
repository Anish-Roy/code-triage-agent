"""FastAPI application factory."""

from __future__ import annotations

import logging
import sqlite3

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from code_triage_agent import __version__
from code_triage_agent.config import ConfigError, load_config
from code_triage_agent.settings import Settings

logger = logging.getLogger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    app = FastAPI(title="Code Triage Agent", version=__version__)
    app.state.settings = settings

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
    try:
        settings.database_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(settings.database_path) as connection:
            connection.execute("SELECT 1")
    except (OSError, sqlite3.Error) as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "path": str(settings.database_path)}


app = create_app()
