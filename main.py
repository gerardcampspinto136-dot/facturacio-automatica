"""Entry point: the Telegram bot, the web panel and the reminder scheduler, together.

All three always run. The web panel used to start only in "manual" review mode, which
left an "auto" installation with no panel to manage accounts in and -- worse -- with no
money or stock reminders at all, since the scheduler was tied to the same switch.
"""

import logging

from dotenv import load_dotenv

load_dotenv()

from src.bot import run_bot  # noqa: E402 — must load .env first
from src.config_loader import get_config  # noqa: E402

logger = logging.getLogger(__name__)


def _start_web(cfg) -> None:
    import threading

    import uvicorn

    config = uvicorn.Config(
        "src.web.app:app", host=cfg.web_host, port=cfg.web_port, log_level="warning"
    )
    server = uvicorn.Server(config)
    # Not the main thread → don't let uvicorn install signal handlers.
    server.install_signal_handlers = lambda: None
    threading.Thread(target=server.run, daemon=True).start()
    logger.info("Web panel running at %s", cfg.web_base_url)


def _log_to_file() -> None:
    """Keep a log on disk as well as in the window.

    The window is closed, or the computer restarts, and whatever went wrong last night
    is gone. Five files of 5 MB each is weeks of history in a few MB.
    """
    from logging.handlers import RotatingFileHandler
    from pathlib import Path

    folder = Path("data/logs")
    folder.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(folder / "facturacion.log", maxBytes=5 * 1024 * 1024,
                                  backupCount=5, encoding="utf-8")
    handler.setFormatter(logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s"))
    logging.getLogger().addHandler(handler)


if __name__ == "__main__":
    logging.basicConfig(
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
    )
    _log_to_file()
    cfg = get_config()

    # Import anything left by the earlier JSON-file store. No-op after the first run.
    from src import store

    imported = store.migrate_from_json()
    if not imported["skipped"] and (imported["pending"] or imported["issued"]):
        logger.info(
            "Imported %s pending and %s issued invoices from the old JSON files",
            imported["pending"], imported["issued"],
        )

    _start_web(cfg)
    from src.scheduler import start_scheduler

    start_scheduler()

    run_bot()
