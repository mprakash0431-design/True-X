
import logging
from datetime import datetime, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler

LOGGER = logging.getLogger(__name__)

scheduler = AsyncIOScheduler(timezone="UTC")


def start_scheduler(task_status=None, active_tasks=None):
    """Start scheduled maintenance for the leech bot."""

    if scheduler.running:
        LOGGER.info("Scheduler is already running.")
        return

    async def cleanup_task_status():
        """Remove old completed task status entries."""
        if task_status is None:
            return

        now = datetime.now(timezone.utc)
        remove_ids = []

        for user_id, status in list(task_status.items()):
            finished_at = status.get("finished_at")

            if not finished_at:
                continue

            age = (now - finished_at).total_seconds()

            # Keep status for 24 hours.
            if age > 86400:
                remove_ids.append(user_id)

        for user_id in remove_ids:
            task_status.pop(user_id, None)

        if remove_ids:
            LOGGER.info(
                "Removed %s expired task status entries.",
                len(remove_ids),
            )

    async def log_active_tasks():
        """Log the number of active tasks."""
        if active_tasks is None:
            return

        active_count = sum(
            1 for task in active_tasks.values()
            if not task.done()
        )

        LOGGER.debug("Active leech tasks: %s", active_count)

    scheduler.add_job(
        cleanup_task_status,
        trigger="interval",
        minutes=30,
        id="cleanup_task_status",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    scheduler.add_job(
        log_active_tasks,
        trigger="interval",
        minutes=5,
        id="log_active_tasks",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    scheduler.start()
    LOGGER.info("Mini Leech Bot scheduler started.")


def stop_scheduler():
    """Stop scheduled jobs safely."""
    if scheduler.running:
        scheduler.shutdown(wait=False)
        LOGGER.info("Mini Leech Bot scheduler stopped.")
