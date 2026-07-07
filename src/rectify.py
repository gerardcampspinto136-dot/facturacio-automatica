"""Contra / rectifying invoices (factura rectificativa).

Cancels a previously issued invoice by issuing a new invoice in the "R" series with the
same line items negated, referencing the original. It is logged to Sheets and emailed to
the client just like a normal invoice.
"""

from src import store
from src.email_sender import send_invoice_email
from src.invoice_generator import generate_invoice_pdf
from src.invoice_number import get_next_invoice_number
from src.models import InvoiceData, InvoiceItem


def create_rectifying_invoice(original_number: str) -> tuple[InvoiceData, str]:
    """Issue a full-cancellation rectifying invoice for ``original_number``.

    Returns (rectifying_invoice, pdf_path). Raises ValueError if the original does not
    exist or has already been rectified.
    """
    record = store.get_issued(original_number)
    if record is None:
        raise ValueError(
            f"No encuentro la factura {original_number} en el registro de facturas emitidas."
        )
    if record.get("rectified_by"):
        raise ValueError(
            f"La factura {original_number} ya fue rectificada por {record['rectified_by']}."
        )

    original: InvoiceData = record["invoice"]

    neg_items = [
        InvoiceItem(
            description=item.description,
            quantity=item.quantity,
            unit_price=-item.unit_price,
            total=-item.total,
        )
        for item in original.items
    ]

    rectifying = InvoiceData(
        client_name=original.client_name,
        client_email=original.client_email,
        items=neg_items,
        client_address=original.client_address,
        client_id=original.client_id,
        notes=f"Factura rectificativa que anula la factura {original_number}.",
        rectifies=original_number,
    )
    rectifying.invoice_number = get_next_invoice_number("R")

    pdf_path = f"data/invoices/Factura_{rectifying.invoice_number}.pdf"
    generate_invoice_pdf(rectifying, pdf_path)

    add_invoice_to_sheet_safe(rectifying)

    if rectifying.client_email:
        send_invoice_email(rectifying, pdf_path)

    store.record_issued(rectifying)
    store.mark_rectified(original_number, rectifying.invoice_number)

    return rectifying, pdf_path


def add_invoice_to_sheet_safe(invoice: InvoiceData) -> None:
    # Kept as a thin wrapper so the import stays local and testable.
    from src.sheets import add_invoice_to_sheet

    add_invoice_to_sheet(invoice)
