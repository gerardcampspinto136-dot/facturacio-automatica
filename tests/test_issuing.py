"""Issuing an invoice: gap-free, frozen once issued, and honest about the email.

The number and the invoice are written in one transaction, so nothing -- a crash, Gmail
being down, two people approving the same draft -- can leave a number with no invoice
behind it. Once issued, the database itself refuses to change or delete the invoice.
And whether the email actually went out is recorded and reported, never assumed.
"""

import sqlite3
from datetime import date, timedelta

import pytest

from src import accounts, db, finalize, rectify, store
from src.config_loader import reload_config
from src.invoice_number import clean_series, peek_invoice_number
from src.models import InvoiceData, InvoiceItem
from src.totals import compute_totals

YEAR = date.today().year


def an_invoice(**kw):
    return InvoiceData(
        client_name=kw.pop("client_name", "Talleres Puig"),
        client_email=kw.pop("client_email", "taller@puig.es"),
        client_id=kw.pop("client_id", "B87654321"),
        items=kw.pop("items", [InvoiceItem("Reparación", 1, 100.0)]),
        **kw,
    )


@pytest.fixture
def gmail_down(offline, monkeypatch):
    def fail(invoice, path):
        raise RuntimeError("Gmail no responde")
    monkeypatch.setattr(finalize, "send_invoice_email", fail)
    return offline


# ── No gaps, ever ────────────────────────────────────────────────────────────

class TestNoGaps:
    def test_numbers_follow_on(self, offline):
        first = finalize.issue(an_invoice())
        second = finalize.issue(an_invoice())
        assert (first.number, second.number) == (f"{YEAR}-0001", f"{YEAR}-0002")

    def test_approving_a_draft_twice_consumes_one_number(self, offline):
        token = store.add_pending(an_invoice(), "d.pdf")
        finalize.issue(store.get_pending(token)["invoice"], token)
        with pytest.raises(KeyError):
            finalize.issue(an_invoice(), token)
        assert peek_invoice_number() == f"{YEAR}-0002"

    def test_a_failure_while_recording_rolls_the_number_back(self, offline, monkeypatch):
        def broken(*a, **k):
            raise RuntimeError("disco lleno")
        monkeypatch.setattr(store, "write_issued", broken)
        with pytest.raises(RuntimeError):
            finalize.issue(an_invoice())
        assert peek_invoice_number() == f"{YEAR}-0001"

    def test_gmail_being_down_does_not_lose_the_invoice(self, gmail_down):
        result = finalize.issue(an_invoice())
        assert store.get_issued(result.number) is not None
        assert peek_invoice_number() == f"{YEAR}-0002"


# ── Frozen once issued ───────────────────────────────────────────────────────

class TestFrozen:
    def issued(self, offline_fixture=None):
        result = finalize.issue(an_invoice())
        return result.number, store.get_issued(result.number)["id"]

    def test_the_amounts_cannot_be_edited(self, offline):
        _, invoice_id = self.issued()
        with pytest.raises(sqlite3.DatabaseError, match="rectificativa"):
            with db.transaction() as conn:
                conn.execute("UPDATE invoice_items SET total = 1 WHERE invoice_id = ?",
                             (invoice_id,))

    def test_the_client_cannot_be_changed(self, offline):
        number, _ = self.issued()
        with pytest.raises(sqlite3.DatabaseError):
            with db.transaction() as conn:
                conn.execute("UPDATE invoices SET client_name = 'Otro' WHERE number = ?",
                             (number,))

    def test_it_cannot_be_deleted(self, offline):
        number, _ = self.issued()
        with pytest.raises(sqlite3.DatabaseError):
            with db.transaction() as conn:
                conn.execute("DELETE FROM invoices WHERE number = ?", (number,))

    def test_lines_cannot_be_added_to_it(self, offline):
        _, invoice_id = self.issued()
        with pytest.raises(sqlite3.DatabaseError):
            with db.transaction() as conn:
                conn.execute("INSERT INTO invoice_items (invoice_id, description) "
                             "VALUES (?, 'extra')", (invoice_id,))

    def test_payments_are_still_recorded(self, offline):
        number, _ = self.issued()
        store.mark_paid(number)
        assert store.get_issued(number)["paid_at"]

    def test_a_pending_draft_is_still_editable(self):
        token = store.add_pending(an_invoice(), "d.pdf")
        store.update_pending(token, an_invoice(client_name="Otro nombre"))
        assert store.get_pending(token)["invoice"].client_name == "Otro nombre"


# ── Telling the truth about the email ────────────────────────────────────────

class TestEmailStatus:
    def test_a_sent_email_is_recorded(self, offline):
        result = finalize.issue(an_invoice())
        assert result.emailed and "Enviada a taller@puig.es" in result.email_status
        assert store.get_issued(result.number)["email_sent_at"]

    def test_a_failed_email_is_reported_not_hidden(self, gmail_down):
        result = finalize.issue(an_invoice())
        assert not result.emailed
        assert "NO se ha podido enviar" in result.email_status
        assert "Gmail no responde" in result.email_status
        assert f"/reenviar {result.number}" in result.email_status
        assert store.get_issued(result.number)["email_error"] == "Gmail no responde"

    def test_resending_clears_the_error(self, gmail_down, monkeypatch):
        result = finalize.issue(an_invoice())
        monkeypatch.setattr(finalize, "send_invoice_email", lambda inv, path: None)

        sent, error = finalize.resend(result.number)

        assert sent and error is None
        record = store.get_issued(result.number)
        assert record["email_error"] is None and record["email_sent_at"]

    def test_no_address_says_so(self, offline):
        result = finalize.issue(an_invoice(client_email=""))
        assert "falta el email" in result.email_status

    def test_the_pdf_of_an_issued_invoice_is_rebuilt_if_missing(self, offline):
        import os

        result = finalize.issue(an_invoice())
        os.unlink(result.pdf_path)
        assert os.path.exists(finalize.pdf_for(result.number))


# ── Rectifying ───────────────────────────────────────────────────────────────

class TestRectifying:
    def test_gmail_down_no_longer_burns_a_number(self, gmail_down):
        original = finalize.issue(an_invoice())
        result = rectify.rectify(original.number)

        assert result.number == f"R-{YEAR}-0001"
        assert store.get_issued(result.number) is not None
        assert store.get_issued(original.number)["rectified_by"] == result.number
        assert not result.emailed

    def test_cancelling_twice_is_refused_without_consuming_a_number(self, offline):
        original = finalize.issue(an_invoice())
        rectify.rectify(original.number)
        with pytest.raises(ValueError, match="rectificada|anulada"):
            rectify.rectify(original.number)
        assert peek_invoice_number("R") == f"R-{YEAR}-0002"

    def test_a_rectifying_invoice_cannot_itself_be_cancelled(self, offline):
        original = finalize.issue(an_invoice())
        credit = rectify.rectify(original.number)
        with pytest.raises(ValueError, match="ya es una rectificativa"):
            rectify.rectify(credit.number)

    def test_it_cancels_to_the_cent_at_the_original_rate(self, offline, monkeypatch):
        original = finalize.issue(an_invoice())
        reload_config().tax_rate = 10  # the default changed since
        credit = rectify.rectify(original.number)
        assert compute_totals(credit.invoice)[2] == -compute_totals(original.invoice)[2]

    def test_the_reason_and_the_original_date_are_on_it(self, offline):
        original = finalize.issue(an_invoice())
        credit = rectify.rectify(original.number, "precio equivocado")
        assert "precio equivocado" in credit.invoice.notes
        assert original.invoice.date.strftime("%d/%m/%Y") in credit.invoice.notes


# ── What an invoice is issued under ──────────────────────────────────────────

class TestSeriesAndRates:
    def test_the_series_from_the_panel_is_used(self, offline):
        company = accounts.create_company("Talleres Mario S.L.")
        accounts.update_company(company, invoice_series="A")
        reload_config()
        assert finalize.issue(an_invoice()).number == f"A-{YEAR}-0001"

    @pytest.mark.parametrize("bad", ["R", "P", "A1", "DEMASIADO"])
    def test_unusable_series_are_refused(self, bad):
        with pytest.raises(ValueError):
            clean_series(bad)

    def test_a_series_is_normalised(self):
        assert clean_series(" tm ") == "TM"

    def test_the_vat_rate_is_frozen_at_issue(self, offline):
        result = finalize.issue(an_invoice())
        reload_config().tax_rate = 10
        stored = store.get_issued(result.number)["invoice"]
        assert stored.tax_rate == 21
        assert compute_totals(stored)[2] == 121.0

    def test_invoices_from_before_keep_using_the_default(self):
        # Recorded with no rate of its own, as every invoice from before this change was.
        store.record_issued(an_invoice(invoice_number=f"{YEAR}-0001"))
        assert store.get_issued(f"{YEAR}-0001")["invoice"].tax_rate is None
        assert compute_totals(store.get_issued(f"{YEAR}-0001")["invoice"])[2] == 121.0

    @pytest.mark.parametrize("terms,days", [
        ("30 días", 30), ("15 dias", 15), ("Al contado", 0), ("", 30),
        ("60 días fecha factura", 60),
    ])
    def test_the_due_date_follows_the_payment_terms(self, offline, terms, days):
        company = accounts.create_company("Talleres Mario S.L.")
        accounts.update_company(company, payment_terms=terms or None)
        reload_config()
        result = finalize.issue(an_invoice())
        due = store.get_issued(result.number)["due_date"]
        assert due == (date.today() + timedelta(days=days)).isoformat()


# ── In the chat ──────────────────────────────────────────────────────────────

class TestInTheChat:
    @pytest.fixture(autouse=True)
    def owner(self, monkeypatch):
        from src import bot, conversation
        from test_telegram_access import OWNER_CHAT

        monkeypatch.setenv("TELEGRAM_CHAT_ID", str(OWNER_CHAT))
        monkeypatch.setattr(bot, "parse_invoice_from_transcript", lambda text: an_invoice())
        conversation._sessions.clear()
        return OWNER_CHAT

    def test_a_failed_email_is_said_with_a_retry_button(self, gmail_down, owner):
        from src import bot
        from test_telegram_access import press, say

        chat = say(bot.handle_text, owner, text="Factura para Talleres Puig")
        press(owner, "approve", chat)

        assert "NO se ha podido enviar" in chat.text
        assert "Enviada a" not in chat.text
        assert any(b.startswith("resend:") for b in chat.buttons())

    def test_the_retry_button_sends_it(self, gmail_down, owner, monkeypatch):
        from src import bot
        from test_telegram_access import press, say

        chat = say(bot.handle_text, owner, text="Factura para Talleres Puig")
        press(owner, "approve", chat)
        number = store.list_issued()[0]["invoice"].invoice_number
        monkeypatch.setattr(finalize, "send_invoice_email", lambda inv, path: None)

        retry = press(owner, f"resend:{number}")

        assert "enviada a taller@puig.es" in retry.text
        assert store.get_issued(number)["email_error"] is None

    def test_factura_sends_the_pdf(self, offline, owner):
        from src import bot
        from test_telegram_access import say

        number = finalize.issue(an_invoice()).number
        chat = say(bot.cmd_factura, owner, number)
        assert chat.documents == [(f"Factura_{number}.pdf", None)]

    def test_factura_alone_lists_the_latest(self, offline, owner):
        from src import bot
        from test_telegram_access import say

        number = finalize.issue(an_invoice()).number
        assert number in say(bot.cmd_factura, owner).text

    def test_anular_takes_a_reason(self, offline, owner):
        from src import bot
        from test_telegram_access import say

        number = finalize.issue(an_invoice()).number
        say(bot.cmd_anular, owner, number, "precio", "equivocado")
        credit = store.get_issued(f"R-{YEAR}-0001")["invoice"]
        assert "precio equivocado" in credit.notes


# ── Drafts do not look like invoices ─────────────────────────────────────────

def test_a_draft_pdf_says_borrador_not_none(tmp_path):
    pymupdf = pytest.importorskip("pymupdf")
    from src.invoice_generator import generate_invoice_pdf

    out = tmp_path / "draft.pdf"
    generate_invoice_pdf(an_invoice(), str(out))
    text = "".join(page.get_text() for page in pymupdf.open(str(out)))
    assert "BORRADOR" in text
    assert "None" not in text
