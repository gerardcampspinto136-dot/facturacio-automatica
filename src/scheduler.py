"""Background jobs: pending-review reminders, the money digest, and low-stock alerts.

Each job is wrapped so that a failure logs and the scheduler keeps running -- a Telegram
outage must not silently stop every future reminder.
"""

import logging

from apscheduler.schedulers.background import BackgroundScheduler

from src import bills, catalog, store
from src.config_loader import get_config
from src.notify import send_low_stock_alert, send_money_digest, send_pending_reminder

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


def _pending_tick() -> None:
    try:
        count = store.count_pending()
        if count <= 0:
            return
        send_pending_reminder(count, get_config().web_base_url)
    except Exception:
        logger.error("Pending-reminder tick failed", exc_info=True)


def _money_tick() -> None:
    """Bills falling due and invoices still unpaid, in one message."""
    try:
        config = get_config()
        due = bills.due_soon(within_days=config.bills_due_within_days)
        unpaid = store.list_unpaid()
        if send_money_digest(due, unpaid):
            logger.info(
                "Money digest sent (%s bills due, %s unpaid invoices)", len(due), len(unpaid)
            )
    except Exception:
        logger.error("Money-digest tick failed", exc_info=True)


def _stock_tick() -> None:
    try:
        if send_low_stock_alert(catalog.low_stock()):
            logger.info("Low-stock alert sent")
    except Exception:
        logger.error("Low-stock tick failed", exc_info=True)


def start_scheduler() -> BackgroundScheduler:
    config = get_config()
    scheduler = BackgroundScheduler(daemon=True)

    scheduler.add_job(
        _pending_tick, "interval",
        **_parse_schedule(config.notify_schedule), id="pending_reminder",
    )
    if config.money_schedule:
        scheduler.add_job(
            _money_tick, "interval",
            **_parse_schedule(config.money_schedule), id="money_digest",
        )
    if config.stock_schedule:
        scheduler.add_job(
            _stock_tick, "interval",
            **_parse_schedule(config.stock_schedule), id="low_stock",
        )

    scheduler.start()
    logger.info(
        "Scheduler started — reviews every %s, money every %s, stock every %s",
        config.notify_schedule, config.money_schedule or "off",
        config.stock_schedule or "off",
    )
    return scheduler
