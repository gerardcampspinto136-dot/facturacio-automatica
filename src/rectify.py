"""Contra / rectifying invoices (factura rectificativa).

Cancels a previously issued invoice by issuing a new invoice in the "R" series with the
same line items negated, referencing the original. It is logged to Sheets and emailed to
the client just like a normal invoice.

The R number, the rectifying invoice and the "cancelled by" mark on the original are
written in one transaction. The old code consumed the number first and only recorded the
invoice after the PDF, Sheets and Gmail had all succeeded -- so Gmail being down burned
an R number with nothing behind it (a gap Hacienda does not allow), left the original
looking uncancelled, and the retry burned another.
"""

from typing import Optional

from src import db, store
from src.models import InvoiceData, InvoiceItem

DEFAULT_REASON = "Anulación de la factura"


def create_rectifying_invoice(original_number: str,
                              reason: Optional[str] = None) -> tuple[InvoiceData, str]:
    """Issue a full-cancellation rectifying invoice. Returns (invoice, pdf_path)."""
    result = rectify(original_number, reason)
    return result.invoice, result.pdf_path


def rectify(original_number: str, reason: Optional[str] = None):
    """Issue a full-cancellation rectifying invoice for ``original_number``.

    Returns the finalize.IssueResult, email outcome included. Raises ValueError if the
    original does not exist, is itself a rectifying invoice, or is already cancelled.
    """
    from src import finalize
    from src.invoice_number import next_number

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
    if original.rectifies:
        raise ValueError(
            f"{original_number} ya es una rectificativa: no se anula una anulación."
        )

    neg_items = [
        InvoiceItem(
            description=item.description,
            quantity=item.quantity,
            unit_price=-item.unit_price,
            total=-item.total,
        )
        for item in original.items
    ]

    reason = (reason or "").strip() or DEFAULT_REASON
    rectifying = InvoiceData(
        client_name=original.client_name,
        client_email=original.client_email,
        items=neg_items,
        client_address=original.client_address,
        client_id=original.client_id,
        notes=(f"Factura rectificativa que anula la factura {original_number} de fecha "
               f"{original.date.strftime('%d/%m/%Y')}. Motivo: {reason}."),
        rectifies=original_number,
        prices_include_tax=False,
        prices_normalized=True,
        contact_id=original.contact_id,
        # Exactly the rates the original was issued at, so it cancels to the cent.
        tax_rate=original.tax_rate,
        irpf_rate=original.irpf_rate,
        vat_reason=original.vat_reason,
    )
    finalize.prepare(rectifying)

    with db.transaction() as conn:
        # Checked again inside the lock: two people pressing "Anular" at once must not
        # produce two rectifying invoices for one original.
        row = conn.execute(
            "SELECT rectified_by FROM invoices WHERE number = ? AND status = 'issued'",
            (original_number,),
        ).fetchone()
        if row is None or row["rectified_by"]:
            raise ValueError(f"La factura {original_number} ya está anulada.")
        number = next_number(conn, "R")
        store.write_issued(conn, rectifying, number, due_days=0)
        conn.execute("UPDATE invoices SET rectified_by = ? WHERE number = ?",
                     (number, original_number))
        finalize._register(conn, number)
    rectifying.invoice_number = number

    result = finalize.deliver(rectifying, finalize.invoice_path(number), move_stock=False)

    # Cancelling a sale returns whatever it took out of stock.
    from src import catalog

    result.stock_movements = catalog.apply_invoice(original, ref=number, sign=1)
    return result
