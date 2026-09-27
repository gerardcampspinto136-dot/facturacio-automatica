"""Clients and suppliers: fixable on the web, and kept up to date by the bot.

A client stored once with a wrong email used to be wrong forever -- the bot filled
every later invoice from the record and nothing could edit it. And an email or NIF
given later lived only on that one invoice.
"""

import base64
import json

import itsdangerous
import pytest
from fastapi.testclient import TestClient

from src import accounts, contacts, conversation, finalize, store
from src.conversation import Session
from src.models import InvoiceData, InvoiceItem


@pytest.fixture
def company():
    return accounts.create_company("Talleres Mario S.L.", tax_id="B12345678")


@pytest.fixture
def client(monkeypatch, company):
    monkeypatch.delenv("WEB_DEV_NO_AUTH", raising=False)
    monkeypatch.setenv("SESSION_SECRET", "test-secret")
    from src.web import app as web_app

    accounts.create_user("jefe@talleres.es", accounts.ADMIN, company_id=company)
    accounts.create_user("pepe@talleres.es", accounts.EMPLOYEE, company_id=company,
                         permissions=["contacts.view"])
    return TestClient(web_app.app, follow_redirects=False)


def sign_in(client, email):
    signer = itsdangerous.TimestampSigner("test-secret")
    data = base64.b64encode(json.dumps({"user": email}).encode())
    client.cookies.set("session", signer.sign(data).decode())


# ── The page ─────────────────────────────────────────────────────────────────

class TestPage:
    def test_the_list_and_the_search(self, client):
        contacts.create(contacts.CLIENT, "Talleres Puig", tax_id="B87654321")
        contacts.create(contacts.CLIENT, "Bar Pepe", email="bar@pepe.es")
        sign_in(client, "jefe@talleres.es")
        assert "Talleres Puig" in client.get("/contacts").text
        found = client.get("/contacts?q=bar@pepe").text
        assert "Bar Pepe" in found and "Talleres Puig" not in found

    def test_a_wrong_email_can_finally_be_fixed(self, client):
        cid = contacts.create(contacts.CLIENT, "Talleres Puig", email="viejo@puig.es")
        sign_in(client, "jefe@talleres.es")
        client.post(f"/contacts/{cid}", data={
            "name": "Talleres Puig", "email": "nuevo@puig.es", "tax_id": "b87654321",
            "payment_terms_days": "60", "irpf_rate": ""})
        stored = contacts.get(cid)
        assert (stored["email"], stored["tax_id"], stored["payment_terms_days"]) == \
            ("nuevo@puig.es", "B87654321", 60)

    def test_a_duplicate_tax_id_is_refused_in_words(self, client):
        contacts.create(contacts.CLIENT, "Uno", tax_id="B11111111")
        other = contacts.create(contacts.CLIENT, "Dos")
        sign_in(client, "jefe@talleres.es")
        page = client.post(f"/contacts/{other}", data={"name": "Dos",
                                                      "tax_id": "B11111111"}).text
        assert "Ya hay otro con ese NIF" in page

    def test_the_client_page_shows_what_they_owe(self, client, offline):
        cid = contacts.create(contacts.CLIENT, "Talleres Puig", email="t@puig.es")
        finalize.issue(InvoiceData("Talleres Puig", "t@puig.es",
                                   [InvoiceItem("Servicio", 1, 100.0)],
                                   client_id="B87654321", contact_id=cid))
        sign_in(client, "jefe@talleres.es")
        page = client.get(f"/contacts/{cid}").text
        assert "121,00" in page and "Sus facturas" in page

    def test_a_viewer_can_look_but_not_change(self, client):
        cid = contacts.create(contacts.CLIENT, "Talleres Puig", email="t@puig.es")
        sign_in(client, "pepe@talleres.es")
        assert "readonly" in client.get(f"/contacts/{cid}").text
        client.post(f"/contacts/{cid}", data={"name": "Otro nombre"})
        assert contacts.get(cid)["name"] == "Talleres Puig"

    def test_adding_one(self, client):
        sign_in(client, "jefe@talleres.es")
        client.post("/contacts/new", data={"kind": "supplier", "name": "Ferretería Puig",
                                           "tax_id": "b22222222"})
        stored = contacts.find_by_name("Ferretería Puig", contacts.SUPPLIER)
        assert stored and stored["tax_id"] == "B22222222"


# ── The bot keeps the record current ─────────────────────────────────────────

def an_invoice(**kw):
    return InvoiceData(client_name="Talleres Puig", client_email=kw.pop("email", ""),
                       client_id=kw.pop("client_id", None),
                       items=[InvoiceItem("Reparación", 1, 300.0)], **kw)


class TestSync:
    def test_a_new_email_dictated_is_remembered(self):
        cid = contacts.create(contacts.CLIENT, "Talleres Puig", email="viejo@puig.es",
                              tax_id="B87654321")
        s = Session()
        s.start(an_invoice(email="nuevo@puig.es"))
        assert s.sync_contact() == ["el email"]
        assert contacts.get(cid)["email"] == "nuevo@puig.es"

    def test_a_nif_asked_for_is_remembered(self):
        cid = contacts.create(contacts.CLIENT, "Talleres Puig", email="t@puig.es")
        s = Session()
        s.start(an_invoice())
        assert s.awaiting == "client_id"
        s.handle_text("B87654321")
        assert s.sync_contact() == ["el NIF"]
        assert contacts.get(cid)["tax_id"] == "B87654321"

    def test_nothing_changes_when_nothing_is_new(self):
        contacts.create(contacts.CLIENT, "Talleres Puig", email="t@puig.es",
                        tax_id="B87654321")
        s = Session()
        s.start(an_invoice())
        assert s.sync_contact() == []

    def test_two_clients_without_nif_can_both_be_saved(self):
        for name in ("Particular Uno", "Particular Dos"):
            s = Session()
            s.start(InvoiceData(name, "p@x.es", [InvoiceItem("x", 1, 10.0)],
                                client_id="SIN NIF"))
            assert s.save_contact() is not None
        assert len(contacts.list_all(contacts.CLIENT)) == 2

    def test_through_the_bot_it_says_so(self, offline, monkeypatch):
        from src import bot
        from test_telegram_access import press, say

        monkeypatch.setenv("TELEGRAM_CHAT_ID", "5001")
        conversation._sessions.clear()
        contacts.create(contacts.CLIENT, "Talleres Puig", email="viejo@puig.es",
                        tax_id="B87654321")
        monkeypatch.setattr(bot, "parse_invoice_from_transcript",
                            lambda t: an_invoice(email="nuevo@puig.es"))
        chat = say(bot.handle_text, 5001, text="factura")
        press(5001, "approve", chat)
        assert "He actualizado el email de Talleres Puig" in chat.text
        assert store.list_issued()[0]["invoice"].client_email == "nuevo@puig.es"
