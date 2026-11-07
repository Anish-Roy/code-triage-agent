"""Single-process worker that claims events and runs the collector."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from datetime import datetime

from code_triage_agent.db import (
    Database,
    claim_next_event,
    mark_event_done,
    mark_event_failed,
    utcnow,
)
from code_triage_agent.logging import bind_context
from code_triage_agent.models import EventRecord

logger = logging.getLogger(__name__)

Processor = Callable[[EventRecord], None]


class Worker:
    """Poll-claim-process loop with retries, backoff, and crash recovery.

    Claiming uses ``BEGIN IMMEDIATE`` so two workers cannot process the same row.
    A row left in ``processing`` past the lease is reclaimed, which covers a
    process that died after claiming.
    """

    def __init__(
        self,
        *,
        database: Database,
        processor: Processor,
        poll_interval: float = 2.0,
        lease_seconds: int = 600,
        max_attempts: int = 5,
        base_backoff_seconds: int = 60,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._database = database
        self._processor = processor
        self._poll_interval = poll_interval
        self._lease_seconds = lease_seconds
        self._max_attempts = max_attempts
        self._base_backoff_seconds = base_backoff_seconds
        self._clock = clock or utcnow

    def run_once(self) -> bool:
        """Claim and process at most one event. Returns True if work was found."""
        with self._database.connection() as connection:
            event = claim_next_event(
                connection,
                now=self._clock(),
                lease_seconds=self._lease_seconds,
            )
        if event is None:
            return False

        with bind_context(delivery_id=event.delivery_id, event_id=event.id):
            logger.info("processing event")
            try:
                self._processor(event)
            except Exception as exc:
                logger.exception("event processing failed")
                with self._database.connection() as connection:
                    mark_event_failed(
                        connection,
                        event.id,
                        error=f"{type(exc).__name__}: {exc}",
                        now=self._clock(),
                        max_attempts=self._max_attempts,
                        base_backoff_seconds=self._base_backoff_seconds,
                    )
            else:
                with self._database.connection() as connection:
                    mark_event_done(connection, event.id, now=self._clock())
                logger.info("event processed")
        return True

    def run_forever(self, stop: threading.Event | None = None) -> None:
        shutdown = stop or threading.Event()
        logger.info("worker started", extra={"poll_interval": self._poll_interval})
        try:
            while not shutdown.is_set():
                if not self.run_once():
                    shutdown.wait(self._poll_interval)
        finally:
            logger.info("worker stopped")
