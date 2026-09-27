"""Talking to Telegram from outside the bot: the web panel and the scheduler.

The bot itself runs on python-telegram-bot, but the web process and the reminder jobs
also need to message people -- a reminder, an "approve this invoice?" prompt with its
buttons, the quarterly pack as a file. They do it through the plain Bot API over HTTP,
which needs nothing but the token.

The token is resolved in one place for everybody. It used to be read from .env by the
reminders and from the admin panel by the bot, so a client whose bot was connected in
the panel -- the documented way -- silently never received a single reminder.
"""

import json
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

API = "https://api.telegram.org/bot{token}/{method}"


def bot_token() -> Optional[str]:
    """Which Telegram bot this installation runs as.

    Each client company has its own bot, with its own name and token, entered in the
    admin panel. TELEGRAM_BOT_TOKEN in .env still wins when it is set, so an existing
    installation keeps working untouched.
    """
    from_env = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
    if from_env:
        return from_env

    try:
        from src import accounts

        company = accounts.active_company()
        if company:
            token = (company.get("telegram_bot_token") or "").strip()
            if token:
                return token
    except Exception:
        logger.debug("Could not read the company's bot token", exc_info=True)
    return None


_username_cache: dict[str, Optional[str]] = {}


def bot_username() -> Optional[str]:
    """The bot's @username, for building t.me links. None when it cannot be reached."""
    token = bot_token()
    if not token:
        return None
    if token in _username_cache:
        return _username_cache[token]
    try:
        result = _call("getMe", {})
        name = (result or {}).get("username")
    except Exception:
        logger.warning("Could not ask Telegram for the bot's username", exc_info=True)
        return None
    _username_cache[token] = name
    return name


def _call(method: str, payload: dict, files: Optional[dict] = None):
    import requests

    token = bot_token()
    if not token:
        raise RuntimeError("No hay ningún bot de Telegram configurado.")
    url = API.format(token=token, method=method)
    if files:
        response = requests.post(url, data=payload, files=files, timeout=60)
    else:
        response = requests.post(url, json=payload, timeout=20)
    response.raise_for_status()
    body = response.json()
    if not body.get("ok"):
        raise RuntimeError(f"Telegram rechazó {method}: {body.get('description')}")
    return body.get("result")


def keyboard(buttons) -> Optional[dict]:
    """[(label, callback_data), ...] -> an inline keyboard, one button per row.

    A row may also be a list of (label, data) pairs to put several buttons side by side.
    """
    if not buttons:
        return None
    rows = []
    for entry in buttons:
        if isinstance(entry, list):
            rows.append([{"text": label, "callback_data": data} for label, data in entry])
        else:
            label, data = entry
            rows.append([{"text": label, "callback_data": data}])
    return {"inline_keyboard": rows}


def send_message(chat_id, text: str, buttons=None, markdown: bool = False) -> bool:
    """Send one message. Returns False (and logs) instead of raising."""
    if not chat_id:
        return False
    payload = {"chat_id": chat_id, "text": text}
    if markdown:
        payload["parse_mode"] = "Markdown"
    markup = keyboard(buttons)
    if markup:
        payload["reply_markup"] = markup
    try:
        _call("sendMessage", payload)
        return True
    except Exception:
        logger.error("Telegram message to %s failed", chat_id, exc_info=True)
        return False


def send_document(chat_id, path: str, caption: str = "", buttons=None,
                  filename: Optional[str] = None) -> bool:
    """Send a file (a PDF, a ZIP). Returns False (and logs) instead of raising."""
    if not chat_id:
        return False
    payload = {"chat_id": str(chat_id)}
    if caption:
        payload["caption"] = caption[:1024]
    markup = keyboard(buttons)
    if markup:
        payload["reply_markup"] = json.dumps(markup)
    try:
        with open(path, "rb") as handle:
            _call("sendDocument", payload,
                  files={"document": (filename or os.path.basename(path), handle)})
        return True
    except Exception:
        logger.error("Telegram document to %s failed", chat_id, exc_info=True)
        return False
