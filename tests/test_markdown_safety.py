"""Client data never breaks a Telegram message.

In Telegram's Markdown an underscore opens italics, so an email like
juan_perez@gmail.com made Telegram refuse the summary outright: the person saw "Ha
fallado algo" instead of the invoice to confirm. User text is escaped, and if Telegram
still objects the message goes out as plain text.
"""

import asyncio
import re
from types import SimpleNamespace

import pytest
from telegram.error import BadRequest

from src import bot
from src.conversation import Session, md
from src.models import InvoiceData, InvoiceItem


def telegram_would_accept(text: str) -> bool:
    """A strict stand-in for Telegram's legacy Markdown parser.

    Unescaped _ * ` must come in pairs (they open and close entities), and an escaped
    one (\\_) is literal. That is the rule the real server applies.
    """
    unescaped = re.sub(r"\\[_*`\[]", "", text)
    return all(unescaped.count(ch) % 2 == 0 for ch in "_*`")


@pytest.mark.parametrize("value", [
    "juan_perez@gmail.com", "Taller_1 *Premium*", "precio 2*3", "`raro`_",
])
def test_escaped_user_text_is_accepted(value):
    assert telegram_would_accept(f"Email: {md(value)}")


def test_the_whole_summary_survives_awkward_client_data():
    invoice = InvoiceData(
        client_name="Taller_1 & *Hijos*", client_email="juan_perez@gmail.com",
        client_id="B12345678", client_address="C/ Mar_Azul 3",
        items=[InvoiceItem("Tornillos 8*20 mm_inox", 20, 0.25)],
        notes="entregar a `Pepe_2`",
    )
    replies = Session().start(invoice)
    assert all(telegram_would_accept(r.text) for r in replies if r.markdown)
    assert "juan\\_perez@gmail.com" in replies[-1].text


def test_a_rejected_message_is_resent_as_plain_text():
    sent = []

    class Target:
        async def reply_text(self, text, parse_mode=None, reply_markup=None):
            if parse_mode == "Markdown":
                raise BadRequest("Can't parse entities: can't find end of the entity")
            sent.append(text)

    asyncio.run(bot._reply(Target(), "Email: juan_perez@gmail.com"))
    assert sent == ["Email: juan_perez@gmail.com"]


def test_other_errors_are_not_swallowed():
    class Target:
        async def reply_text(self, text, parse_mode=None, reply_markup=None):
            raise BadRequest("Chat not found")

    with pytest.raises(BadRequest):
        asyncio.run(bot._reply(Target(), "hola"))
