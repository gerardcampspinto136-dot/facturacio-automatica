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


def _telegram(chat_id, text: str) -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token or not chat_id:
        logger.warning("Telegram notification skipped (missing token or chat_id)")
        return
    import requests

    resp = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": text},
        timeout=15,
    )
    resp.raise_for_status()


def _dispatch(subject: str, text: str) -> None:
    """Send one message on every channel the config asks for, surviving a failure on either."""
    config = get_config()
    channels = config.notify_channels or []

    if "telegram" in channels:
        try:
            _telegram(config.notify_telegram_chat_id, text)
        except Exception:
            logger.error("Failed to send Telegram notification", exc_info=True)

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
        f"Revísalas y envíalas aquí:\n{url}",
    )


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
        if lines:
            lines.append("")
        overdue = [i for i in unpaid_invoices if i["days_overdue"] > 0]
        total = sum(sum(it.total for it in i["invoice"].items) for i in unpaid_invoices)
        lines.append(f"📥 FACTURAS SIN COBRAR — {_money(total)}")
        if overdue:
            lines.append(f"  {len(overdue)} de ellas ya vencidas:")
            for inv in overdue[:10]:
                amount = sum(it.total for it in inv["invoice"].items)
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
    _dispatch("Resumen de cobros y pagos", body)
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
    _dispatch("Stock bajo", "\n".join(lines))
    return True
