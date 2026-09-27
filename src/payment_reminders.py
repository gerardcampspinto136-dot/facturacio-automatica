"""Chasing unpaid invoices: a polite email to the client, without anyone writing it.

For a small business the invoice that is never paid is a smaller problem than the ten
that are paid late, because each needs someone to notice, find the PDF, write the email
and remember to try again next week. That is the job done here.

Each morning the overdue invoices are checked. What happens then is the company's choice
(`collections.mode`), because this writes to the company's customers:

  ask   the owner is asked in Telegram, invoice by invoice, with three buttons:
        send the reminder / it has been paid / stop chasing this one. (Default.)
  auto  the reminders go out on their own, and the owner is told what was sent.
  off   nothing.

A client is reminded at most `max_reminders` times, `repeat_every_days` apart, starting
`first_after_days` after the due date. The invoice PDF goes with each reminder.
"""

import logging
from datetime import date, datetime
from typing import Optional

from src import db, store
from src.config_loader import get_config

logger = logging.getLogger(__name__)

DEFAULT_BODY = """Estimado/a {client_name}:

Según nuestros registros, la factura {invoice_number} del {invoice_date}, por importe de {total}, venció el {due_date} y todavía no nos consta su pago.

Le adjuntamos una copia. Puede abonarla por transferencia a la cuenta {iban}, indicando el número de factura como referencia.

Si ya la ha pagado, le rogamos que disculpe este mensaje.

Un cordial saludo,
{company_name}
{company_phone}
{company_email}"""


def _days_since(stamp: Optional[str], today: date) -> Optional[int]:
    if not stamp:
        return None
    return (today - datetime.fromisoformat(stamp).date()).days


def due(today: Optional[date] = None, for_prompt: bool = False) -> list[dict]:
    """Overdue invoices that should be chased today, most overdue first."""
    config = get_config()
    today = today or date.today()
    out = []
    for record in store.list_unpaid(as_of=today, overdue_only=True):
        invoice = record["invoice"]
        if not invoice.client_email or record["reminders_paused"]:
            continue
        if record["days_overdue"] < config.collections_first_after:
            continue
        if record["reminder_count"] >= config.collections_max:
            continue
        since_last = _days_since(record["last_reminder_at"], today)
        if since_last is not None and since_last < config.collections_every:
            continue
        if for_prompt:
            # Asked once per round: the owner is not asked again every morning about
            # an invoice they have not answered for yet.
            since_asked = _days_since(record["reminder_prompted_at"], today)
            if since_asked is not None and since_asked < config.collections_every:
                continue
        out.append(record)
    return sorted(out, key=lambda r: -r["days_overdue"])


def _fields(record: dict, today: date) -> dict:
    from src.totals import compute_totals, format_money

    config = get_config()
    invoice = record["invoice"]
    due_date = date.fromisoformat(record["due_date"]) if record["due_date"] else None
    return {
        "client_name": invoice.client_name,
        "invoice_number": invoice.invoice_number,
        "invoice_date": invoice.date.strftime("%d/%m/%Y"),
        "due_date": due_date.strftime("%d/%m/%Y") if due_date else "",
        "days_overdue": (today - due_date).days if due_date else 0,
        "total": format_money(compute_totals(invoice, config)[2], config),
        "iban": config.bank_account or "",
        "company_name": config.name,
        "company_phone": config.phone or "",
        "company_email": config.email or "",
    }


def compose(record: dict, today: Optional[date] = None) -> tuple[str, str]:
    """(subject, body) of the reminder for this invoice."""
    config = get_config()
    fields = _fields(record, today or date.today())
    template = config.collections_body or DEFAULT_BODY
    try:
        body = template.format(**fields)
        subject = config.collections_subject.format(**fields)
    except (KeyError, IndexError, ValueError):
        # A placeholder mistyped in company.yaml must not stop the reminder.
        logger.warning("The reminder template has an unknown placeholder; using the default")
        body = DEFAULT_BODY.format(**fields)
        subject = f"Recordatorio de pago — factura {fields['invoice_number']}"
    return subject, body


def send(number: str, today: Optional[date] = None) -> tuple[bool, str]:
    """Email the reminder for one invoice now. Returns (sent, what happened)."""
    from src import finalize
    from src.email_sender import send_email

    today = today or date.today()
    record = store.get_issued(number)
    if record is None:
        return False, f"No encuentro la factura {number}."
    if record["paid_at"]:
        return False, f"La factura {number} ya está cobrada."
    if record.get("rectified_by"):
        return False, f"La factura {number} está anulada."
    invoice = record["invoice"]
    if not invoice.client_email:
        return False, f"La factura {number} no tiene email de cliente."

    subject, body = compose(record, today)
    try:
        send_email(to=invoice.client_email, subject=subject, body=body,
                   pdf_path=finalize.pdf_for(number),
                   attachment_name=f"Factura_{number}.pdf")
    except Exception as exc:
        logger.exception("Could not send the reminder for %s", number)
        return False, f"No se ha podido enviar el recordatorio: {exc}"

    with db.transaction() as conn:
        conn.execute(
            "UPDATE invoices SET reminder_count = reminder_count + 1, "
            "last_reminder_at = ? WHERE number = ?",
            (datetime.now().isoformat(timespec="seconds"), number),
        )
    count = record["reminder_count"] + 1
    return True, (f"Recordatorio {count} enviado a {invoice.client_email} "
                  f"({invoice.client_name}, factura {number}).")


def pause(number: str) -> None:
    """Stop chasing this invoice: the owner is dealing with it another way."""
    with db.transaction() as conn:
        conn.execute("UPDATE invoices SET reminders_paused = 1 WHERE number = ?", (number,))


def resume(number: str) -> None:
    with db.transaction() as conn:
        conn.execute("UPDATE invoices SET reminders_paused = 0 WHERE number = ?", (number,))


def _mark_prompted(number: str) -> None:
    with db.transaction() as conn:
        conn.execute("UPDATE invoices SET reminder_prompted_at = ? WHERE number = ?",
                     (datetime.now().isoformat(timespec="seconds"), number))


def buttons(number: str) -> list:
    return [[("📧 Enviar recordatorio", f"dun:send:{number}"),
             ("✅ Ya está cobrada", f"dun:paid:{number}")],
            [("⏸ No insistir con esta", f"dun:stop:{number}")]]


def prompt_text(record: dict, today: date) -> str:
    from src.totals import compute_totals, format_money

    config = get_config()
    invoice = record["invoice"]
    total = format_money(compute_totals(invoice, config)[2], config)
    previous = record["reminder_count"]
    return (f"💸 {invoice.client_name} no ha pagado la factura {invoice.invoice_number} "
            f"({total}): {record['days_overdue']} día(s) de retraso."
            + (f" Ya se le ha recordado {previous} vez/veces." if previous else "")
            + f"\n\n¿Le mando un recordatorio a {invoice.client_email}, con la factura "
              "adjunta?")


def run_due(today: Optional[date] = None) -> list[str]:
    """The daily round. Returns the numbers it acted on (asked about or reminded)."""
    from src import telegram_access, telegram_api

    config = get_config()
    today = today or date.today()
    if config.collections_mode == "off":
        return []

    chats = telegram_access.notify_chats("receivables.manage")
    acted = []
    if config.collections_mode == "auto":
        for record in due(today):
            number = record["invoice"].invoice_number
            sent, message = send(number, today)
            acted.append(number)
            for chat in chats:
                telegram_api.send_message(chat, ("📧 " if sent else "⚠️ ") + message)
        return acted

    # "ask": one question per invoice, each with its buttons.
    for record in due(today, for_prompt=True):
        number = record["invoice"].invoice_number
        told = 0
        for chat in chats:
            told += int(telegram_api.send_message(chat, prompt_text(record, today),
                                                  buttons(number)))
        if told:
            _mark_prompted(number)
            acted.append(number)
    return acted
