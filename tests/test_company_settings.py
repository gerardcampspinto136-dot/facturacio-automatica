"""Setting a client up from the admin panel.

The point of these is that the form is not decoration: what the vendor types has to
reach the invoice. The last test here is the one that matters -- save the settings,
build a PDF, and read the client's own name and CIF back out of it.
"""

import base64
import json

import pytest
from fastapi.testclient import TestClient

from src import accounts
from src.config_loader import reload_config


@pytest.fixture
def client(monkeypatch):
    monkeypatch.delenv("WEB_DEV_NO_AUTH", raising=False)
    monkeypatch.setenv("SESSION_SECRET", "test-secret")
    from src.web import app as web_app

    return TestClient(web_app.app, follow_redirects=False)


@pytest.fixture(autouse=True)
def fresh_config():
    reload_config()
    yield
    reload_config()


def as_vendor(client):
    import itsdangerous

    accounts.create_user("gerard@vendor.es", accounts.SUPERADMIN)
    signer = itsdangerous.TimestampSigner("test-secret")
    data = base64.b64encode(json.dumps({"user": "gerard@vendor.es"}).encode())
    client.cookies.clear()
    client.cookies.set("session", signer.sign(data).decode())
    return client


FULL = {
    "name": "Talleres Mario S.L.",
    "tax_id": "B12345678",
    "address": "Carrer Indústria 5, 08025 Barcelona",
    "phone": "+34 933 111 222",
    "invoice_email": "facturacion@talleresmario.es",
    "iban": "ES21 0081 0298 1100 0123 4567",
    "tax_rate": "10",
    "payment_terms": "60 días",
    "prices_include_tax": "1",
    "review_mode": "auto",
    "telegram_bot_token": "1234567890:AAbbCCddEE",
    "telegram_chat_id": "998877",
}


# ── Storing them ─────────────────────────────────────────────────────────────

class TestSettingsStorage:
    def test_everything_from_the_form_is_kept(self):
        company_id = accounts.create_company("Sin configurar")
        accounts.update_company(company_id, **FULL)

        company = accounts.get_company(company_id)
        assert company["name"] == "Talleres Mario S.L."
        assert company["tax_id"] == "B12345678"
        assert company["iban"] == "ES21 0081 0298 1100 0123 4567"
        assert company["telegram_bot_token"] == "1234567890:AAbbCCddEE"

    def test_a_company_is_only_configured_once_the_fiscal_details_are_there(self):
        company_id = accounts.create_company("A medias")
        assert accounts.get_company(company_id)["configured_at"] is None
        assert "CIF" in accounts.missing_settings(accounts.get_company(company_id))

        accounts.update_company(company_id, tax_id="B12345678",
                                address="Calle Real 1")
        company = accounts.get_company(company_id)
        assert company["configured_at"] is not None
        assert accounts.missing_settings(company) == []

    def test_clearing_a_required_field_marks_it_unconfigured_again(self):
        company_id = accounts.create_company("Completa", tax_id="B12345678")
        accounts.update_company(company_id, address="Calle Real 1")
        assert accounts.get_company(company_id)["configured_at"] is not None

        accounts.update_company(company_id, address="")
        assert accounts.get_company(company_id)["configured_at"] is None

    def test_a_blank_name_is_refused(self):
        company_id = accounts.create_company("Tiene nombre")
        with pytest.raises(ValueError):
            accounts.update_company(company_id, name="   ")

    def test_unknown_fields_are_ignored(self):
        company_id = accounts.create_company("X")
        accounts.update_company(company_id, name="Y", es_admin="si")
        assert accounts.get_company(company_id)["name"] == "Y"


# ── Which company's settings are in force ────────────────────────────────────

class TestActiveCompany:
    def test_one_active_company_is_the_one_in_force(self):
        company_id = accounts.create_company("Talleres Mario S.L.")
        assert accounts.active_company()["id"] == company_id

    def test_no_companies_means_the_config_file_stays_in_charge(self):
        assert accounts.active_company() is None

    def test_two_active_companies_means_neither(self):
        accounts.create_company("Una S.L.")
        accounts.create_company("Otra S.L.")
        assert accounts.active_company() is None

    def test_suspending_one_of_two_resolves_it(self):
        first = accounts.create_company("Una S.L.")
        second = accounts.create_company("Otra S.L.")
        accounts.set_company_status(second, accounts.SUSPENDED)
        assert accounts.active_company()["id"] == first


# ── The settings reaching the invoice ────────────────────────────────────────

class TestSettingsDriveTheInvoice:
    def test_the_configured_company_replaces_the_file_defaults(self):
        company_id = accounts.create_company("Sin configurar")
        accounts.update_company(company_id, **FULL)

        config = reload_config()
        assert config.name == "Talleres Mario S.L."
        assert config.cif == "B12345678"
        assert config.bank_account == "ES21 0081 0298 1100 0123 4567"
        assert config.tax_rate == 10
        assert config.prices_include_tax is True
        assert config.review_mode == "auto"
        assert config.notify_telegram_chat_id == 998877
        assert config.is_placeholder is False

    def test_a_half_filled_company_keeps_the_defaults_for_the_rest(self):
        company_id = accounts.create_company("Media S.L.", tax_id="B99999999")
        config = reload_config()
        assert config.name == "Media S.L."
        # Never set, so the shipped default still applies rather than a blank.
        assert config.tax_rate == 21
        assert config.currency_symbol == "€"

    def test_an_unconfigured_company_still_stamps_invoices_as_tests(self):
        accounts.create_company("EMPRESA DE PRUEBA, S.L.")
        assert reload_config().is_placeholder is True

    def test_the_settings_reach_a_real_pdf(self, tmp_path):
        """The one that proves the form is wired to something."""
        pymupdf = pytest.importorskip("pymupdf")

        from src.invoice_generator import generate_invoice_pdf
        from src.models import InvoiceData, InvoiceItem

        company_id = accounts.create_company("Sin configurar")
        accounts.update_company(company_id, **FULL)
        reload_config()

        out = tmp_path / "factura.pdf"
        generate_invoice_pdf(
            InvoiceData(client_name="Cliente", client_email="c@x.es",
                        items=[InvoiceItem("Servicio", 1, 100.0)],
                        invoice_number="2026-0001"),
            str(out),
        )
        text = "".join(page.get_text() for page in pymupdf.open(str(out)))

        assert "Talleres Mario S.L." in text
        assert "B12345678" in text
        assert "ES21 0081 0298 1100 0123 4567" in text
        # Configured, so no test banner.
        assert "DOCUMENTO DE PRUEBA" not in text


# ── The panel itself ─────────────────────────────────────────────────────────

class TestSettingsPanel:
    def test_saving_the_form_configures_the_company(self, client):
        as_vendor(client)
        company_id = accounts.create_company("Sin configurar")

        response = client.post(f"/admin/{company_id}/settings", data=FULL)
        assert response.status_code == 303

        company = accounts.get_company(company_id)
        assert company["name"] == "Talleres Mario S.L."
        assert company["telegram_bot_token"] == "1234567890:AAbbCCddEE"
        assert company["tax_rate"] == 10.0
        assert company["configured_at"] is not None

    def test_saving_takes_effect_without_a_restart(self, client):
        as_vendor(client)
        company_id = accounts.create_company("Sin configurar")
        client.post(f"/admin/{company_id}/settings", data=FULL)
        # No reload_config() here on purpose: the route must have done it.
        from src.config_loader import get_config

        assert get_config().name == "Talleres Mario S.L."

    def test_the_form_shows_what_is_missing(self, client):
        as_vendor(client)
        company_id = accounts.create_company("A medias")
        body = client.get(f"/admin/{company_id}/settings").text
        assert "Faltan datos obligatorios" in body
        assert "CIF" in body

    def test_a_configured_company_says_so(self, client):
        as_vendor(client)
        company_id = accounts.create_company("Lista", tax_id="B12345678")
        accounts.update_company(company_id, address="Calle Real 1")
        assert "Lista para facturar" in client.get(f"/admin/{company_id}/settings").text

    def test_the_company_list_flags_a_missing_bot(self, client):
        as_vendor(client)
        accounts.create_company("Sin bot", tax_id="B12345678")
        assert "Sin bot de Telegram" in client.get("/admin").text

    def test_the_company_list_shows_a_connected_bot(self, client):
        as_vendor(client)
        company_id = accounts.create_company("Con bot", tax_id="B12345678")
        accounts.update_company(company_id, telegram_bot_token="123:ABC")
        assert "bot ✓" in client.get("/admin").text

    def test_an_owner_cannot_reach_the_settings(self, client):
        import itsdangerous

        company_id = accounts.create_company("Talleres Mario S.L.")
        accounts.create_user("mario@talleres.es", accounts.ADMIN, company_id=company_id)
        signer = itsdangerous.TimestampSigner("test-secret")
        data = base64.b64encode(json.dumps({"user": "mario@talleres.es"}).encode())
        client.cookies.set("session", signer.sign(data).decode())

        assert "No tienes permiso" in client.get(f"/admin/{company_id}/settings").text
        client.post(f"/admin/{company_id}/settings", data={"name": "Secuestrada"})
        assert accounts.get_company(company_id)["name"] == "Talleres Mario S.L."

    def test_settings_for_a_deleted_company_are_reported(self, client):
        as_vendor(client)
        assert "ya no existe" in client.get("/admin/999/settings").text


class TestBotToken:
    """Which bot the process runs as: each client company has its own."""

    def test_the_env_token_still_wins_for_existing_installs(self, monkeypatch):
        from src import bot

        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "from-env")
        company_id = accounts.create_company("Talleres Mario S.L.")
        accounts.update_company(company_id, telegram_bot_token="from-panel")
        assert bot._bot_token() == "from-env"

    def test_the_company_token_is_used_when_env_is_empty(self, monkeypatch):
        from src import bot

        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
        company_id = accounts.create_company("Talleres Mario S.L.")
        accounts.update_company(company_id, telegram_bot_token="from-panel")
        assert bot._bot_token() == "from-panel"

    def test_no_token_anywhere_is_none_not_a_crash(self, monkeypatch):
        from src import bot

        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
        accounts.create_company("Sin bot")
        assert bot._bot_token() is None

    def test_starting_without_a_token_says_where_to_put_one(self, monkeypatch):
        from src import bot

        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
        with pytest.raises(RuntimeError, match="panel de administración"):
            bot.run_bot()
