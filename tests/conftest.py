import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import db  # noqa: E402


@pytest.fixture(autouse=True)
def temp_db(tmp_path):
    """Give every test its own empty database file."""
    db.set_db_path(tmp_path / "test.db")
    db.connect()
    yield
    db.close()


@pytest.fixture(autouse=True)
def fresh_config():
    """Settings are cached per process; a test that changes them must not leak them."""
    from src import config_loader

    config_loader._config = None
    yield
    config_loader._config = None


@pytest.fixture(autouse=True)
def telegram_outbox(monkeypatch):
    """Nothing in the tests reaches Telegram. What would have been sent is recorded here."""
    from src import telegram_access, telegram_api

    sent: list[tuple[str, dict]] = []

    def fake_call(method, payload, files=None):
        sent.append((method, payload))
        return {"username": "facturas_test_bot"} if method == "getMe" else {}

    monkeypatch.setattr(telegram_api, "_call", fake_call)
    telegram_api._username_cache.clear()
    telegram_access._reported.clear()
    return sent


@pytest.fixture
def offline(monkeypatch, tmp_path):
    """Stub Sheets and Gmail so issuing an invoice never leaves the machine.

    The PDF is still really built -- it is local, and a broken PDF is worth catching --
    but into the test's own folder, never into the real data/invoices.
    Returns the list of emails that would have gone out, as (to, subject, pdf) tuples.
    """
    import src.finalize as finalize_module

    monkeypatch.setattr(finalize_module, "INVOICES_DIR", tmp_path / "invoices")
    monkeypatch.setattr(finalize_module, "DRAFTS_DIR", tmp_path / "invoices" / "borradores")

    outbox: list[tuple] = []

    sheets = types.ModuleType("src.sheets")
    sheets.add_invoice_to_sheet = lambda invoice: None

    email_sender = types.ModuleType("src.email_sender")

    def send_invoice_email(invoice, path):
        outbox.append((invoice.client_email, f"Factura {invoice.invoice_number}", path))

    def send_email(to, subject, body, pdf_path=None, attachment_name=None):
        outbox.append((to, subject, pdf_path))

    email_sender.send_invoice_email = send_invoice_email
    email_sender.send_email = send_email

    monkeypatch.setitem(sys.modules, "src.sheets", sheets)
    monkeypatch.setitem(sys.modules, "src.email_sender", email_sender)

    import src.finalize as finalize
    import src.rectify as rectify

    for module in (finalize, rectify):
        for name, value in (("add_invoice_to_sheet", sheets.add_invoice_to_sheet),
                            ("send_invoice_email", send_invoice_email)):
            if hasattr(module, name):
                monkeypatch.setattr(module, name, value)
    if hasattr(rectify, "add_invoice_to_sheet_safe"):
        monkeypatch.setattr(rectify, "add_invoice_to_sheet_safe", lambda invoice: None)
    return outbox
