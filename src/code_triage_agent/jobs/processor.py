"""Wires the GitHub client and collector into a worker processor."""

from __future__ import annotations

from code_triage_agent.collector.collect import Collector, GitHubReadClient
from code_triage_agent.collector.redact import Redactor
from code_triage_agent.config.loader import LoadedConfig
from code_triage_agent.db import Database
from code_triage_agent.github.auth import GitHubAppAuth
from code_triage_agent.github.client import GitHubClient
from code_triage_agent.models import EventRecord
from code_triage_agent.settings import Settings


class CollectProcessor:
    """Phase 1 processor: collect and snapshot, with no model call."""

    def __init__(
        self,
        *,
        collector: Collector,
        client: GitHubReadClient | None = None,
    ) -> None:
        self._collector = collector
        self._client = client

    def __call__(self, event: EventRecord) -> None:
        self._collector.collect(event)

    def close(self) -> None:
        close = getattr(self._client, "close", None)
        if callable(close):
            close()


def build_collect_processor(
    settings: Settings,
    config: LoadedConfig,
    database: Database,
    *,
    client: GitHubReadClient | None = None,
) -> CollectProcessor:
    """Build the Phase 1 processor, creating a GitHub App client if needed."""
    owned_client: GitHubClient | None = None
    if client is None:
        if not settings.github_app_configured():
            raise RuntimeError(
                "GitHub App credentials are required to collect: set "
                "CTA_GITHUB_APP_ID, CTA_GITHUB_APP_PRIVATE_KEY and "
                "CTA_GITHUB_INSTALLATION_ID"
            )
        assert settings.github_app_private_key is not None
        assert settings.github_app_id is not None
        assert settings.github_installation_id is not None
        auth = GitHubAppAuth(
            app_id=settings.github_app_id,
            private_key=settings.github_app_private_key.get_secret_value(),
            installation_id=settings.github_installation_id,
        )
        owned_client = GitHubClient(auth=auth)
        client = owned_client

    collector = Collector(
        client=client,
        config=config,
        database=database,
        snapshot_root=settings.snapshot_root,
        redactor=Redactor(settings.redaction_patterns),
    )
    return CollectProcessor(collector=collector, client=client)
