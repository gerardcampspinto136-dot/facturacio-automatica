"""Issuing an invoice: shared by the bot, the web panel and the scheduler.

Two phases, and the order is the whole point:

1. **The record.** In ONE database transaction: consume the next number in the
   company's series, write the invoice under it, and freeze it. Either all of it
   happens or none of it does, so the series can never get a gap -- not from a crash,
   not from two people approving the same draft at once, not from anything below.

2. **The delivery.** The PDF, the Google Sheets log, the email to the client and the
   stock movement. Each can fail on its own (Gmail down, no internet) without undoing
   the invoice, which already legally exists. Each failure is *reported*, not
   swallowed: the bot used to say "Enviada" whether or not Gmail had accepted the
   email. The email's fate is written on the invoice so it can be retried later.
"""

import logging
import os
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Optional

from src import catalog, db, store
from src.config_loader import get_config
from src.email_sender import send_invoice_email
from src.invoice_generator import generate_invoice_pdf
from src.invoice_number import next_number
from src.models import InvoiceData
from src.sheets import add_invoice_to_sheet
from src.totals import normalize_prices

logger = logging.getLogger(__name__)

_DRAFT_MARKERS = {None, "", "BORRADOR"}

# Where invoice PDFs live. Module-level so the tests can point them somewhere else.
INVOICES_DIR = Path("data/invoices")
DRAFTS_DIR = INVOICES_DIR / "borradores"


def draft_path(token: str) -> str:
    return str(DRAFTS_DIR / f"Borrador_{token}.pdf")


def invoice_path(number: str) -> str:
    return str(INVOICES_DIR / f"Factura_{number}.pdf")


@dataclass
class IssueResult:
    """What happened when an invoice was issued -- all of it, including what failed."""

    invoice: InvoiceData
    pdf_path: str
    emailed: bool = False
    email_error: Optional[str] = None
    logged: bool = False
    stock_movements: list = field(default_factory=list)

    @property
    def number(self) -> str:
        return self.invoice.invoice_number

    @property
    def email_status(self) -> str:
        """One line for the chat: sent, not sent and why, or no address to send to."""
        if self.emailed:
            return f"📧 Enviada a {self.invoice.client_email}"
        if not self.invoice.client_email:
            return "⚠️ No enviada: falta el email del cliente"
        return (f"⚠️ NO se ha podido enviar el email ({self.email_error}). La factura "
                f"está emitida y guardada; reenvíala con /reenviar {self.number}")


def prepare(invoice: InvoiceData, config=None) -> InvoiceData:
    """Settle everything about an invoice that must not change once it is issued.

    Net prices, and the VAT and IRPF rates -- taken from the company default only if
    the invoice has none of its own, and then kept on the invoice for good.
    """
    config = config or get_config()
    normalize_prices(invoice, config)
    if invoice.tax_rate is None:
        invoice.tax_rate = float(config.tax_rate)
    if invoice.irpf_rate is None:
        invoice.irpf_rate = float(getattr(config, "irpf_rate", 0) or 0)
    return invoice


def record(invoice: InvoiceData, token: Optional[str] = None,
           series: Optional[str] = None) -> str:
    """Phase 1: number and write the invoice in one transaction. Returns the number.

    `token` promotes that waiting draft instead of writing a new row; if it is no
    longer pending (approved by someone else a moment ago) KeyError is raised and
    nothing at all is written -- the number included.
    """
    config = get_config()
    prepare(invoice, config)
    series = config.invoice_series if series is None else series

    if token:
        # A draft approved days after it was dictated is issued TODAY: the date on an
        # invoice is its issue date, numbers in a series must not go back in time, and
        # Verifactu registers the issue date. The day the work was done, if different,
        # is stated as the date of the operation, as the invoicing rules ask.
        today = date.today()
        worked = invoice.date or today
        if worked != today:
            mention = f"Fecha de la operación: {worked.strftime('%d/%m/%Y')}."
            if mention not in (invoice.notes or ""):
                invoice.notes = f"{invoice.notes} {mention}" if invoice.notes else mention
        invoice.date = today

    with db.transaction() as conn:
        if invoice.invoice_number in _DRAFT_MARKERS:
            number = next_number(conn, series)
        else:
            number = invoice.invoice_number
        store.write_issued(conn, invoice, number, token=token,
                           due_days=config.payment_days)
        _register(conn, number)
        if invoice.quote_number:
            # The quote it came from is now invoiced -- in the same breath.
            conn.execute("UPDATE quotes SET status = 'invoiced', invoice_number = ? "
                         "WHERE number = ?", (number, invoice.quote_number))
    invoice.invoice_number = number
    return number


def _register(conn, number: str) -> None:
    """The Verifactu record, written in the same transaction as the invoice itself."""
    from src import verifactu

    verifactu.register_issued(conn, number)


def email(invoice: InvoiceData, pdf_path: str) -> tuple[bool, Optional[str]]:
    """Send the invoice to the client, recording the outcome on it. (sent, error)"""
    if not invoice.client_email:
        return False, None
    try:
        send_invoice_email(invoice, pdf_path)
    except Exception as exc:
        logger.exception("Could not email invoice %s", invoice.invoice_number)
        error = str(exc).strip().splitlines()[0][:200] if str(exc).strip() else \
            type(exc).__name__
        store.record_delivery(invoice.invoice_number, error=error)
        return False, error
    store.record_delivery(invoice.invoice_number)
    return True, None


def deliver(invoice: InvoiceData, pdf_path: str, move_stock: bool = True) -> IssueResult:
    """Phase 2: everything that happens to an invoice once it exists."""
    result = IssueResult(invoice=invoice, pdf_path=pdf_path)
    generate_invoice_pdf(invoice, pdf_path)

    try:
        add_invoice_to_sheet(invoice)
        result.logged = True
    except Exception:
        logger.exception("Could not log invoice %s to Sheets", invoice.invoice_number)

    result.emailed, result.email_error = email(invoice, pdf_path)

    if move_stock:
        result.stock_movements = catalog.apply_invoice(invoice)
        invoice.stock_movements = result.stock_movements
        for movement in result.stock_movements:
            logger.info(
                "Stock after %s: %s %s%g -> %g %s",
                invoice.invoice_number, movement["name"],
                "+" if movement["delta"] > 0 else "", movement["delta"],
                movement["balance"], movement["unit"],
            )
    return result


def issue(invoice: InvoiceData, token: Optional[str] = None) -> IssueResult:
    """Issue an invoice: record it, then deliver it. See the module docstring."""
    record(invoice, token=token)
    result = deliver(invoice, invoice_path(invoice.invoice_number))
    if token:
        _forget_draft(token)
    return result


def finalize_invoice(invoice: InvoiceData, token: Optional[str] = None) -> str:
    """Issue an invoice and return the path to its PDF (the older interface)."""
    return issue(invoice, token=token).pdf_path


def _forget_draft(token: str) -> None:
    """The draft PDF of an approved invoice is superseded by the real one."""
    path = draft_path(token)
    if os.path.exists(path):
        try:
            os.unlink(path)
        except OSError:
            pass


# ── After the fact ───────────────────────────────────────────────────────────

def pdf_for(number: str) -> Optional[str]:
    """The PDF of an issued invoice, rebuilt from the record if the file has gone."""
    record_ = store.get_issued(number)
    if record_ is None:
        return None
    path = invoice_path(number)
    if not os.path.exists(path):
        generate_invoice_pdf(record_["invoice"], path)
    return path


def resend(number: str) -> tuple[bool, Optional[str]]:
    """Email an issued invoice to its client again. Returns (sent, error)."""
    record_ = store.get_issued(number)
    if record_ is None:
        return False, f"No encuentro la factura {number}."
    invoice = record_["invoice"]
    if not invoice.client_email:
        return False, "La factura no tiene email de cliente."
    return email(invoice, pdf_for(number))
