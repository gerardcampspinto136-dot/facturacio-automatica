"""Reviewer notifications via Telegram and/or email.

Two kinds go out: the pending-review reminder, and a money digest covering bills falling
due and invoices the client has not paid. The digest is deliberately one message rather
than three separate alerts -- three notifications a day is how a reminder becomes noise
that gets muted, and a muted reminder protects nothing.
"""

import logging
import os
from datetime import date
from typing import Optional

from src.config_loader import get_config

logger = logging.getLogger(__name__)


def _dispatch(subject: str, text: str, permission: str) -> None:
    """Send one message on every channel the config asks for, surviving a failure on either.

    On Telegram it goes to the owner's chat and to every linked account that holds
    `permission` -- the people who can actually act on it.
    """
    from src import telegram_access, telegram_api

    config = get_config()
    channels = config.notify_channels or []

    if "telegram" in channels:
        chats = telegram_access.notify_chats(permission)
        if not chats:
            logger.warning("Telegram notification skipped: nobody to send it to")
        for chat in chats:
            telegram_api.send_message(chat, text)

    if "email" in channels and config.notify_email:
        try:
            from src.email_sender import send_email

            send_email(to=config.notify_email, subject=subject, body=text)
        except Exception:
            logger.error("Failed to send email notification", exc_info=True)


def send_pending_reminder(count: int, url: str) -> None:
    """Notify the reviewer(s) that ``count`` invoices are waiting, linking to ``url``."""
    _dispatch(
        f"{count} factura(s) pendiente(s) de revisión",
        f"Tienes {count} factura(s) pendiente(s) de revisión.\n"
        f"Revísalas y envíalas aquí:\n{url}\n\n"
        "O desde aquí mismo con /pendientes.",
        "invoices.approve",
    )


# ── Approval requests ────────────────────────────────────────────────────────

def request_approval(token: str, exclude_chat: Optional[int] = None,
                     intro: Optional[str] = None) -> int:
    """Put a waiting draft in front of everyone who may approve it, with the buttons.

    The approver gets the draft PDF itself, so approving is a look and a tap from the
    phone rather than a trip to the office computer. Returns how many chats were told.
    `intro` replaces the opening line ("Pepe ha preparado una factura...").
    """
    from src import store, telegram_access, telegram_api
    from src.totals import compute_totals, format_money

    pending = store.get_pending(token)
    if pending is None:
        return 0
    invoice = pending["invoice"]
    config = get_config()
    _, _, total = compute_totals(invoice, config)

    who = pending.get("created_by_name") or "Alguien del equipo"
    caption = (
        (intro or f"🧾 {who} ha preparado una factura y espera tu aprobación.") + "\n\n"
        f"Cliente: {invoice.client_name}\n"
        f"Total: {format_money(total, config)}\n"
        f"Se enviará a: {invoice.client_email or '(sin email: no se enviará)'}"
    )
    buttons = [[("✅ Aprobar y enviar", f"pend:ok:{token}"),
                ("❌ Descartar", f"pend:no:{token}")]]

    told = 0
    for chat in telegram_access.notify_chats("invoices.approve"):
        if exclude_chat is not None and chat == exclude_chat:
            continue
        draft = pending.get("draft_path")
        if draft and os.path.exists(draft):
            ok = telegram_api.send_document(chat, draft, caption, buttons,
                                            filename="Borrador_factura.pdf")
        else:
            ok = telegram_api.send_message(chat, caption, buttons)
        told += int(ok)
    return told


def tell_creator(pending: dict, text: str) -> None:
    """Let whoever prepared a draft know what happened to it."""
    from src import telegram_api

    chat = (pending or {}).get("created_chat_id")
    if chat:
        telegram_api.send_message(chat, text)


def _money(amount: float) -> str:
    symbol = get_config().currency_symbol
    return f"{amount:,.2f} {symbol}".replace(",", "@").replace(".", ",").replace("@", ".")


def build_money_digest(bills_due: list, unpaid_invoices: list,
                       as_of: Optional[date] = None) -> Optional[str]:
    """Compose the money digest, or None when there is nothing worth interrupting for."""
    if not bills_due and not unpaid_invoices:
        return None

    as_of = as_of or date.today()
    lines: list[str] = []

    if bills_due:
        owed = sum(b["total"] for b in bills_due)
        lines.append(f"💸 PAGOS A PROVEEDORES — {_money(owed)}")
        for bill in bills_due[:10]:
            days = bill["days_until_due"]
            if bill["overdue"]:
                when = f"VENCIDA hace {abs(days)} día(s)"
            elif days == 0:
                when = "vence HOY"
            else:
                when = f"vence en {days} día(s)"
            ref = f" ({bill['reference']})" if bill.get("reference") else ""
            lines.append(f"  • {bill['supplier_name']}{ref}: {_money(bill['total'])} — {when}")
        if len(bills_due) > 10:
            lines.append(f"  … y {len(bills_due) - 10} más")

    if unpaid_invoices:
        from src.totals import compute_totals

        def owed(record) -> float:
            # What the client actually has to transfer: VAT included, IRPF withheld.
            # This used to add up the net line totals, so a 121 € invoice was
            # reported as 100 € owed -- every receivable understated by the VAT.
            return compute_totals(record["invoice"])[2]

        if lines:
            lines.append("")
        overdue = [i for i in unpaid_invoices if i["days_overdue"] > 0]
        total = sum(owed(i) for i in unpaid_invoices)
        lines.append(f"📥 FACTURAS SIN COBRAR — {_money(total)}")
        if overdue:
            lines.append(f"  {len(overdue)} de ellas ya vencidas:")
            for inv in overdue[:10]:
                amount = owed(inv)
                lines.append(
                    f"  • {inv['invoice'].invoice_number} — {inv['invoice'].client_name}: "
                    f"{_money(amount)}, {inv['days_overdue']} día(s) de retraso"
                )
            if len(overdue) > 10:
                lines.append(f"  … y {len(overdue) - 10} más")
        else:
            lines.append("  Ninguna vencida todavía.")

    return "\n".join(lines)


def send_money_digest(bills_due: list, unpaid_invoices: list,
                      as_of: Optional[date] = None) -> bool:
    """Send the digest. Returns False when there was nothing to send."""
    body = build_money_digest(bills_due, unpaid_invoices, as_of)
    if body is None:
        return False
    _dispatch("Resumen de cobros y pagos", body, "receivables.manage")
    return True


def send_low_stock_alert(products: list) -> bool:
    """Warn that tracked products have reached their reorder point."""
    if not products:
        return False
    lines = ["📦 STOCK BAJO — hay que reponer:"]
    for p in products[:15]:
        lines.append(
            f"  • {p['name']}: quedan {p['stock_qty']:g} {p['unit']} "
            f"(punto de pedido {p['reorder_point']:g})"
        )
    if len(products) > 15:
        lines.append(f"  … y {len(products) - 15} más")
    _dispatch("Stock bajo", "\n".join(lines), "stock.manage")
    return True
