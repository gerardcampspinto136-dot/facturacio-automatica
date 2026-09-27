"""Chasing unpaid invoices: who gets reminded, when, how often -- and never by surprise.

These emails go to the company's customers, so the rules are pinned down exactly: not
before the grace days, not twice within the interval, never past the maximum, never
to an invoice that was paid, cancelled or taken out of chasing. In the default mode the
owner is asked first, once per round.
"""

from datetime import date, timedelta

import pytest

from src import accounts, finalize, payment_reminders, rectify, store
from src.config_loader import get_config, reload_config
from src.models import InvoiceData, InvoiceItem

TODAY = date.today()


def overdue_invoice(days_late: int = 10, email: str = "taller@puig.es", **kw):
    """Issued with 30-day terms, dated so that it is now `days_late` days overdue."""
    invoice = InvoiceData(
        client_name=kw.pop("client_name", "Talleres Puig"), client_email=email,
        client_id="B87654321", items=[InvoiceItem("Reparación", 1, 100.0)],
        date=TODAY - timedelta(days=30 + days_late), **kw)
    return finalize.issue(invoice).number


@pytest.fixture(autouse=True)
def owner(monkeypatch):
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "5001")
    company = accounts.create_company("Talleres Mario S.L.", tax_id="B12345678")
    accounts.update_company(company, address="Pol. Les Comes 14",
                            iban="ES21 0081 0298 1100 0123 4567")
    reload_config()
    return company


def prompts(outbox):
    return [p for m, p in outbox if m == "sendMessage" and "dun:send" in str(p)]


# ── Who is due a reminder ────────────────────────────────────────────────────

class TestWhoIsDue:
    def test_overdue_past_the_grace_days(self, offline):
        number = overdue_invoice(days_late=5)
        assert [r["invoice"].invoice_number for r in payment_reminders.due()] == [number]

    def test_not_within_the_grace_days(self, offline):
        overdue_invoice(days_late=2)
        assert payment_reminders.due() == []

    def test_not_without_an_email(self, offline):
        overdue_invoice(email="")
        assert payment_reminders.due() == []

    def test_not_once_paid(self, offline):
        store.mark_paid(overdue_invoice())
        assert payment_reminders.due() == []

    def test_not_once_cancelled(self, offline):
        rectify.rectify(overdue_invoice())
        assert payment_reminders.due() == []

    def test_not_when_told_to_stop(self, offline):
        payment_reminders.pause(overdue_invoice())
        assert payment_reminders.due() == []

    def test_not_again_within_the_interval_but_again_after_it(self, offline):
        number = overdue_invoice()
        payment_reminders.send(number)
        assert payment_reminders.due() == []
        assert payment_reminders.due(TODAY + timedelta(days=7))

    def test_never_past_the_maximum(self, offline):
        number = overdue_invoice()
        for week in range(3):
            payment_reminders.send(number, TODAY + timedelta(days=7 * week))
        assert payment_reminders.due(TODAY + timedelta(days=60)) == []

    def test_most_overdue_first(self, offline):
        recent = overdue_invoice(days_late=5)
        oldest = overdue_invoice(days_late=40)
        assert [r["invoice"].invoice_number for r in payment_reminders.due()] == \
            [oldest, recent]


# ── The email ────────────────────────────────────────────────────────────────

class TestTheEmail:
    def test_it_goes_to_the_client_with_the_invoice_attached(self, offline):
        number = overdue_invoice()
        sent, message = payment_reminders.send(number)
        assert sent and "Recordatorio 1" in message
        to, subject, attachment = offline[-1]
        assert to == "taller@puig.es" and number in subject
        assert attachment.endswith(f"Factura_{number}.pdf")
        assert store.get_issued(number)["reminder_count"] == 1

    def test_it_says_what_is_owed_and_where_to_pay(self, offline):
        number = overdue_invoice()
        _, body = payment_reminders.compose(store.get_issued(number))
        assert "121,00 €" in body                   # with its VAT
        assert "ES21 0081 0298 1100 0123 4567" in body
        assert number in body

    def test_a_bad_placeholder_in_the_template_falls_back(self, offline):
        number = overdue_invoice()
        get_config().collections_body = "Hola {nombre_que_no_existe}"
        _, body = payment_reminders.compose(store.get_issued(number))
        assert number in body

    def test_a_paid_invoice_is_not_chased(self, offline):
        number = overdue_invoice()
        store.mark_paid(number)
        sent, message = payment_reminders.send(number)
        assert not sent and "ya está cobrada" in message


# ── The daily round ──────────────────────────────────────────────────────────

class TestTheRound:
    def test_ask_mode_asks_the_owner_once_with_buttons(self, offline, telegram_outbox):
        number = overdue_invoice()
        assert payment_reminders.run_due() == [number]
        asked = prompts(telegram_outbox)
        assert len(asked) == 1 and asked[0]["chat_id"] == 5001
        # Asking is all it does: nothing has gone to the client.
        assert not any("Recordatorio" in subject for _, subject, _ in offline)

        assert payment_reminders.run_due() == []  # not again tomorrow morning
        assert payment_reminders.run_due(TODAY + timedelta(days=7)) == [number]

    def test_auto_mode_sends_and_says_so(self, offline, telegram_outbox):
        number = overdue_invoice()
        get_config().collections_mode = "auto"
        assert payment_reminders.run_due() == [number]
        assert store.get_issued(number)["reminder_count"] == 1
        told = [p["text"] for m, p in telegram_outbox if m == "sendMessage"]
        assert any("Recordatorio 1 enviado" in t for t in told)

    def test_off_means_off(self, offline, telegram_outbox):
        overdue_invoice()
        get_config().collections_mode = "off"
        assert payment_reminders.run_due() == []
        assert prompts(telegram_outbox) == []


# ── From the chat ────────────────────────────────────────────────────────────

class TestChat:
    def test_the_send_button(self, offline):
        from test_telegram_access import press

        number = overdue_invoice()
        chat = press(5001, f"dun:send:{number}")
        assert "Recordatorio 1 enviado" in chat.text

    def test_the_paid_button(self, offline):
        from test_telegram_access import press

        number = overdue_invoice()
        press(5001, f"dun:paid:{number}")
        assert store.get_issued(number)["paid_at"]

    def test_the_stop_button(self, offline):
        from test_telegram_access import press

        number = overdue_invoice()
        press(5001, f"dun:stop:{number}")
        assert store.get_issued(number)["reminders_paused"]

    def test_cobrada_alone_lists_what_is_unpaid_with_buttons(self, offline):
        from src import bot
        from test_telegram_access import say

        number = overdue_invoice()
        chat = say(bot.cmd_cobrada, 5001)
        assert number in chat.text
        assert f"dun:paid:{number}" in chat.buttons()

    def test_cobrada_with_a_number(self, offline):
        from src import bot
        from test_telegram_access import say

        number = overdue_invoice()
        say(bot.cmd_cobrada, 5001, number)
        assert store.get_issued(number)["paid_at"]

    def test_staff_without_the_permission_cannot_chase(self, offline, owner):
        from test_telegram_access import linked_employee, press

        number = overdue_invoice()
        linked_employee(owner, ["receivables.view"])
        assert "No tienes permiso" in press(7002, f"dun:send:{number}").text
        assert store.get_issued(number)["reminder_count"] == 0


# ── From the web ─────────────────────────────────────────────────────────────

class TestWeb:
    @pytest.fixture
    def client(self, monkeypatch, owner):
        import base64
        import json

        import itsdangerous
        from fastapi.testclient import TestClient

        monkeypatch.delenv("WEB_DEV_NO_AUTH", raising=False)
        monkeypatch.setenv("SESSION_SECRET", "test-secret")
        from src.web import app as web_app

        accounts.create_user("jefe@talleres.es", accounts.ADMIN, company_id=owner)
        client = TestClient(web_app.app, follow_redirects=False)
        signer = itsdangerous.TimestampSigner("test-secret")
        data = base64.b64encode(json.dumps({"user": "jefe@talleres.es"}).encode())
        client.cookies.set("session", signer.sign(data).decode())
        return client

    def test_remind_pause_and_resume(self, client, offline):
        number = overdue_invoice()
        assert "Recordar" in client.get("/receivables").text

        client.post(f"/receivables/{number}/remind")
        assert store.get_issued(number)["reminder_count"] == 1
        assert "recordada 1 vez" in client.get("/receivables").text

        client.post(f"/receivables/{number}/pause")
        assert store.get_issued(number)["reminders_paused"]
        client.post(f"/receivables/{number}/resume")
        assert not store.get_issued(number)["reminders_paused"]

    def test_the_mode_is_set_in_the_company_settings(self, client, owner):
        accounts.create_user("gerard@vendor.es", accounts.SUPERADMIN)
        import base64
        import json

        import itsdangerous

        signer = itsdangerous.TimestampSigner("test-secret")
        data = base64.b64encode(json.dumps({"user": "gerard@vendor.es"}).encode())
        client.cookies.set("session", signer.sign(data).decode())
        client.post(f"/admin/{owner}/settings",
                    data={"name": "Talleres Mario S.L.", "collections_mode": "off"})
        assert get_config().collections_mode == "off"
