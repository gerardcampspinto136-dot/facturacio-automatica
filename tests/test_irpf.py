"""IRPF withholding: what a professional's client keeps back and pays to Hacienda.

Without it a consultant, a designer or an architect invoicing a company cannot use the
software at all. What matters: the company default applies on its own, it can be taken
off (a private client, a foreign one) with one tap, whatever was decided is remembered
for that client, and it never touches the VAT.
"""

import pytest

from src import accounts, contacts, conversation, finalize, parser, store, verifactu
from src.config_loader import reload_config
from src.conversation import Session
from src.models import InvoiceData, InvoiceItem
from src.totals import breakdown


def an_invoice(**kw):
    return InvoiceData(
        client_name=kw.pop("client_name", "Estudio Nube S.L."),
        client_email=kw.pop("client_email", "admin@estudionube.es"),
        client_id=kw.pop("client_id", "B11223344"),
        items=kw.pop("items", [InvoiceItem("Consultoría", 1, 1000.0)]),
        **kw,
    )


def texts(replies):
    return " ".join(r.text for r in replies)


def buttons(replies):
    return [data for r in replies for _, data in r.buttons]


@pytest.fixture
def professional():
    """A company that applies 15% withholding to everything, as a consultant does."""
    company = accounts.create_company("Consultora Garcia", tax_id="12345678Z")
    accounts.update_company(company, irpf_rate=15)
    reload_config()
    return company


class TestDefault:
    def test_the_company_default_applies_on_its_own(self, professional):
        s = Session()
        replies = s.start(an_invoice())
        assert "Retención IRPF (15%)" in texts(replies)
        assert "TOTAL A PAGAR: 1.060,00" in texts(replies)
        assert "toggle_irpf" in buttons(replies)

    def test_a_company_without_withholding_never_sees_it(self):
        replies = Session().start(an_invoice())
        assert "Retención" not in texts(replies)
        assert "toggle_irpf" not in buttons(replies)

    def test_it_does_not_touch_the_vat(self, professional):
        t = breakdown(finalize.prepare(an_invoice()))
        assert (t.base, t.tax, t.irpf, t.total) == (1000.0, 210.0, 150.0, 1060.0)


class TestTheButton:
    def test_taking_it_off(self, professional):
        s = Session()
        s.start(an_invoice())
        replies = s.toggle_irpf()
        assert "sin retención" in texts(replies)
        assert "TOTAL: 1.210,00" in texts(replies)

    def test_putting_it_back(self, professional):
        s = Session()
        s.start(an_invoice())
        s.toggle_irpf()
        assert "Retención IRPF (15%)" in texts(s.toggle_irpf())

    def test_offered_to_a_professional_even_after_removal(self, professional):
        s = Session()
        s.start(an_invoice())
        assert "toggle_irpf" in buttons(s.toggle_irpf())

    def test_a_private_client_with_withholding_is_flagged(self, professional):
        replies = Session().start(an_invoice(client_id="SIN NIF"))
        assert "a un particular no se le aplica" in texts(replies)


class TestSaidOutLoud:
    def test_a_dictated_rate_wins_over_the_default(self, professional):
        s = Session()
        replies = s.start(an_invoice(irpf_rate=7))
        assert "Retención IRPF (7%)" in texts(replies)

    def test_sin_retencion_on_a_company_that_normally_applies_it(self, professional):
        replies = Session().start(an_invoice(irpf_rate=0))
        assert "Retención IRPF" not in texts(replies)

    def test_a_company_without_withholding_can_still_apply_it_once(self):
        replies = Session().start(an_invoice(irpf_rate=15))
        assert "Retención IRPF (15%)" in texts(replies)
        assert "toggle_irpf" in buttons(replies)

    @pytest.mark.parametrize("raw,expected", [
        (15, 15.0), ("7", 7.0), ("15%", 15.0), (0, 0.0), (None, None), (100, None),
        (-3, None), ("", None), (True, None),
    ])
    def test_implausible_rates_from_the_model_are_dropped(self, raw, expected):
        assert parser._rate(raw, maximum=50) == expected


class TestClientMemory:
    def test_removing_it_is_remembered_for_that_client(self, professional, offline):
        contact = contacts.create(contacts.CLIENT, "Estudio Nube S.L.",
                                  email="admin@estudionube.es", tax_id="B11223344")
        s = Session()
        s.start(an_invoice())
        s.toggle_irpf()
        s.remember_terms()
        assert contacts.get(contact)["irpf_rate"] == 0

        again = Session()
        replies = again.start(an_invoice())
        assert "Retención IRPF" not in texts(replies)

    def test_nothing_is_remembered_when_nothing_was_decided(self, professional):
        contact = contacts.create(contacts.CLIENT, "Estudio Nube S.L.",
                                  email="admin@estudionube.es", tax_id="B11223344")
        s = Session()
        s.start(an_invoice())
        s.remember_terms()
        assert contacts.get(contact)["irpf_rate"] is None

    def test_through_the_bot_end_to_end(self, professional, offline, monkeypatch):
        from src import bot
        from test_telegram_access import press, say

        monkeypatch.setenv("TELEGRAM_CHAT_ID", "5001")
        reload_config()  # the fixture above cached the settings before the chat was set
        monkeypatch.setattr(bot, "parse_invoice_from_transcript", lambda t: an_invoice())
        conversation._sessions.clear()

        chat = say(bot.handle_text, 5001, text="Factura para Estudio Nube")
        press(5001, "toggle_irpf", chat)
        press(5001, "approve", chat)

        issued = store.list_issued()[0]["invoice"]
        assert issued.irpf_rate == 0
        assert contacts.find_by_name("Estudio Nube S.L.")["irpf_rate"] == 0


class TestIssued:
    def test_the_rate_is_frozen_on_the_invoice(self, professional, offline):
        result = finalize.issue(an_invoice())
        accounts.update_company(accounts.active_company()["id"], irpf_rate=7)
        reload_config()
        assert store.get_issued(result.number)["invoice"].irpf_rate == 15

    def test_verifactu_registers_the_invoice_amount_not_the_net_payment(
            self, professional, offline):
        # The withholding is not part of the invoice's amount for VAT purposes.
        record = verifactu.record_for(finalize.issue(an_invoice()).number)
        assert (record["tax_total"], record["amount_total"]) == ("210.00", "1210.00")

    def test_what_the_client_owes_is_after_withholding(self, professional, offline):
        from src.totals import compute_totals

        finalize.issue(an_invoice())
        assert compute_totals(store.list_unpaid()[0]["invoice"])[2] == 1060.0


class TestPanel:
    @pytest.fixture
    def vendor_client(self, monkeypatch):
        import base64
        import json

        import itsdangerous
        from fastapi.testclient import TestClient

        monkeypatch.delenv("WEB_DEV_NO_AUTH", raising=False)
        monkeypatch.setenv("SESSION_SECRET", "test-secret")
        from src.web import app as web_app

        accounts.create_user("gerard@vendor.es", accounts.SUPERADMIN)
        client = TestClient(web_app.app, follow_redirects=False)
        signer = itsdangerous.TimestampSigner("test-secret")
        data = base64.b64encode(json.dumps({"user": "gerard@vendor.es"}).encode())
        client.cookies.set("session", signer.sign(data).decode())
        return client

    def test_the_default_is_set_in_the_panel(self, vendor_client):
        company = accounts.create_company("Consultora Garcia")
        vendor_client.post(f"/admin/{company}/settings",
                           data={"name": "Consultora Garcia", "irpf_rate": "15"})
        assert accounts.get_company(company)["irpf_rate"] == 15.0
        from src.config_loader import get_config

        assert get_config().irpf_rate == 15.0

    def test_nonsense_is_refused(self, vendor_client):
        company = accounts.create_company("Consultora Garcia")
        page = vendor_client.post(f"/admin/{company}/settings",
                                  data={"name": "Consultora Garcia", "irpf_rate": "80"})
        assert "entre 0 y 50" in page.text
        assert accounts.get_company(company)["irpf_rate"] is None

    def test_a_pending_invoice_can_have_it_changed(self, vendor_client, professional,
                                                   tmp_path):
        # The edit rebuilds the draft PDF, so it must live in the test's own folder.
        token = store.add_pending(an_invoice(), str(tmp_path / "draft.pdf"))
        vendor_client.post(f"/invoice/{token}/edit", data={
            "client_name": "Estudio Nube S.L.", "client_email": "admin@estudionube.es",
            "item_desc": "Consultoría", "item_qty": "1", "item_price": "1000",
            "tax_rate": "21", "irpf_rate": "0"})
        assert store.get_pending(token)["invoice"].irpf_rate == 0
