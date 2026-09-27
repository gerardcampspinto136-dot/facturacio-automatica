"""Who may use the Telegram bot, and as whom.

Two ways in:

1. A Telegram account linked to a panel account (see accounts.link_telegram). It gets
   exactly that account's permissions, re-read on every message.
2. The company's own notification chat -- TELEGRAM_CHAT_ID in .env, or "Chat de avisos"
   in the admin panel. Whoever set the installation up put it there, so it is the
   owner's chat and gets owner access. This is also what keeps an installation that
   predates account linking working exactly as before.

Everyone else is refused. Nothing here knows about python-telegram-bot, so the rules are
testable with plain integers.
"""

import logging
from typing import Optional

from src import accounts
from src.config_loader import get_config

logger = logging.getLogger(__name__)

# How the owner's chat appears to the permission rules: a company owner.
OWNER_CHAT_USER = {
    "id": None,
    "email": "",
    "name": "Responsable",
    "role": accounts.ADMIN,
    "permissions": [],
    "active": 1,
    "company_status": accounts.ACTIVE,
    "via": "owner_chat",
}


def owner_chat_ids() -> set[int]:
    chat = get_config().notify_telegram_chat_id
    return {int(chat)} if chat else set()


def resolve(telegram_user_id: Optional[int], chat_id: Optional[int]) -> Optional[dict]:
    """The account a Telegram message acts as, or None for a stranger."""
    user = accounts.find_by_telegram(telegram_user_id)
    if user is not None:
        return user

    owners = owner_chat_ids()
    if owners and (chat_id in owners or telegram_user_id in owners):
        # A client the vendor has suspended loses the bot too, as it loses the panel.
        companies = accounts.list_companies()
        if companies and not any(c["status"] == accounts.ACTIVE for c in companies):
            return None
        company = accounts.active_company()
        owner = dict(OWNER_CHAT_USER)
        if company:
            owner["company_id"] = company["id"]
            owner["company_name"] = company["name"]
        return owner
    return None


def notify_chats(permission: str) -> list[int]:
    """Every chat that should hear about something needing `permission`.

    The owner's chat plus each linked account holding the permission, without
    duplicates -- the owner linking their own account must not get everything twice.
    """
    chats: list[int] = []
    for chat in owner_chat_ids():
        chats.append(chat)
    for user in accounts.telegram_recipients(permission):
        if user["telegram_id"] not in chats:
            chats.append(user["telegram_id"])
    return chats


# Strangers are reported to the owner once each, so an employee who writes to the bot
# before being given access shows up with their name -- and a curious outsider is
# noticed -- without the owner being flooded if someone keeps trying.
_reported: set[int] = set()


def report_stranger(telegram_user_id: int, name: str, username: Optional[str]) -> None:
    if not telegram_user_id or telegram_user_id in _reported:
        return
    _reported.add(telegram_user_id)
    from src import telegram_api

    who = name or "Alguien"
    if username:
        who += f" (@{username})"
    text = (
        f"🔒 {who} ha escrito al bot sin tener acceso (ID de Telegram "
        f"{telegram_user_id}). No ha visto ni hecho nada.\n\n"
        "Si es de tu equipo, dale una cuenta en el panel (Equipo) y que la conecte "
        "desde «Mi cuenta → Conectar Telegram»."
    )
    for chat in owner_chat_ids():
        telegram_api.send_message(chat, text)
    logger.warning("Refused Telegram user %s (%s)", telegram_user_id, who)
