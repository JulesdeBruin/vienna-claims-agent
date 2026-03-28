"""Daily scheduler — runs the claims pipeline on a recurring schedule.

Uses only stdlib (threading, signal, time) — zero extra dependencies.
"""

import logging
import signal
import time
from datetime import datetime, timedelta
from typing import Callable

from .config import settings
from .orchestrator import Orchestrator, PipelineResult

logger = logging.getLogger(__name__)


class Scheduler:
    """Run the claims pipeline daily at a configured time."""

    def __init__(
        self,
        orchestrator: Orchestrator,
        on_complete: Callable[[PipelineResult], None] | None = None,
    ):
        self.orchestrator = orchestrator
        self.on_complete = on_complete
        self._running = False

    def _next_run_time(self, hour: int, minute: int) -> datetime:
        """Calculate the next occurrence of HH:MM (local time)."""
        now = datetime.now()
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        return target

    def _sleep_until(self, target: datetime) -> bool:
        """Sleep until target time. Returns False if interrupted."""
        while self._running:
            remaining = (target - datetime.now()).total_seconds()
            if remaining <= 0:
                return True
            # Sleep in 10-second chunks to allow clean shutdown
            time.sleep(min(remaining, 10))
        return False

    def run_once(self) -> PipelineResult:
        """Execute a single pipeline run immediately."""
        logger.info("Running pipeline (one-shot)...")
        result = self.orchestrator.run_pipeline()
        if self.on_complete:
            self.on_complete(result)
        return result

    def start(self, hour: int | None = None, minute: int | None = None) -> None:
        """Start the daily scheduler. Blocks until stop() is called.

        Args:
            hour: Hour to run (0-23). Defaults to settings.schedule_hour.
            minute: Minute to run (0-59). Defaults to settings.schedule_minute.
        """
        hour = hour if hour is not None else settings.schedule_hour
        minute = minute if minute is not None else settings.schedule_minute
        self._running = True

        # Register signal handlers for clean shutdown
        def handle_signal(signum, frame):
            logger.info("Received signal %s — shutting down scheduler.", signum)
            self._running = False

        signal.signal(signal.SIGINT, handle_signal)
        signal.signal(signal.SIGTERM, handle_signal)

        logger.info(
            "Scheduler started. Pipeline will run daily at %02d:%02d local time.",
            hour,
            minute,
        )
        logger.info("Press Ctrl+C to stop.")

        while self._running:
            next_run = self._next_run_time(hour, minute)
            wait_seconds = (next_run - datetime.now()).total_seconds()
            logger.info(
                "Next pipeline run: %s (in %.0f minutes)",
                next_run.strftime("%Y-%m-%d %H:%M"),
                wait_seconds / 60,
            )

            if not self._sleep_until(next_run):
                break  # Interrupted

            if self._running:
                try:
                    result = self.run_once()
                    logger.info("Pipeline completed: %d drafts created", result.drafts_created)
                except Exception as e:
                    logger.error("Pipeline failed: %s", e)

        logger.info("Scheduler stopped.")

    def stop(self) -> None:
        """Signal the scheduler to stop."""
        self._running = False
