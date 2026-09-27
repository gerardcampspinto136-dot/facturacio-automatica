"""Background jobs: reminders, digests, and the daily chores that act on their own.

Each job has a period ("1d", "1w"...) and runs at a set hour of the day. The time of its
last run is kept in the database, and a tick every few minutes runs whatever is due.

That replaces plain "every N days" timers, which count from the moment the program
started. On a computer that is switched off at night -- where this software usually
lives -- a weekly timer restarted every morning never once reached a week, so the money
digest simply never arrived. Now a job that was due while the computer was off runs at
the next tick after it comes back, and never at night.

Each job is wrapped so that a failure logs and the scheduler keeps running -- a Telegram
outage must not silently stop every future reminder.
"""

import logging
from datetime import datetime, timedelta
from typing import Callable, Optional

from apscheduler.schedulers.background import BackgroundScheduler

from src import bills, catalog, db, store
from src.config_loader import get_config
from src.notify import send_low_stock_alert, send_money_digest, send_pending_reminder

logger = logging.getLogger(__name__)

_UNITS = {"m": "minutes", "h": "hours", "d": "days", "w": "weeks"}

# Nothing is sent after this hour, however overdue: a digest at 23:40 wakes people up
# and then gets ignored. It waits for the next morning instead.
QUIET_FROM = 21
# A daily job that ran at 09:10 yesterday is still due at 09:00 today.
_SLACK = timedelta(hours=2)
TICK_MINUTES = 10


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


def period(schedule: str) -> Optional[timedelta]:
    """'1w' -> 7 days. Blank means the job is switched off (None)."""
    if not (schedule or "").strip():
        return None
    return timedelta(**_parse_schedule(schedule))


# ── When a job last ran ──────────────────────────────────────────────────────

def last_run(job: str) -> Optional[datetime]:
    row = db.connect().execute(
        "SELECT value FROM meta WHERE key = ?", (f"job:{job}",)
    ).fetchone()
    return datetime.fromisoformat(row["value"]) if row and row["value"] else None


def mark_run(job: str, when: Optional[datetime] = None) -> None:
    with db.transaction() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
            (f"job:{job}", (when or datetime.now()).isoformat(timespec="seconds")),
        )


def is_due(last: Optional[datetime], every: Optional[timedelta], now: datetime,
           hour: int) -> bool:
    """Should a job with this period run now, given when it last ran?

    Only between `hour` and QUIET_FROM. A job that has never run is due as soon as the
    window opens; after that, once its period has passed (less a little slack, so a
    daily job does not creep later by a tick every day).
    """
    if every is None:
        return False
    if not (hour <= now.hour < QUIET_FROM):
        return False
    if last is None:
        return True
    if every < timedelta(days=1):
        return now - last >= every
    return now - last >= every - _SLACK


# ── The jobs ─────────────────────────────────────────────────────────────────

def _pending_job() -> None:
    count = store.count_pending()
    if count > 0:
        send_pending_reminder(count, get_config().web_base_url)


def _money_job() -> None:
    """Bills falling due and invoices still unpaid, in one message."""
    config = get_config()
    due = bills.due_soon(within_days=config.bills_due_within_days)
    unpaid = store.list_unpaid()
    if send_money_digest(due, unpaid):
        logger.info("Money digest sent (%s bills due, %s unpaid invoices)",
                    len(due), len(unpaid))


def _stock_job() -> None:
    if send_low_stock_alert(catalog.low_stock()):
        logger.info("Low-stock alert sent")


def jobs() -> list[tuple[str, Optional[timedelta], Callable[[], None]]]:
    """(name, period, function) for every job, read from the settings in force now.

    Read on each tick rather than once at start, so a schedule changed in the panel
    takes effect without a restart. Later features add their daily chores here.
    """
    config = get_config()
    out = [
        ("pending_reminder", period(config.notify_schedule), _pending_job),
        ("money_digest", period(config.money_schedule), _money_job),
        ("low_stock", period(config.stock_schedule), _stock_job),
    ]
    out.extend(_extra_jobs())
    return out


def _extra_jobs() -> list:
    """Daily chores contributed by other modules (recurring invoices, dunning...)."""
    extra = []
    for module_name, job_name, attr in (
        ("src.recurring", "recurring_invoices", "run_due"),
        ("src.payment_reminders", "payment_reminders", "run_due"),
        ("src.gestor_pack", "tax_calendar", "quarter_reminder"),
    ):
        try:
            module = __import__(module_name, fromlist=[attr])
        except ImportError:
            continue
        extra.append((job_name, timedelta(days=1), getattr(module, attr)))
    return extra


def tick(now: Optional[datetime] = None) -> list[str]:
    """Run every job that is due. Returns the names of those that ran."""
    now = now or datetime.now()
    hour = int(getattr(get_config(), "alert_hour", 9))
    ran = []
    for name, every, job in jobs():
        if not is_due(last_run(name), every, now, hour):
            continue
        try:
            job()
            ran.append(name)
        except Exception:
            logger.error("Scheduled job %s failed", name, exc_info=True)
        # Marked even after a failure: a job that crashes must not be retried every
        # ten minutes all day, flooding the chat. It tries again next period.
        mark_run(name, now)
    return ran


def start_scheduler() -> BackgroundScheduler:
    scheduler = BackgroundScheduler(daemon=True)
    scheduler.add_job(tick, "interval", minutes=TICK_MINUTES, id="tick",
                      next_run_time=datetime.now() + timedelta(seconds=60))
    scheduler.start()
    config = get_config()
    logger.info(
        "Scheduler started — from %s:00: reviews every %s, money every %s, stock every %s",
        getattr(config, "alert_hour", 9), config.notify_schedule,
        config.money_schedule or "off", config.stock_schedule or "off",
    )
    return scheduler
