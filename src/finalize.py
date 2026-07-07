"""Shared invoice finalization used by both the bot (auto mode) and the web app (approval).

Assigns a gap-free invoice number (if the invoice does not already have a real one),
generates the final PDF, logs it to Google Sheets, emails the client (when an email is
known), and records the invoice in the issued store so it can be rectified later.
"""

from src import store
from src.email_sender import send_invoice_email
from src.invoice_generator import generate_invoice_pdf
from src.invoice_number import get_next_invoice_number
from src.models import InvoiceData
from src.sheets import add_invoice_to_sheet

_DRAFT_MARKERS = {None, "", "BORRADOR"}


def finalize_invoice(invoice: InvoiceData) -> str:
    """Finalize and (if possible) send an invoice. Returns the path to the generated PDF."""
    if invoice.invoice_number in _DRAFT_MARKERS:
        invoice.invoice_number = get_next_invoice_number()

    pdf_path = f"data/invoices/Factura_{invoice.invoice_number}.pdf"
    generate_invoice_pdf(invoice, pdf_path)

    add_invoice_to_sheet(invoice)

    if invoice.client_email:
        send_invoice_email(invoice, pdf_path)

    store.record_issued(invoice)
    return pdf_path
