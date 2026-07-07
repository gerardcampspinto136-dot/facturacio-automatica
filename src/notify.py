"""Reviewer notifications: 'you have N invoices pending review' via Telegram and/or email."""

import logging
import os

from src.config_loader import get_config

logger = logging.getLogger(__name__)


def _telegram(chat_id, text: str) -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token or not chat_id:
        logger.warning("Telegram notification skipped (missing token or chat_id)")
        return
    import requests

    resp = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": text},
        timeout=15,
    )
    resp.raise_for_status()


def send_pending_reminder(count: int, url: str) -> None:
    """Notify the reviewer(s) that ``count`` invoices are waiting, linking to ``url``."""
    config = get_config()
    channels = config.notify_channels or []

    subject = f"{count} factura(s) pendiente(s) de revisión"
    text = (
        f"Tienes {count} factura(s) pendiente(s) de revisión.\n"
        f"Revísalas y envíalas aquí:\n{url}"
    )

    if "telegram" in channels:
        try:
            _telegram(config.notify_telegram_chat_id, text)
        except Exception:
            logger.error("Failed to send Telegram reminder", exc_info=True)

    if "email" in channels and config.notify_email:
        try:
            from src.email_sender import send_email

            send_email(to=config.notify_email, subject=subject, body=text)
        except Exception:
            logger.error("Failed to send email reminder", exc_info=True)
