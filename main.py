"""Entry point for the Invoice Bot.

In review ("manual") mode it also starts the web review app and the pending-invoice
reminder scheduler alongside the Telegram bot. In "auto" mode only the bot runs.
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
    logger.info("Web review app running at %s", cfg.web_base_url)


if __name__ == "__main__":
    logging.basicConfig(
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
    )
    cfg = get_config()

    if cfg.review_mode == "manual":
        _start_web(cfg)
        from src.scheduler import start_scheduler

        start_scheduler()
    else:
        logger.info("Review mode is 'auto' — invoices will be sent immediately.")

    run_bot()
