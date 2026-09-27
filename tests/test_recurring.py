"""Invoices that repeat on their own: made from one issued, prepared or sent each period.

The dangerous failure here is a duplicate: the same monthly invoice sent to a client
twice. So the tests pin down that a template moves on as soon as its invoice exists,
even when the PDF or the email fails afterwards.
"""

from datetime import date, timedelta

import pytest

from src import accounts, finalize, recurring, store
from src.config_loader import reload_config
from src.models import InvoiceData, InvoiceItem

TODAY = date.today()


def issued(day: date = TODAY, **kw):
    invoice = InvoiceData(
        client_name="Comunidad Rambla 12", client_email="admin@rambla12.es",
        client_id="H12345678", items=[InvoiceItem("Mantenimiento ascensor", 1, 150.0)],
        date=day, **kw)
    return finalize.issue(invoice)


@pytest.fixture(autouse=True)
def owner(monkeypatch):
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "5001")
    company = accounts.create_company("Ascensores Vila S.L.", tax_id="B12345678")
    accounts.update_company(company, address="Carrer Major 1")
    reload_config()
    return company


# ── The calendar ─────────────────────────────────────────────────────────────

class TestCalendar:
    def test_the_31st_becomes_the_last_day_of_a_short_month(self):
        assert recurring.add_months(date(2026, 1, 31), 1, 31) == date(2026, 2, 28)
        assert recurring.add_months(date(2028, 1, 31), 1, 31) == date(2028, 2, 29)

    def test_and_comes_back_to_the_31st_afterwards(self):
        feb = recurring.add_months(date(2026, 1, 31), 1, 31)
        assert recurring.add_months(feb, 1, 31) == date(2026, 3, 31)

    def test_quarterly_and_yearly(self):
        assert recurring.next_after(date(2026, 11, 15), recurring.QUARTERLY, 15) == \
            date(2027, 2, 15)
        assert recurring.next_after(date(2026, 3, 1), recurring.YEARLY, 1) == \
            date(2027, 3, 1)

    def test_period_labels(self):
        assert recurring.period_label(date(2026, 10, 1), recurring.MONTHLY) == "octubre 2026"
        assert recurring.period_label(date(2026, 11, 1), recurring.QUARTERLY) == "4T 2026"


# ── Templates ────────────────────────────────────────────────────────────────

class TestTemplates:
    def test_the_first_repetition_is_one_month_on(self, offline):
        result = issued(date(2026, 9, 1))
        template = recurring.get(recurring.create_from_invoice(result.invoice))
        assert template["next_date"] == "2026-10-01"
        assert template["source_number"] == result.number

    def test_it_repeats_the_same_lines_rates_and_client(self, offline):
        result = issued(irpf_rate=15)
        template = recurring.get(recurring.create_from_invoice(result.invoice))
        again = recurring.invoice_for(template, TODAY)
        assert [(i.description, i.total) for i in again.items] == \
            [("Mantenimiento ascensor", 150.0)]
        assert (again.irpf_rate, again.client_email) == (15, "admin@rambla12.es")


# ── The run ──────────────────────────────────────────────────────────────────

class TestRun:
    def make(self, auto=False, days_ago=31):
        result = issued(TODAY - timedelta(days=days_ago))
        template_id = recurring.create_from_invoice(result.invoice, auto_send=auto)
        return template_id

    def test_nothing_before_its_date(self, offline):
        self.make(days_ago=5)
        assert recurring.run_due() == []

    def test_by_default_it_is_prepared_for_approval(self, offline, telegram_outbox):
        template_id = self.make()
        before = len(store.list_issued())

        done = recurring.run_due()

        assert len(done) == 1 and store.count_pending() == 1
        assert len(store.list_issued()) == before  # nothing sent to the client
        pending = store.list_pending()[0]["invoice"]
        assert "Periodo:" in pending.notes
        docs = [p for m, p in telegram_outbox if m == "sendDocument"]
        assert docs and "recurrente" in docs[0]["caption"]
        assert recurring.get(template_id)["next_date"] > TODAY.isoformat()

    def test_auto_send_issues_it_and_says_so(self, offline, telegram_outbox):
        template_id = self.make(auto=True)
        done = recurring.run_due()
        assert len(done) == 1
        number = done[0]
        assert store.get_issued(number) is not None
        assert recurring.get(template_id)["last_number"] == number
        told = [p["text"] for m, p in telegram_outbox if m == "sendMessage"]
        assert any("Factura recurrente emitida" in t for t in told)
        assert offline[-1][0] == "admin@rambla12.es"

    def test_it_does_not_run_twice_the_same_day(self, offline):
        self.make(auto=True)
        recurring.run_due()
        assert recurring.run_due() == []

    def test_a_long_absence_catches_up_one_period_at_a_time(self, offline):
        self.make(auto=True, days_ago=70)
        assert len(recurring.run_due()) == 1
        assert len(recurring.run_due()) == 1
        assert recurring.run_due() == []

    def test_a_failing_pdf_does_not_mean_a_second_invoice_tomorrow(self, offline,
                                                                  monkeypatch):
        template_id = self.make(auto=True)
        count = len(store.list_issued())

        def broken(invoice, path):
            raise RuntimeError("disco lleno")
        with monkeypatch.context() as m:
            m.setattr(finalize, "generate_invoice_pdf", broken)
            recurring.run_due()

        assert len(store.list_issued()) == count + 1   # the invoice exists
        assert recurring.run_due() == []               # and is not made again
        assert recurring.get(template_id)["next_date"] > TODAY.isoformat()

    def test_a_cancelled_one_never_runs(self, offline):
        recurring.cancel(self.make(auto=True))
        assert recurring.run_due() == []


# ── From the chat and the web ────────────────────────────────────────────────

class TestSurfaces:
    def test_issuing_offers_to_repeat_it_and_one_tap_does(self, offline, monkeypatch):
        from src import bot, conversation
        from test_telegram_access import press, say

        monkeypatch.setattr(bot, "parse_invoice_from_transcript", lambda t: InvoiceData(
            client_name="Comunidad Rambla 12", client_email="admin@rambla12.es",
            client_id="H12345678", items=[InvoiceItem("Mantenimiento", 1, 150.0)]))
        conversation._sessions.clear()
        chat = say(bot.handle_text, 5001, text="Factura mantenimiento")
        press(5001, "approve", chat)
        number = store.list_issued()[0]["invoice"].invoice_number
        assert f"rec:new:{number}" in chat.buttons()

        made = press(5001, f"rec:new:{number}")
        assert "cada mes" in made.text
        template = recurring.list_active()[0]
        assert template["source_number"] == number and not template["auto_send"]

        press(5001, f"rec:auto:{template['id']}")
        assert recurring.get(template["id"])["auto_send"] == 1
        press(5001, f"rec:del:{template['id']}")
        assert recurring.list_active() == []

    def test_recurrentes_lists_them(self, offline):
        from src import bot
        from test_telegram_access import say

        recurring.create_from_invoice(issued().invoice)
        chat = say(bot.cmd_recurrentes, 5001)
        assert "Comunidad Rambla 12" in chat.text and "cada mes" in chat.text

    def test_the_web_page(self, offline, owner, monkeypatch):
        import base64
        import json

        import itsdangerous
        from fastapi.testclient import TestClient

        monkeypatch.delenv("WEB_DEV_NO_AUTH", raising=False)
        monkeypatch.setenv("SESSION_SECRET", "test-secret")
        from src.web import app as web_app

        accounts.create_user("jefe@vila.es", accounts.ADMIN, company_id=owner)
        client = TestClient(web_app.app, follow_redirects=False)
        signer = itsdangerous.TimestampSigner("test-secret")
        data = base64.b64encode(json.dumps({"user": "jefe@vila.es"}).encode())
        client.cookies.set("session", signer.sign(data).decode())

        number = issued().number
        client.post("/recurring/new", data={"number": number, "frequency": "quarterly"})
        template = recurring.list_active()[0]
        assert template["frequency"] == "quarterly"
        assert "Comunidad Rambla 12" in client.get("/recurring").text

        client.post(f"/recurring/{template['id']}/auto")
        assert recurring.get(template["id"])["auto_send"] == 1
        client.post(f"/recurring/{template['id']}/cancel")
        assert recurring.list_active() == []
