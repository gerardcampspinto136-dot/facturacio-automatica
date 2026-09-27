"""An invoice without VAT says why, as the law requires -- and the reason is remembered.

RD 1619/2012 art. 6.1: an invoice at 0% must state the exemption, or "inversión del
sujeto pasivo" when the client accounts for the VAT. The wrong one is not harmless
("exenta" on a service to a French company is a false statement to Hacienda), so the
reason is asked for, never guessed; and then it is remembered for the client, or set
once for a business whose whole activity is exempt.
"""

from datetime import date

import pytest

from src import (accounts, contacts, conversation, db, exemptions, finalize, quotes,
                 rectify, recurring, store, taxes)
from src.config_loader import reload_config
from src.conversation import Session
from src.models import InvoiceData, InvoiceItem

pytestmark = pytest.mark.usefixtures("offline")

EU_SERVICES = exemptions.REASONS["eu_services"]


def an_invoice(**kw):
    return InvoiceData(
        client_name=kw.pop("client_name", "Studio Lumière SARL"),
        client_email=kw.pop("client_email", "compta@lumiere.fr"),
        client_id=kw.pop("client_id", "FR40303265045"),
        items=kw.pop("items", [InvoiceItem("Diseño de la web", 1, 2000.0)]),
        **kw,
    )


def texts(replies):
    return " ".join(r.text for r in replies)


def buttons(replies):
    return [data for r in replies for _, data in r.buttons]


@pytest.fixture(autouse=True)
def company(monkeypatch):
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "5001")
    company = accounts.create_company("Estudio Nube S.L.", tax_id="B11223344")
    accounts.update_company(company, address="Carrer Major 1, 08001 Barcelona")
    reload_config()
    conversation._sessions.clear()
    return company


def approve(session):
    """What the bot does on "Enviar": remember the client and the terms, then issue."""
    if session.contact_is_new():
        session.save_contact()
    session.remember_terms()
    return finalize.issue(session.invoice)


class TestAsked:
    def test_no_vat_and_no_reason_is_asked_before_anything_is_shown(self):
        s = Session()
        replies = s.start(an_invoice(tax_rate=0))
        assert "sin IVA" in texts(replies) and "motivo" in texts(replies)
        assert "vatwhy:eu_services" in buttons(replies)
        assert "approve" not in buttons(replies)          # nothing to send yet
        # An id from another EU country: the EU reasons come first.
        assert buttons(replies)[0].startswith("vatwhy:eu_")

    def test_the_chosen_reason_is_printed_and_remembered_for_the_client(self):
        s = Session()
        s.start(an_invoice(tax_rate=0))
        replies = s.choose_vat_reason("eu_services")
        assert "Sin IVA: Servicio a empresa de la UE" in texts(replies)
        assert "Inversión del sujeto pasivo" in texts(replies)
        assert "approve" in buttons(replies)

        result = approve(s)
        record = store.get_issued(result.number)["invoice"]
        assert record.vat_reason == "eu_services"
        assert EU_SERVICES.text in record.notes
        contact = contacts.find_by_name("Studio Lumière SARL")
        assert contact["vat_reason"] == "eu_services"

        # Next time nothing needs saying: 0%, the reason, straight to the review.
        again = Session()
        replies = again.start(an_invoice())
        assert "Sin IVA: Servicio a empresa de la UE" in texts(replies)
        assert again.invoice.tax_rate == 0 and "approve" in buttons(replies)

    @pytest.mark.parametrize("answer, key", [
        ("es un servicio a una empresa francesa", "eu_services"),
        ("le vendo material a una empresa de Alemania", "eu_goods"),
        ("servicio a un cliente de fuera de la UE", "non_eu_services"),
        ("es de Estados Unidos", "non_eu_services"),
        ("exportación", "export"),
        ("es formación, está exenta", "exempt"),
        ("subcontrata de una obra", "reverse_charge"),
        ("2", "eu_services"),
    ])
    def test_a_typed_or_spoken_answer(self, answer, key):
        s = Session()
        s.start(an_invoice(tax_rate=0))
        s.handle_text(answer)
        assert s.invoice.vat_reason == key

    def test_an_answer_that_names_nothing_is_asked_again(self):
        s = Session()
        s.start(an_invoice(tax_rate=0))
        replies = s.handle_text("pues no sé")
        assert "No lo he entendido" in texts(replies)
        assert s.awaiting == conversation.AWAIT_VAT_REASON

    def test_an_exempt_service_is_not_pinned_on_the_client(self):
        # "This course is exempt" is about the service, not about who buys it.
        s = Session()
        s.start(an_invoice(tax_rate=0, client_id="B87654321", client_name="Acme S.L."))
        s.choose_vat_reason("exempt")
        approve(s)
        assert contacts.find_by_name("Acme S.L.")["vat_reason"] is None

    def test_changing_the_reason_leaves_one_mention(self):
        s = Session()
        s.start(an_invoice(tax_rate=0))
        s.choose_vat_reason("eu_goods")
        s.ask_vat_reason()
        s.choose_vat_reason("eu_services")
        notes = s.invoice.notes
        assert EU_SERVICES.text in notes
        assert exemptions.REASONS["eu_goods"].text not in notes

    def test_an_eu_company_charged_vat_gets_a_warning_and_a_way_out(self):
        s = Session()
        replies = s.start(an_invoice())                  # 21% by default
        assert "otro país de la UE" in texts(replies) and "vat0" in buttons(replies)
        replies = s.drop_vat()
        assert s.invoice.tax_rate == 0 and "vatwhy:eu_services" in buttons(replies)

    def test_a_spanish_client_with_vat_hears_nothing_about_it(self):
        replies = Session().start(an_invoice(client_id="B87654321"))
        assert "UE" not in texts(replies)
        assert not [b for b in buttons(replies) if b.startswith("vat")]


class TestExemptBusiness:
    @pytest.fixture
    def academy(self, company):
        accounts.update_company(company, tax_rate=0, vat_reason="exempt")
        reload_config()

    def test_never_asked_and_always_printed(self, academy):
        s = Session()
        replies = s.start(an_invoice(client_id="12345678Z", client_name="Marta Soler"))
        assert "approve" in buttons(replies)
        assert "artículo 20" in texts(replies)
        result = approve(s)
        assert "artículo 20" in store.get_issued(result.number)["invoice"].notes

    def test_the_settings_page_keeps_a_zero_rate(self, academy, monkeypatch):
        """The form showed `tax_rate or 21`: an exempt business saw 21, and saving any
        other setting quietly put its invoices back at 21%."""
        import base64
        import json

        import itsdangerous
        from fastapi.testclient import TestClient

        monkeypatch.delenv("WEB_DEV_NO_AUTH", raising=False)
        monkeypatch.setenv("SESSION_SECRET", "test-secret")
        accounts.create_user("vendor@example.com", accounts.SUPERADMIN)
        from src.web import app as web_app

        client = TestClient(web_app.app, follow_redirects=False)
        signer = itsdangerous.TimestampSigner("test-secret")
        cookie = base64.b64encode(json.dumps({"user": "vendor@example.com"}).encode())
        client.cookies.set("session", signer.sign(cookie).decode())

        company_id = accounts.list_companies()[0]["id"]
        page = client.get(f"/admin/{company_id}/settings").text
        assert "name='tax_rate' type='number' value='0'" in page
        assert "value='exempt' selected" in page


class TestItTravels:
    def issued_eu(self):
        s = Session()
        s.start(an_invoice(tax_rate=0))
        s.choose_vat_reason("eu_services")
        return approve(s)

    def test_frozen_once_issued(self):
        import sqlite3

        number = self.issued_eu().number
        with pytest.raises(sqlite3.DatabaseError):
            with db.transaction() as conn:
                conn.execute("UPDATE invoices SET vat_reason = 'exempt' WHERE number = ?",
                             (number,))

    def test_the_rectifying_invoice_says_it_too(self):
        credit = rectify.rectify(self.issued_eu().number)
        invoice = store.get_issued(credit.number)["invoice"]
        assert invoice.vat_reason == "eu_services" and EU_SERVICES.text in invoice.notes
        assert "rectificativa" in invoice.notes

    def test_a_quote_and_the_invoice_it_becomes(self, tmp_path, monkeypatch):
        monkeypatch.setattr(quotes, "QUOTES_DIR", tmp_path / "presupuestos")
        number = quotes.issue(an_invoice(tax_rate=0, vat_reason="eu_services",
                                         document="quote")).number
        assert EU_SERVICES.text in quotes.get(number)["invoice"].notes
        invoice = quotes.to_invoice(number)
        result = finalize.issue(invoice)
        stored = store.get_issued(result.number)["invoice"]
        assert stored.vat_reason == "eu_services"
        assert "Según presupuesto" in stored.notes and EU_SERVICES.text in stored.notes

    def test_a_recurring_invoice(self):
        original = store.get_issued(self.issued_eu().number)["invoice"]
        template = recurring.get(recurring.create_from_invoice(original, recurring.MONTHLY))
        next_one = finalize.issue(recurring.invoice_for(template, date.today()))
        stored = store.get_issued(next_one.number)["invoice"]
        assert stored.vat_reason == "eu_services" and EU_SERVICES.text in stored.notes

    def test_on_the_pdf(self):
        pymupdf = pytest.importorskip("pymupdf")
        result = self.issued_eu()
        text = " ".join(pymupdf.open(result.pdf_path)[0].get_text().split())
        assert "Inversión del sujeto pasivo" in text

    def test_adding_vat_back_removes_the_mention(self):
        invoice = an_invoice(tax_rate=0, vat_reason="eu_services")
        exemptions.settle(invoice)
        assert EU_SERVICES.text in invoice.notes
        invoice.tax_rate = 21
        exemptions.settle(invoice)
        assert invoice.vat_reason is None and not invoice.notes


class TestTheQuarter:
    def test_the_gestor_sees_each_kind_apart(self):
        TestItTravels().issued_eu()
        finalize.issue(an_invoice(client_id="B87654321", client_name="Acme S.L."))
        year, quarter = taxes.quarter_of(date.today())
        vat = taxes.vat_return(year, quarter)
        assert vat.without_vat == {"eu_services": 2000.0}
        rows = {name: base for name, base, _ in taxes.vat_rows(vat)}
        assert rows["Sin IVA: Servicio a empresa de la UE"] == 2000.0
        summary = taxes.summary_text(year, quarter)
        assert "Sin IVA: Servicio a empresa de la UE: 2.000,00" in summary
        assert "IVA repercutido 21%: 420,00" in summary


class TestPanel:
    @pytest.fixture
    def client(self, company, monkeypatch):
        import base64
        import json

        import itsdangerous
        from fastapi.testclient import TestClient

        monkeypatch.delenv("WEB_DEV_NO_AUTH", raising=False)
        monkeypatch.setenv("SESSION_SECRET", "test-secret")
        from src.web import app as web_app

        accounts.create_user("jefe@nube.es", accounts.ADMIN, company_id=company)
        client = TestClient(web_app.app, follow_redirects=False)
        signer = itsdangerous.TimestampSigner("test-secret")
        cookie = base64.b64encode(json.dumps({"user": "jefe@nube.es"}).encode())
        client.cookies.set("session", signer.sign(cookie).decode())
        return client

    def test_a_client_marked_eu_in_the_panel(self, client):
        cid = contacts.create(contacts.CLIENT, "Studio Lumière SARL",
                              tax_id="FR40303265045")
        assert "Servicio a empresa de la UE" in client.get(f"/contacts/{cid}").text
        client.post(f"/contacts/{cid}", data={"name": "Studio Lumière SARL",
                                              "vat_reason": "eu_services"})
        assert contacts.get(cid)["vat_reason"] == "eu_services"
        client.post(f"/contacts/{cid}", data={"name": "Studio Lumière SARL",
                                              "vat_reason": "nonsense"})
        assert contacts.get(cid)["vat_reason"] is None

    def test_a_draft_at_zero_without_a_reason_is_flagged_and_fixed_in_edit(self, client):
        token = store.add_pending(an_invoice(tax_rate=0), "d.pdf")
        assert "falta el motivo" in client.get("/pending").text
        client.post(f"/invoice/{token}/edit", data={
            "client_name": "Studio Lumière SARL", "client_email": "compta@lumiere.fr",
            "client_id": "FR40303265045", "tax_rate": "0", "irpf_rate": "0",
            "vat_reason": "eu_services", "item_desc": "Diseño de la web",
            "item_qty": "1", "item_price": "2000"})
        draft = store.get_pending(token)["invoice"]
        assert draft.vat_reason == "eu_services" and EU_SERVICES.text in draft.notes
        assert "falta el motivo" not in client.get("/pending").text


class TestElectronic:
    def test_reverse_charge_in_ubl_and_facturae(self):
        from pathlib import Path

        etree = pytest.importorskip("lxml.etree")
        from src import einvoice

        result = finalize.issue(an_invoice(
            client_name="Construcciones Puig S.L.", client_id="B87654321",
            client_address="Carrer Nou 3, 17001 Girona", tax_rate=0,
            vat_reason="reverse_charge"))
        ubl = etree.fromstring(einvoice.ubl(result.number))
        ns = {"cac": einvoice.CAC, "cbc": einvoice.CBC}
        category = ubl.find("cac:TaxTotal/cac:TaxSubtotal/cac:TaxCategory", ns)
        assert category.findtext("cbc:ID", namespaces=ns) == "AE"
        assert "Inversión del sujeto pasivo" in category.findtext(
            "cbc:TaxExemptionReason", namespaces=ns)

        fixtures = Path(__file__).parent / "fixtures"
        ubl_schema = etree.XMLSchema(etree.parse(
            str(fixtures / "ubl" / "maindoc" / "UBL-Invoice-2.1.xsd")))
        ubl_schema.assertValid(ubl)

        facturae = etree.fromstring(einvoice.facturae(result.number))
        # Neither exempt nor "not subject": no special event, the legal text says it.
        assert facturae.find(".//SpecialTaxableEvent") is None
        assert "Inversión del sujeto pasivo" in facturae.findtext(
            ".//LegalLiterals/LegalReference")
