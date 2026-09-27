"""Invoices that repeat: the maintenance contract, the rent, the monthly retainer.

Typing the same invoice every month is exactly the kind of work this software exists to
remove. A recurring invoice is a template made from one already issued -- one tap on
"Repetir cada mes" under it -- and on each due date it becomes a real invoice:

  - by default it is **prepared for approval**: the draft reaches whoever can approve it,
    on their phone, with an Aprobar button. One tap and it goes out.
  - with `auto_send`, it is issued and emailed on its own, and the owner is told.

The invoice is dated the day it is made (an invoice cannot be backdated), and its notes
say which period it covers. A computer that was off on the due date catches up on the
next run, one period at a time.
"""

import calendar
import json
import logging
from datetime import date, datetime
from typing import Optional

from src import db
from src.models import InvoiceData, InvoiceItem

logger = logging.getLogger(__name__)

MONTHLY, QUARTERLY, YEARLY = "monthly", "quarterly", "yearly"
FREQUENCIES = {MONTHLY: ("cada mes", 1), QUARTERLY: ("cada trimestre", 3),
               YEARLY: ("cada año", 12)}
MONTHS = ("enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto",
          "septiembre", "octubre", "noviembre", "diciembre")


def add_months(day: date, months: int, day_of_month: int) -> date:
    """The same day `months` later, clamped to the month's end (31 Jan -> 28 Feb)."""
    total = day.year * 12 + day.month - 1 + months
    year, month = divmod(total, 12)
    month += 1
    last = calendar.monthrange(year, month)[1]
    return date(year, month, min(day_of_month, last))


def next_after(day: date, frequency: str, day_of_month: int) -> date:
    return add_months(day, FREQUENCIES[frequency][1], day_of_month)


def period_label(day: date, frequency: str) -> str:
    """What the invoice made on `day` covers: "octubre 2026", "4T 2026", "2027"."""
    if frequency == MONTHLY:
        return f"{MONTHS[day.month - 1]} {day.year}"
    if frequency == QUARTERLY:
        return f"{(day.month - 1) // 3 + 1}T {day.year}"
    return str(day.year)


# ── Templates ────────────────────────────────────────────────────────────────

def _items_json(invoice: InvoiceData) -> str:
    return json.dumps([
        {"description": i.description, "quantity": i.quantity,
         "unit_price": i.unit_price, "total": i.total}
        for i in invoice.items
    ], ensure_ascii=False)


def create_from_invoice(invoice: InvoiceData, frequency: str = MONTHLY, *,
                        auto_send: bool = False, created_by: Optional[int] = None,
                        created_chat_id: Optional[int] = None) -> int:
    """Make a template from an issued invoice. The first repetition is one period on."""
    if frequency not in FREQUENCIES:
        raise ValueError(f"Frecuencia desconocida: {frequency}")
    start = invoice.date or date.today()
    with db.transaction() as conn:
        cur = conn.execute(
            "INSERT INTO recurring_invoices (contact_id, client_name, client_email, "
            "client_address, client_id, items_json, notes, tax_rate, irpf_rate, frequency, "
            "day_of_month, next_date, auto_send, created_by, created_chat_id, "
            "source_number) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (invoice.contact_id, invoice.client_name, invoice.client_email,
             invoice.client_address, invoice.client_id, _items_json(invoice),
             None, invoice.tax_rate, invoice.irpf_rate, frequency, start.day,
             next_after(start, frequency, start.day).isoformat(), int(auto_send),
             created_by, created_chat_id, invoice.invoice_number),
        )
        return cur.lastrowid


def get(template_id: int) -> Optional[dict]:
    row = db.connect().execute(
        "SELECT * FROM recurring_invoices WHERE id = ?", (template_id,)
    ).fetchone()
    return dict(row) if row else None


def list_active() -> list[dict]:
    rows = db.connect().execute(
        "SELECT * FROM recurring_invoices WHERE active = 1 ORDER BY next_date, id"
    ).fetchall()
    return [dict(r) for r in rows]


def cancel(template_id: int) -> None:
    with db.transaction() as conn:
        conn.execute("UPDATE recurring_invoices SET active = 0 WHERE id = ?", (template_id,))


def set_auto_send(template_id: int, auto: bool) -> None:
    with db.transaction() as conn:
        conn.execute("UPDATE recurring_invoices SET auto_send = ? WHERE id = ?",
                     (int(auto), template_id))


def invoice_for(template: dict, today: date) -> InvoiceData:
    """The invoice a template produces today."""
    items = [InvoiceItem(i["description"], i["quantity"], i["unit_price"], i["total"])
             for i in json.loads(template["items_json"])]
    period = period_label(date.fromisoformat(template["next_date"]), template["frequency"])
    notes = f"Periodo: {period}."
    if template.get("notes"):
        notes = f"{template['notes']} {notes}"
    return InvoiceData(
        client_name=template["client_name"],
        client_email=template["client_email"] or "",
        client_address=template["client_address"],
        client_id=template["client_id"],
        items=items,
        date=today,
        notes=notes,
        contact_id=template["contact_id"],
        tax_rate=template["tax_rate"],
        irpf_rate=template["irpf_rate"],
        prices_include_tax=False,
        prices_normalized=True,
    )


def describe(template: dict) -> str:
    from src.config_loader import get_config
    from src.totals import compute_totals, format_money

    config = get_config()
    total = compute_totals(invoice_for(template, date.today()), config)[2]
    when = FREQUENCIES[template["frequency"]][0]
    how = "se envía sola" if template["auto_send"] else "te la preparo para aprobar"
    next_day = date.fromisoformat(template["next_date"]).strftime("%d/%m/%Y")
    return (f"{template['client_name']} — {format_money(total, config)} {when} "
            f"(próxima: {next_day}; {how})")


# ── The daily run ────────────────────────────────────────────────────────────

def _advance(conn, template: dict, number: Optional[str], today: date) -> None:
    following = next_after(date.fromisoformat(template["next_date"]),
                           template["frequency"], template["day_of_month"])
    conn.execute(
        "UPDATE recurring_invoices SET next_date = ?, last_run_at = ?, "
        "last_number = COALESCE(?, last_number) WHERE id = ?",
        (following.isoformat(), datetime.now().isoformat(timespec="seconds"), number,
         template["id"]),
    )


def run_due(today: Optional[date] = None) -> list[str]:
    """Turn every template whose date has come into an invoice. Returns what was done.

    One period per template per run: a computer that was off for two months catches
    up one invoice a day, each one asked about, rather than dumping a pile at once.
    """
    from src import finalize, notify, store, telegram_access, telegram_api
    from src.config_loader import get_config
    from src.invoice_generator import generate_invoice_pdf
    from src.totals import compute_totals, format_money

    today = today or date.today()
    done = []
    rows = db.connect().execute(
        "SELECT * FROM recurring_invoices WHERE active = 1 AND next_date <= ? "
        "ORDER BY next_date, id", (today.isoformat(),),
    ).fetchall()

    for template in (dict(r) for r in rows):
        invoice = invoice_for(template, today)
        config = get_config()
        try:
            if template["auto_send"]:
                # Record, then move the template on at once, and only then deliver:
                # if the PDF or the email fails, the invoice exists and the template
                # has moved, so tomorrow does not issue the same invoice twice.
                number = finalize.record(invoice)
                with db.transaction() as conn:
                    _advance(conn, template, number, today)
                result = finalize.deliver(invoice, finalize.invoice_path(number))
                total = format_money(compute_totals(invoice, config)[2], config)
                text = (f"🔁 Factura recurrente emitida: {result.number}, "
                        f"{invoice.client_name}, {total}.\n{result.email_status}")
                for chat in telegram_access.notify_chats("invoices.approve"):
                    telegram_api.send_message(chat, text)
                done.append(result.number)
            else:
                token = store.new_token()
                draft = finalize.draft_path(token)
                generate_invoice_pdf(invoice, draft)
                store.add_pending(invoice, draft, token=token,
                                  created_by=template["created_by"],
                                  created_by_name="Factura recurrente",
                                  created_chat_id=None)
                with db.transaction() as conn:
                    _advance(conn, template, None, today)
                notify.request_approval(
                    token, intro=(f"🔁 Toca la factura recurrente de {invoice.client_name}"
                                  f" ({period_label(date.fromisoformat(template['next_date']), template['frequency'])})."))
                done.append(token)
        except Exception:
            logger.exception("Recurring invoice %s failed", template["id"])
    return done
