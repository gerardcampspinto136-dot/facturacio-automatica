"""Sending email by SMTP: the message is right, and nothing reaches a real server.

The Gmail API token expires every 7 days while the Google app is in "Testing", which
silently stopped every invoice email once. SMTP with an app password does not expire,
so it is what installations use; these tests pin its behaviour with a fake server.
"""

import pytest

import src.email_sender as email_sender


class FakeSMTP:
    """Stands in for smtplib.SMTP / SMTP_SSL and records what it was asked to do."""

    instances: list["FakeSMTP"] = []

    def __init__(self, host, port, timeout=None, context=None):
        self.host, self.port, self.sent, self.logged_in, self.tls = host, port, [], None, False
        FakeSMTP.instances.append(self)

    def starttls(self, context=None):
        self.tls = True

    def login(self, user, password):
        self.logged_in = (user, password)

    def send_message(self, msg):
        self.sent.append(msg)

    def quit(self):
        pass


@pytest.fixture
def smtp(monkeypatch):
    FakeSMTP.instances.clear()
    monkeypatch.setattr(email_sender.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(email_sender.smtplib, "SMTP_SSL", FakeSMTP)
    monkeypatch.setenv("SMTP_HOST", "smtp.gmail.com")
    monkeypatch.setenv("SMTP_PORT", "465")
    monkeypatch.setenv("SMTP_USER", "empresa@gmail.com")
    monkeypatch.setenv("SMTP_PASS", "app-password")
    monkeypatch.delenv("EMAIL_BACKEND", raising=False)
    return FakeSMTP.instances


def test_smtp_is_used_whenever_it_is_configured(smtp):
    assert email_sender.backend() == "smtp"


def test_without_smtp_it_falls_back_to_the_gmail_api(monkeypatch):
    monkeypatch.delenv("SMTP_HOST", raising=False)
    monkeypatch.delenv("EMAIL_BACKEND", raising=False)
    assert email_sender.backend() == "gmail"


def test_an_invoice_goes_out_with_its_pdf(smtp, tmp_path):
    pdf = tmp_path / "Factura_2026-0001.pdf"
    pdf.write_bytes(b"%PDF-1.4 test")
    email_sender.send_email("cliente@ejemplo.es", "Factura 2026-0001", "Adjunta.",
                            str(pdf), "Factura_2026-0001.pdf")
    server = smtp[-1]
    assert (server.host, server.port) == ("smtp.gmail.com", 465)
    assert server.logged_in == ("empresa@gmail.com", "app-password")
    msg = server.sent[0]
    assert msg["To"] == "cliente@ejemplo.es"
    attachment = next(msg.iter_attachments())
    assert attachment.get_content_type() == "application/pdf"
    assert attachment.get_filename() == "Factura_2026-0001.pdf"


def test_port_587_uses_starttls(smtp, monkeypatch):
    monkeypatch.setenv("SMTP_PORT", "587")
    email_sender.send_email("a@b.es", "x", "y")
    assert smtp[-1].tls


def test_the_company_is_the_sender_and_replies_go_to_its_address(smtp):
    config = email_sender.get_config()
    config.name, config.email = "Talleres Mario S.L.", "facturas@talleresmario.es"
    msg = email_sender.build_message("a@b.es", "x", "y")
    assert msg["From"] == '"Talleres Mario S.L." <empresa@gmail.com>'
    assert msg["Reply-To"] == "facturas@talleresmario.es"


@pytest.mark.parametrize("name,mime", [
    ("Gestor_2026-3T.zip", "application/zip"),
    ("Libros.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
])
def test_other_attachments_get_their_real_type(tmp_path, name, mime):
    path = tmp_path / name
    path.write_bytes(b"data")
    msg = email_sender.build_message("a@b.es", "x", "y", [(str(path), name)])
    assert next(msg.iter_attachments()).get_content_type() == mime


def test_checking_the_connection_logs_in_without_sending(smtp):
    ok, detail = email_sender.check_connection()
    assert ok and "smtp.gmail.com" in detail
    assert smtp[-1].sent == []


def test_a_revoked_google_token_is_reported_not_hidden(monkeypatch, tmp_path):
    """The failure that went unnoticed: the Gmail token revoked by Google."""
    monkeypatch.delenv("SMTP_HOST", raising=False)
    monkeypatch.delenv("EMAIL_BACKEND", raising=False)
    monkeypatch.setenv("GOOGLE_TOKEN_PATH", str(tmp_path / "missing.json"))
    ok, detail = email_sender.check_connection()
    assert not ok and "authorize_google" in detail
