"""Quotes (presupuestos): priced, sent, and one tap from becoming the invoice.

A tradesman quotes before every job, and then types the same lines again into the
invoice when the job is done. Here the quote is dictated like an invoice ("presupuesto
para...") and, once the client says yes, "Aceptado → facturar" turns it into the invoice
-- through the normal review, so anything the quote did not need (the client's tax id)
is asked for then.

A quote is not an invoice: it has its own P- numbering, no Verifactu record, no
Sheets line, and it expires. It is stored so it can be found, converted and counted.
"""

import logging
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

from src import db
from src.config_loader import get_config
from src.models import InvoiceData, InvoiceItem

logger = logging.getLogger(__name__)

SERIES = "P"
QUOTES_DIR = Path("data/presupuestos")

SENT, ACCEPTED, REJECTED, INVOICED = "sent", "accepted", "rejected", "invoiced"
STATUS_LABELS = {SENT: "enviado", ACCEPTED: "aceptado", REJECTED: "rechazado",
                 INVOICED: "facturado"}

DEFAULT_BODY = """Estimado/a {client_name}:

Le adjuntamos el presupuesto {number}, por un importe total de {total}, válido hasta el {valid_until}.

Para aceptarlo basta con responder a este correo. Quedamos a su disposición para cualquier duda.

Un cordial saludo,
{company_name}
{company_phone}
{company_email}"""


@dataclass
class QuoteResult:
    number: str
    invoice: InvoiceData
    valid_until: date
    pdf_path: str
    emailed: bool = False
    email_error: Optional[str] = None

    @property
    def email_status(self) -> str:
        if self.emailed:
            return f"📧 Enviado a {self.invoice.client_email}"
        if not self.invoice.client_email:
            return "⚠️ No enviado: falta el email del cliente"
        return f"⚠️ NO se ha podido enviar el email ({self.email_error})"


def quote_path(number: str) -> str:
    return str(QUOTES_DIR / f"Presupuesto_{number}.pdf")


def validity_days() -> int:
    return int(getattr(get_config(), "quote_validity_days", 30) or 30)


# ── Issuing ──────────────────────────────────────────────────────────────────

def issue(invoice: InvoiceData, created_by: Optional[int] = None) -> QuoteResult:
    """Number, store, render and email a quote."""
    from src import finalize
    from src.invoice_number import next_number

    finalize.prepare(invoice)
    today = invoice.date or date.today()
    valid_until = today + timedelta(days=validity_days())

    with db.transaction() as conn:
        number = next_number(conn, SERIES)
        cur = conn.execute(
            "INSERT INTO quotes (number, contact_id, client_name, client_email, "
            "client_address, client_id, date, valid_until, notes, tax_rate, irpf_rate, "
            "created_by, vat_reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (number, invoice.contact_id, invoice.client_name, invoice.client_email,
             invoice.client_address, invoice.client_id, today.isoformat(),
             valid_until.isoformat(), invoice.notes, invoice.tax_rate, invoice.irpf_rate,
             created_by, invoice.vat_reason),
        )
        for position, item in enumerate(invoice.items):
            conn.execute(
                "INSERT INTO quote_items (quote_id, position, description, quantity, "
                "unit_price, total) VALUES (?, ?, ?, ?, ?, ?)",
                (cur.lastrowid, position, item.description, item.quantity,
                 item.unit_price, item.total),
            )
    invoice.invoice_number = number

    result = QuoteResult(number, invoice, valid_until, pdf_for(number))
    result.emailed, result.email_error = _email(number)
    return result


def _email(number: str) -> tuple[bool, Optional[str]]:
    from src.email_sender import send_email
    from src.totals import compute_totals, format_money

    quote = get(number)
    invoice = quote["invoice"]
    if not invoice.client_email:
        return False, None
    config = get_config()
    body = DEFAULT_BODY.format(
        client_name=invoice.client_name, number=number,
        total=format_money(compute_totals(invoice, config)[2], config),
        valid_until=quote["valid_until"].strftime("%d/%m/%Y"),
        company_name=config.name, company_phone=config.phone or "",
        company_email=config.email or "",
    )
    try:
        send_email(to=invoice.client_email,
                   subject=f"Presupuesto {number} - {config.name}", body=body,
                   pdf_path=pdf_for(number), attachment_name=f"Presupuesto_{number}.pdf")
    except Exception as exc:
        logger.exception("Could not email quote %s", number)
        error = str(exc).strip().splitlines()[0][:200] if str(exc).strip() else \
            type(exc).__name__
        _set(number, email_error=error)
        return False, error
    from datetime import datetime

    _set(number, email_sent_at=datetime.now().isoformat(timespec="seconds"),
         email_error=None)
    return True, None


def _set(number: str, **fields) -> None:
    allowed = {"status", "invoice_number", "email_sent_at", "email_error"}
    changes = {k: v for k, v in fields.items() if k in allowed}
    if not changes:
        return
    with db.transaction() as conn:
        conn.execute(
            f"UPDATE quotes SET {', '.join(f'{k} = ?' for k in changes)} WHERE number = ?",
            (*changes.values(), number),
        )


# ── Reading ──────────────────────────────────────────────────────────────────

def _payload(conn, row) -> dict:
    items = [InvoiceItem(r["description"], r["quantity"], r["unit_price"], r["total"])
             for r in conn.execute(
                 "SELECT * FROM quote_items WHERE quote_id = ? ORDER BY position, id",
                 (row["id"],)).fetchall()]
    invoice = InvoiceData(
        client_name=row["client_name"], client_email=row["client_email"] or "",
        items=items, client_address=row["client_address"], client_id=row["client_id"],
        invoice_number=row["number"], date=date.fromisoformat(row["date"]),
        notes=row["notes"], prices_include_tax=False, prices_normalized=True,
        contact_id=row["contact_id"], tax_rate=row["tax_rate"],
        irpf_rate=row["irpf_rate"], vat_reason=row["vat_reason"], document="quote",
    )
    valid_until = date.fromisoformat(row["valid_until"])
    status = row["status"]
    expired = status == SENT and valid_until < date.today()
    return {
        "number": row["number"], "status": status, "expired": expired,
        "valid_until": valid_until, "invoice_number": row["invoice_number"],
        "email_sent_at": row["email_sent_at"], "email_error": row["email_error"],
        "invoice": invoice,
    }


def get(number: str) -> Optional[dict]:
    conn = db.connect()
    row = conn.execute("SELECT * FROM quotes WHERE number = ?", (number,)).fetchone()
    return _payload(conn, row) if row else None


def list_quotes(open_only: bool = False, limit: int = 100) -> list[dict]:
    conn = db.connect()
    sql = "SELECT * FROM quotes"
    if open_only:
        sql += f" WHERE status IN ('{SENT}', '{ACCEPTED}')"
    sql += " ORDER BY date DESC, id DESC LIMIT ?"
    return [_payload(conn, r) for r in conn.execute(sql, (limit,)).fetchall()]


def status_label(quote: dict) -> str:
    return "caducado" if quote["expired"] else STATUS_LABELS[quote["status"]]


def pdf_for(number: str) -> Optional[str]:
    """The quote's PDF, built from the record if it is not on disk."""
    import os

    from src.invoice_generator import generate_invoice_pdf

    quote = get(number)
    if quote is None:
        return None
    path = quote_path(number)
    if not os.path.exists(path):
        generate_invoice_pdf(quote["invoice"], path, valid_until=quote["valid_until"])
    return path


# ── Its outcome ──────────────────────────────────────────────────────────────

def reject(number: str) -> None:
    _set(number, status=REJECTED)


def to_invoice(number: str) -> InvoiceData:
    """An invoice with the quote's lines, client and rates, ready for the usual review.

    The quote is marked accepted; it becomes "invoiced" when that invoice is issued.
    """
    quote = get(number)
    if quote is None:
        raise KeyError(f"Quote {number} not found")
    if quote["status"] == INVOICED:
        raise ValueError(f"El presupuesto {number} ya se facturó "
                         f"({quote['invoice_number']}).")
    source = quote["invoice"]
    _set(number, status=ACCEPTED)
    return InvoiceData(
        client_name=source.client_name, client_email=source.client_email,
        items=[InvoiceItem(i.description, i.quantity, i.unit_price, i.total)
               for i in source.items],
        client_address=source.client_address, client_id=source.client_id,
        notes=f"Según presupuesto {number}.", prices_include_tax=False,
        prices_normalized=True, contact_id=source.contact_id,
        tax_rate=source.tax_rate, irpf_rate=source.irpf_rate, quote_number=number,
        vat_reason=source.vat_reason,
    )


def mark_invoiced(number: str, invoice_number: str) -> None:
    _set(number, status=INVOICED, invoice_number=invoice_number)
