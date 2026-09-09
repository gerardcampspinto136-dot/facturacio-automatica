"""Shared invoice finalization used by both the bot (auto mode) and the web app (approval).

Assigns a gap-free invoice number (if the invoice does not already have a real one),
generates the final PDF, logs it to Google Sheets, emails the client (when an email is
known), records the invoice as issued, and moves stock for any line that matches a
catalog product.

The number is assigned first and the invoice is recorded immediately, so a failure in
Sheets or Gmail leaves a correctly numbered, recorded invoice that can be re-sent --
rather than a consumed number with nothing behind it.
"""

import logging
from typing import Optional

from src import catalog, store
from src.email_sender import send_invoice_email
from src.invoice_generator import generate_invoice_pdf
from src.invoice_number import get_next_invoice_number
from src.models import InvoiceData
from src.sheets import add_invoice_to_sheet

logger = logging.getLogger(__name__)

_DRAFT_MARKERS = {None, "", "BORRADOR"}


def finalize_invoice(invoice: InvoiceData, token: Optional[str] = None) -> str:
    """Finalize and (if possible) send an invoice. Returns the path to the generated PDF.

    `token` is the pending-review token when this invoice is being approved from the web
    page; passing it promotes that draft row in place instead of writing a second one.
    """
    if invoice.invoice_number in _DRAFT_MARKERS:
        invoice.invoice_number = get_next_invoice_number()

    if token:
        store.approve_pending(token, invoice.invoice_number)
    store.record_issued(invoice)

    pdf_path = f"data/invoices/Factura_{invoice.invoice_number}.pdf"
    generate_invoice_pdf(invoice, pdf_path)

    try:
        add_invoice_to_sheet(invoice)
    except Exception:
        logger.exception("Could not log invoice %s to Sheets", invoice.invoice_number)

    if invoice.client_email:
        try:
            send_invoice_email(invoice, pdf_path)
        except Exception:
            logger.exception("Could not email invoice %s", invoice.invoice_number)

    for product in catalog.apply_invoice(invoice):
        logger.warning(
            "Low stock after %s: %s at %s %s (reorder point %s)",
            invoice.invoice_number, product["name"],
            product["stock_qty"], product["unit"], product["reorder_point"],
        )

    return pdf_path
