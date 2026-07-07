"""Background scheduler that periodically reminds reviewers of pending invoices."""

import logging

from apscheduler.schedulers.background import BackgroundScheduler

from src import store
from src.config_loader import get_config
from src.notify import send_pending_reminder

logger = logging.getLogger(__name__)

_UNITS = {"m": "minutes", "h": "hours", "d": "days", "w": "weeks"}


def _parse_schedule(schedule: str) -> dict:
    """Turn '1d', '3d', '1w', '2h' into APScheduler interval kwargs. Defaults to 1 day."""
    schedule = (schedule or "1d").strip().lower()
    unit = schedule[-1]
    if unit not in _UNITS:
        return {"days": 1}
    try:
        value = int(schedule[:-1])
    except ValueError:
        value = 1
    return {_UNITS[unit]: max(value, 1)}


def _tick() -> None:
    try:
        count = store.count_pending()
        if count <= 0:
            return
        config = get_config()
        send_pending_reminder(count, config.web_base_url)
    except Exception:
        logger.error("Pending-reminder tick failed", exc_info=True)


def start_scheduler() -> BackgroundScheduler:
    config = get_config()
    interval = _parse_schedule(config.notify_schedule)
    scheduler = BackgroundScheduler(daemon=True)
    scheduler.add_job(_tick, "interval", **interval, id="pending_reminder")
    scheduler.start()
    logger.info("Reminder scheduler started (every %s)", config.notify_schedule)
    return scheduler
