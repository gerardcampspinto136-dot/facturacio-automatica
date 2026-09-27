"""Quotes: dictated like an invoice, sent, and turned into the invoice with one tap.

What matters: a quote is not an invoice (its own numbering, no Verifactu record, no
tax id needed), and accepting it produces an invoice through the normal review, with
the quote marked as invoiced once that invoice is issued.
"""

from datetime import date, timedelta

import pytest

from src import accounts, conversation, finalize, quotes, store, verifactu
from src.config_loader import reload_config
from src.conversation import Session
from src.models import InvoiceData, InvoiceItem

YEAR = date.today().year


def a_quote(**kw):
    return InvoiceData(
        client_name=kw.pop("client_name", "Pere Soler"),
        client_email=kw.pop("client_email", "pere@soler.cat"),
        client_id=kw.pop("client_id", None),
        items=kw.pop("items", [InvoiceItem("Reforma del baño", 1, 3200.0)]),
        document="quote", **kw,
    )


@pytest.fixture(autouse=True)
def setup(monkeypatch, tmp_path):
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "5001")
    monkeypatch.setattr(quotes, "QUOTES_DIR", tmp_path / "presupuestos")
    company = accounts.create_company("Reformes Vila S.L.", tax_id="B12345678")
    accounts.update_company(company, address="Carrer Major 1")
    reload_config()
    conversation._sessions.clear()
    return company


class TestTheQuote:
    def test_its_own_numbering_and_no_verifactu_record(self, offline):
        result = quotes.issue(a_quote())
        assert result.number == f"P-{YEAR}-0001"
        assert verifactu.list_records() == []
        assert store.list_issued() == []

    def test_it_does_not_ask_for_a_tax_id(self):
        s = Session()
        replies = s.start(a_quote())
        assert s.awaiting == conversation.AWAIT_CONFIRM
        assert "Revisa el presupuesto" in replies[-1].text
        assert ("📤 Enviar presupuesto", "approve") in replies[-1].buttons

    def test_it_is_emailed_with_its_pdf(self, offline):
        pymupdf = pytest.importorskip("pymupdf")
        result = quotes.issue(a_quote())
        assert result.emailed and offline[-1][0] == "pere@soler.cat"
        text = pymupdf.open(result.pdf_path)[0].get_text()
        assert "PRESUPUESTO" in text and "Válido hasta" in text
        assert "QR tributario" not in text and "FACTURA" not in text

    def test_it_expires(self, offline):
        result = quotes.issue(a_quote(date=date.today() - timedelta(days=40)))
        assert quotes.status_label(quotes.get(result.number)) == "caducado"


class TestAccepting:
    def test_accepting_makes_an_invoice_through_the_normal_review(self, offline):
        number = quotes.issue(a_quote()).number
        invoice = quotes.to_invoice(number)
        s = Session()
        s.start(invoice)
        # The quote did not need the client's tax id; the invoice does.
        assert s.awaiting == "client_id"
        assert quotes.get(number)["status"] == "accepted"

    def test_issuing_that_invoice_marks_the_quote_invoiced(self, offline):
        number = quotes.issue(a_quote(client_id="12345678Z")).number
        result = finalize.issue(quotes.to_invoice(number))
        quote = quotes.get(number)
        assert (quote["status"], quote["invoice_number"]) == ("invoiced", result.number)
        assert result.invoice.items[0].description == "Reforma del baño"
        assert "presupuesto" in result.invoice.notes.lower()

    def test_a_quote_cannot_be_invoiced_twice(self, offline):
        number = quotes.issue(a_quote(client_id="12345678Z")).number
        finalize.issue(quotes.to_invoice(number))
        with pytest.raises(ValueError, match="ya se facturó"):
            quotes.to_invoice(number)

    def test_through_a_pending_draft_as_well(self, offline):
        number = quotes.issue(a_quote(client_id="12345678Z")).number
        token = store.add_pending(quotes.to_invoice(number), "d.pdf")
        pending = store.get_pending(token)["invoice"]
        finalize.issue(pending, token)
        assert quotes.get(number)["status"] == "invoiced"


class TestInTheChat:
    @pytest.fixture
    def dictated_quote(self, monkeypatch):
        from src import bot

        monkeypatch.setattr(bot, "parse_invoice_from_transcript", lambda t: a_quote())

    def test_dictate_send_accept_and_invoice(self, offline, dictated_quote):
        from src import bot
        from test_telegram_access import press, say

        chat = say(bot.handle_text, 5001, text="Presupuesto para Pere Soler")
        press(5001, "approve", chat)
        number = f"P-{YEAR}-0001"
        assert chat.documents[-1][0] == f"Presupuesto_{number}.pdf"
        assert f"quo:inv:{number}" in chat.buttons()

        accepted = press(5001, f"quo:inv:{number}")
        assert "Preparo la factura" in accepted.text
        # The review asks for the tax id the quote never needed.
        assert "NIF" in accepted.text

        say(bot.handle_text, 5001, text="12345678Z", chat=accepted)
        press(5001, "approve", accepted)
        issued = store.list_issued()
        assert len(issued) == 1
        assert quotes.get(number)["invoice_number"] == issued[0]["invoice"].invoice_number

    def test_rejecting(self, offline):
        from test_telegram_access import press

        number = quotes.issue(a_quote()).number
        press(5001, f"quo:rej:{number}")
        assert quotes.get(number)["status"] == "rejected"

    def test_presupuestos_lists_the_open_ones(self, offline):
        from src import bot
        from test_telegram_access import say

        number = quotes.issue(a_quote()).number
        chat = say(bot.cmd_presupuestos, 5001)
        assert number in chat.text and f"quo:inv:{number}" in chat.buttons()


class TestWeb:
    def test_list_pdf_convert_and_reject(self, offline, setup, monkeypatch):
        import base64
        import json

        import itsdangerous
        from fastapi.testclient import TestClient

        monkeypatch.delenv("WEB_DEV_NO_AUTH", raising=False)
        monkeypatch.setenv("SESSION_SECRET", "test-secret")
        from src.web import app as web_app

        accounts.create_user("jefe@vila.cat", accounts.ADMIN, company_id=setup)
        client = TestClient(web_app.app, follow_redirects=False)
        signer = itsdangerous.TimestampSigner("test-secret")
        data = base64.b64encode(json.dumps({"user": "jefe@vila.cat"}).encode())
        client.cookies.set("session", signer.sign(data).decode())

        first = quotes.issue(a_quote()).number
        second = quotes.issue(a_quote(client_name="Anna Puig")).number
        page = client.get("/quotes").text
        assert first in page and "Anna Puig" in page
        assert client.get(f"/quotes/{first}/pdf").headers["content-type"] == \
            "application/pdf"

        converted = client.post(f"/quotes/{first}/invoice")
        assert converted.status_code == 303
        assert converted.headers["location"].startswith("/invoice/")
        assert store.count_pending() == 1
        assert store.list_pending()[0]["invoice"].quote_number == first

        client.post(f"/quotes/{second}/reject")
        assert quotes.get(second)["status"] == "rejected"
