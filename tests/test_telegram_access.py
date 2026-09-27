"""The Telegram bot is private, and applies each person's permissions.

It used to answer anybody who found it. These tests drive the real handlers with
stand-in Telegram objects: a stranger learns nothing and can do nothing, a linked
employee can do exactly what their account allows, and an invoice dictated by someone
who may not send it goes to a responsable -- who approves it with one tap.
"""

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from src import accounts, bot, contacts, conversation, db, store, telegram_access
from src.models import InvoiceData, InvoiceItem

OWNER_CHAT = 5001
EMPLOYEE_TG = 7002
STRANGER_TG = 9003


# ── Stand-ins for python-telegram-bot ────────────────────────────────────────

class Sent:
    """What reply_text returns: a message that can later be edited or deleted."""

    def __init__(self, chat, text):
        self.chat, self.text = chat, text

    async def edit_text(self, text, **_kw):
        self.chat.replies.append(text)
        return self

    async def delete(self):
        return None


class Chat:
    """Everything the bot said in one conversation."""

    def __init__(self):
        self.replies: list[str] = []
        self.markups: list = []
        self.documents: list[tuple] = []

    @property
    def text(self) -> str:
        return "\n".join(self.replies)

    def buttons(self) -> list[str]:
        out = []
        for markup in self.markups:
            if markup is None:
                continue
            for row in markup.inline_keyboard:
                out.extend(button.callback_data for button in row)
        return out


class Message:
    def __init__(self, chat: Chat, text: str = ""):
        self._chat = chat
        self.text = text
        self.caption = None
        self.voice = self.audio = self.photo = self.document = None

    async def reply_text(self, text, **kw):
        self._chat.replies.append(text)
        self._chat.markups.append(kw.get("reply_markup"))
        return Sent(self._chat, text)

    async def reply_document(self, document=None, filename=None, caption=None, **kw):
        self._chat.documents.append((filename, caption))
        self._chat.markups.append(kw.get("reply_markup"))
        if caption:
            self._chat.replies.append(caption)
        return Sent(self._chat, caption)


class Query:
    def __init__(self, chat: Chat, data: str):
        self.data = data
        self.message = Message(chat)

    async def answer(self):
        return None

    async def edit_message_reply_markup(self, reply_markup=None):
        return None


def update_for(user_id: int, chat: Chat, text: str = "", data: str | None = None):
    who = SimpleNamespace(id=user_id, username=f"user{user_id}", full_name=f"User {user_id}")
    return SimpleNamespace(
        message=None if data else Message(chat, text),
        callback_query=Query(chat, data) if data else None,
        effective_user=who,
        effective_chat=SimpleNamespace(id=user_id),
    )


def say(handler, user_id: int, *args, text: str = "", chat: Chat | None = None) -> Chat:
    chat = chat or Chat()
    asyncio.run(handler(update_for(user_id, chat, text), SimpleNamespace(args=list(args))))
    return chat


def press(user_id: int, data: str, chat: Chat | None = None) -> Chat:
    chat = chat or Chat()
    asyncio.run(bot.on_button(update_for(user_id, chat, data=data), SimpleNamespace(args=[])))
    return chat


# ── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def owner_chat(monkeypatch):
    monkeypatch.setenv("TELEGRAM_CHAT_ID", str(OWNER_CHAT))
    conversation._sessions.clear()


@pytest.fixture
def company():
    return accounts.create_company("Talleres Mario S.L.", tax_id="B12345678")


def linked_employee(company, permissions, telegram_id=EMPLOYEE_TG,
                    email="pepe@talleres.es", name="Pepe"):
    user_id = accounts.create_user(email, accounts.EMPLOYEE, company_id=company,
                                   name=name, permissions=list(permissions))
    code = accounts.create_telegram_code(user_id)
    user, refusal = accounts.link_telegram(code, telegram_id, "pepe")
    assert refusal is None
    return user_id


@pytest.fixture
def dictation(monkeypatch):
    """The model's reading of a dictation, without calling the model."""
    def parse(_text):
        return InvoiceData(
            client_name="Talleres Puig", client_email="taller@puig.es",
            client_id="B87654321", items=[InvoiceItem("Reparación", 1, 300.0)],
        )
    monkeypatch.setattr(bot, "parse_invoice_from_transcript", parse)


# ── Strangers ────────────────────────────────────────────────────────────────

class TestStrangers:
    @pytest.mark.parametrize("handler,args", [
        (bot.cmd_pagos, ()), (bot.cmd_clientes, ()), (bot.cmd_stock, ()),
        (bot.cmd_producto, ()), (bot.cmd_pendientes, ()), (bot.cmd_help, ()),
        (bot.cmd_anular, ("2026-0001",)), (bot.cmd_gasto, ("Ferreteria", "10")),
        (bot.cmd_entrada, ("Tornillos", "5")), (bot.cmd_start, ()),
    ])
    def test_every_command_refuses_a_stranger(self, handler, args):
        chat = say(handler, STRANGER_TG, *args)
        assert "privado" in chat.text
        assert str(STRANGER_TG) in chat.text  # so a real employee can quote it

    def test_a_stranger_cannot_read_the_clients(self):
        contacts.create(contacts.CLIENT, "Cliente Secreto", tax_id="B11111111",
                        email="secreto@cliente.es")
        chat = say(bot.cmd_clientes, STRANGER_TG)
        assert "Cliente Secreto" not in chat.text
        assert "B11111111" not in chat.text

    def test_a_stranger_cannot_start_an_invoice(self, dictation):
        chat = say(bot.handle_text, STRANGER_TG, text="Factura para Talleres Puig 300")
        assert "privado" in chat.text
        assert not conversation.session_for(STRANGER_TG).active

    def test_a_stranger_cannot_cancel_an_invoice(self, offline):
        from src.finalize import finalize_invoice

        invoice = InvoiceData(client_name="X", client_email="",
                              items=[InvoiceItem("Servicio", 1, 100.0)])
        finalize_invoice(invoice)
        say(bot.cmd_anular, STRANGER_TG, invoice.invoice_number)
        assert store.get_issued(invoice.invoice_number)["rectified_by"] is None

    def test_a_stranger_pressing_an_old_button_is_refused(self, company):
        token = store.add_pending(InvoiceData(
            client_name="X", client_email="x@x.es",
            items=[InvoiceItem("Servicio", 1, 100.0)]), "d.pdf")
        chat = press(STRANGER_TG, f"pend:ok:{token}")
        assert "privado" in chat.text
        assert store.get_pending(token) is not None

    def test_the_owner_is_told_once_who_tried(self, telegram_outbox):
        say(bot.cmd_pagos, STRANGER_TG)
        say(bot.cmd_stock, STRANGER_TG)
        alerts = [p for m, p in telegram_outbox
                  if m == "sendMessage" and p["chat_id"] == OWNER_CHAT]
        assert len(alerts) == 1
        assert str(STRANGER_TG) in alerts[0]["text"]

    def test_chatid_still_works_for_setting_the_owner_up(self):
        assert str(STRANGER_TG) in say(bot.cmd_chatid, STRANGER_TG).text


# ── The owner's chat ─────────────────────────────────────────────────────────

class TestOwnerChat:
    def test_the_configured_chat_has_full_access(self):
        contacts.create(contacts.CLIENT, "Cliente Conocido")
        assert "Cliente Conocido" in say(bot.cmd_clientes, OWNER_CHAT).text

    def test_a_suspended_client_loses_the_bot(self, company):
        accounts.set_company_status(company, accounts.SUSPENDED)
        assert "privado" in say(bot.cmd_pagos, OWNER_CHAT).text

    def test_with_no_owner_chat_configured_nobody_gets_in(self, monkeypatch):
        monkeypatch.delenv("TELEGRAM_CHAT_ID")
        assert "privado" in say(bot.cmd_pagos, OWNER_CHAT).text


# ── Linking an account ───────────────────────────────────────────────────────

class TestLinking:
    def test_start_with_a_code_links_the_account(self, company):
        user_id = accounts.create_user("pepe@talleres.es", accounts.EMPLOYEE,
                                       company_id=company, name="Pepe")
        code = accounts.create_telegram_code(user_id)

        chat = say(bot.cmd_start, EMPLOYEE_TG, code)

        assert "conectado" in chat.text
        assert accounts.find_by_telegram(EMPLOYEE_TG)["id"] == user_id

    def test_a_code_works_once(self, company):
        user_id = accounts.create_user("pepe@talleres.es", accounts.EMPLOYEE,
                                       company_id=company)
        code = accounts.create_telegram_code(user_id)
        say(bot.cmd_start, EMPLOYEE_TG, code)

        chat = say(bot.cmd_start, STRANGER_TG, code)
        assert "no es válido" in chat.text
        assert accounts.find_by_telegram(STRANGER_TG) is None

    def test_an_expired_code_is_refused(self, company):
        user_id = accounts.create_user("pepe@talleres.es", accounts.EMPLOYEE,
                                       company_id=company)
        code = accounts.create_telegram_code(user_id)
        with db.transaction() as conn:
            conn.execute("UPDATE users SET telegram_code_expires = ? WHERE id = ?",
                         ((datetime.now() - timedelta(minutes=1)).isoformat(), user_id))
        assert "caducado" in say(bot.cmd_start, EMPLOYEE_TG, code).text

    def test_a_made_up_code_is_refused(self):
        assert "no es válido" in say(bot.cmd_start, STRANGER_TG, "ABCDEFGHJK").text

    def test_a_deactivated_account_loses_the_bot_immediately(self, company):
        user_id = linked_employee(company, ["stock.view"])
        assert "privado" not in say(bot.cmd_stock, EMPLOYEE_TG).text

        accounts.update_user(user_id, active=False)
        assert "privado" in say(bot.cmd_stock, EMPLOYEE_TG).text

    def test_one_telegram_account_belongs_to_one_panel_account(self, company):
        first = linked_employee(company, ["stock.view"])
        second = accounts.create_user("otro@talleres.es", accounts.EMPLOYEE,
                                      company_id=company)
        accounts.link_telegram(accounts.create_telegram_code(second), EMPLOYEE_TG)
        assert accounts.get_user(first)["telegram_id"] is None
        assert accounts.find_by_telegram(EMPLOYEE_TG)["id"] == second


# ── Permissions in the bot ───────────────────────────────────────────────────

class TestPermissions:
    def test_an_employee_can_do_what_they_were_granted(self, company):
        linked_employee(company, ["stock.view"])
        chat = say(bot.cmd_stock, EMPLOYEE_TG)
        assert "No tienes permiso" not in chat.text

    def test_and_nothing_else(self, company):
        linked_employee(company, ["stock.view"])
        contacts.create(contacts.CLIENT, "Cliente Secreto")
        for handler, args in ((bot.cmd_clientes, ()), (bot.cmd_pagos, ()),
                              (bot.cmd_producto, ("Tornillos", "1", "5")),
                              (bot.cmd_anular, ("2026-0001",))):
            chat = say(handler, EMPLOYEE_TG, *args)
            assert "No tienes permiso" in chat.text, handler.__name__
        assert "Cliente Secreto" not in say(bot.cmd_clientes, EMPLOYEE_TG).text

    def test_a_refusal_names_what_is_missing(self, company):
        linked_employee(company, ["stock.view"])
        assert "Ver clientes y proveedores" in say(bot.cmd_clientes, EMPLOYEE_TG).text

    def test_pagos_shows_only_the_side_they_may_see(self, company):
        from src import bills

        linked_employee(company, ["bills.view"])
        bills.create("Ferretería Puig", 100.0, due_days=2)
        chat = say(bot.cmd_pagos, EMPLOYEE_TG)
        assert "PAGOS A PROVEEDORES" in chat.text
        assert "SIN COBRAR" not in chat.text


# ── Staff invoices wait for approval ─────────────────────────────────────────

class TestApproval:
    def test_someone_who_cannot_send_is_offered_review_instead(self, company, dictation):
        linked_employee(company, ["invoices.create", "invoices.view"])
        chat = say(bot.handle_text, EMPLOYEE_TG, text="Factura para Talleres Puig")
        labels = [b.text for m in chat.markups if m
                  for row in m.inline_keyboard for b in row]
        assert any("Mandar a revisión" in label for label in labels)
        assert not any(label.endswith("Enviar") for label in labels)
        assert "hold" not in chat.buttons()

    def test_confirming_queues_it_and_sends_nothing(self, company, dictation, offline):
        linked_employee(company, ["invoices.create", "invoices.view"])
        chat = say(bot.handle_text, EMPLOYEE_TG, text="Factura para Talleres Puig")
        press(EMPLOYEE_TG, "approve", chat)

        assert "Mandada a revisión" in chat.text
        assert store.count_pending() == 1
        assert store.list_issued() == []
        assert offline == []  # no email went anywhere

    def test_the_approver_gets_the_draft_with_buttons(self, company, dictation, offline,
                                                      telegram_outbox):
        linked_employee(company, ["invoices.create", "invoices.view"])
        chat = say(bot.handle_text, EMPLOYEE_TG, text="Factura para Talleres Puig")
        press(EMPLOYEE_TG, "approve", chat)

        docs = [p for m, p in telegram_outbox if m == "sendDocument"]
        assert [d["chat_id"] for d in docs] == [str(OWNER_CHAT)]
        assert "pend:ok:" in docs[0]["reply_markup"]
        assert "Pepe" in docs[0]["caption"]

    def test_one_tap_issues_it_and_tells_whoever_prepared_it(
            self, company, dictation, offline, telegram_outbox):
        linked_employee(company, ["invoices.create", "invoices.view"])
        chat = say(bot.handle_text, EMPLOYEE_TG, text="Factura para Talleres Puig")
        press(EMPLOYEE_TG, "approve", chat)
        token = store.list_pending()[0]["token"]

        owner = press(OWNER_CHAT, f"pend:ok:{token}")

        issued = store.list_issued()
        assert len(issued) == 1 and store.count_pending() == 0
        assert owner.documents and "aprobada" in owner.text
        assert offline and offline[0][0] == "taller@puig.es"
        told = [p for m, p in telegram_outbox
                if m == "sendMessage" and p["chat_id"] == EMPLOYEE_TG]
        assert told and "aprobado" in told[-1]["text"]

    def test_discarding_tells_them_too(self, company, dictation, offline, telegram_outbox):
        linked_employee(company, ["invoices.create", "invoices.view"])
        chat = say(bot.handle_text, EMPLOYEE_TG, text="Factura para Talleres Puig")
        press(EMPLOYEE_TG, "approve", chat)
        token = store.list_pending()[0]["token"]

        press(OWNER_CHAT, f"pend:no:{token}")

        assert store.count_pending() == 0 and store.list_issued() == []
        told = [p for m, p in telegram_outbox
                if m == "sendMessage" and p["chat_id"] == EMPLOYEE_TG]
        assert "descartado" in told[-1]["text"]

    def test_an_employee_cannot_approve_their_own(self, company, dictation, offline):
        linked_employee(company, ["invoices.create", "invoices.view"])
        chat = say(bot.handle_text, EMPLOYEE_TG, text="Factura para Talleres Puig")
        press(EMPLOYEE_TG, "approve", chat)
        token = store.list_pending()[0]["token"]

        refused = press(EMPLOYEE_TG, f"pend:ok:{token}")
        assert "No tienes permiso" in refused.text
        assert store.count_pending() == 1

    def test_the_second_tap_on_the_same_draft_does_nothing(self, company, offline):
        token = store.add_pending(InvoiceData(
            client_name="X", client_email="x@x.es",
            items=[InvoiceItem("Servicio", 1, 100.0)]), "d.pdf")
        press(OWNER_CHAT, f"pend:ok:{token}")
        again = press(OWNER_CHAT, f"pend:ok:{token}")
        assert "ya no está pendiente" in again.text
        assert len(store.list_issued()) == 1

    def test_the_owner_still_sends_straight_away(self, dictation, offline):
        chat = say(bot.handle_text, OWNER_CHAT, text="Factura para Talleres Puig")
        assert "hold" in chat.buttons()
        press(OWNER_CHAT, "approve", chat)
        assert len(store.list_issued()) == 1

    def test_keeping_it_for_later_queues_without_bothering_anyone(
            self, dictation, offline, telegram_outbox):
        chat = say(bot.handle_text, OWNER_CHAT, text="Factura para Talleres Puig")
        press(OWNER_CHAT, "hold", chat)
        assert store.count_pending() == 1
        assert not [p for m, p in telegram_outbox if m == "sendDocument"]
        assert "Guardada sin enviar" in chat.text

    def test_pendientes_lists_them_with_their_buttons(self, company, offline):
        store.add_pending(InvoiceData(
            client_name="Cliente Uno", client_email="x@x.es",
            items=[InvoiceItem("Servicio", 1, 100.0)]), "d.pdf")
        chat = say(bot.cmd_pendientes, OWNER_CHAT)
        assert "Cliente Uno" in chat.text
        assert any(b.startswith("pend:ok:") for b in chat.buttons())

    def test_a_viewer_sees_pendientes_without_approve_buttons(self, company):
        linked_employee(company, ["invoices.view"])
        store.add_pending(InvoiceData(
            client_name="Cliente Uno", client_email="x@x.es",
            items=[InvoiceItem("Servicio", 1, 100.0)]), "d.pdf")
        chat = say(bot.cmd_pendientes, EMPLOYEE_TG)
        assert "Cliente Uno" in chat.text
        assert not any(b.startswith("pend:ok:") for b in chat.buttons())


# ── Who hears about things ───────────────────────────────────────────────────

class TestRecipients:
    def test_owner_chat_plus_linked_approvers_without_duplicates(self, company):
        linked_employee(company, ["invoices.approve"], telegram_id=OWNER_CHAT,
                        email="jefe@talleres.es")
        linked_employee(company, ["invoices.approve"], telegram_id=8001,
                        email="a@talleres.es")
        linked_employee(company, ["invoices.view"], telegram_id=8002,
                        email="b@talleres.es")
        assert telegram_access.notify_chats("invoices.approve") == [OWNER_CHAT, 8001]


# ── The panel side of linking ────────────────────────────────────────────────

class TestPanel:
    @pytest.fixture
    def client(self, monkeypatch):
        from fastapi.testclient import TestClient

        monkeypatch.delenv("WEB_DEV_NO_AUTH", raising=False)
        monkeypatch.setenv("SESSION_SECRET", "test-secret")
        from src.web import app as web_app

        return TestClient(web_app.app, follow_redirects=False)

    def sign_in(self, client, email):
        import base64
        import json

        import itsdangerous

        signer = itsdangerous.TimestampSigner("test-secret")
        data = base64.b64encode(json.dumps({"user": email}).encode())
        client.cookies.set("session", signer.sign(data).decode())

    def test_anyone_can_connect_their_own_telegram(self, client, company, monkeypatch):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
        accounts.create_user("pepe@talleres.es", accounts.EMPLOYEE, company_id=company)
        self.sign_in(client, "pepe@talleres.es")

        page = client.post("/me/telegram").text

        code = accounts.find_by_email("pepe@talleres.es")["telegram_code"]
        assert code and f"t.me/facturas_test_bot?start={code}" in page
        assert "<svg" in page  # the QR to scan from the phone

    def test_disconnecting(self, client, company):
        linked_employee(company, ["stock.view"])
        self.sign_in(client, "pepe@talleres.es")
        client.post("/me/telegram/unlink")
        assert accounts.find_by_telegram(EMPLOYEE_TG) is None

    def test_an_owner_can_make_a_link_for_their_staff(self, client, company):
        accounts.create_user("jefe@talleres.es", accounts.ADMIN, company_id=company)
        staff = accounts.create_user("pepe@talleres.es", accounts.EMPLOYEE,
                                     company_id=company)
        self.sign_in(client, "jefe@talleres.es")

        page = client.post(f"/team/{staff}/telegram").text
        code = accounts.get_user(staff)["telegram_code"]
        assert code and code in page

    def test_an_employee_cannot_make_links_for_others(self, client, company):
        accounts.create_user("pepe@talleres.es", accounts.EMPLOYEE, company_id=company)
        boss = accounts.create_user("jefe@talleres.es", accounts.ADMIN, company_id=company)
        self.sign_in(client, "pepe@talleres.es")
        client.post(f"/team/{boss}/telegram")
        assert accounts.get_user(boss)["telegram_code"] is None
