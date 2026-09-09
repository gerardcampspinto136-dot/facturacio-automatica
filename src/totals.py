"""VAT arithmetic -- the single place that decides what an invoice adds up to.

Three modules used to compute totals independently (the bot, the web app and the email
sender), which is how a caption can disagree with the PDF it is attached to. They all
call in here now.

A price can be dictated either way round:

  "ciento cincuenta euros más IVA"      -> 150 is the base, VAT is added on top  -> 181.50
  "ciento cincuenta euros IVA incluido" -> 150 is the total, VAT is inside it    -> 123.97 + 26.03

Which one applies is decided per invoice: what the speaker actually said, if they said
anything, else the company default in `invoice.prices_include_tax`. Whichever it is, line
items are normalised to net prices as soon as they are captured, so everything downstream
-- the PDF, Sheets, the email, the stored record -- works on the same convention and a
line total never disagrees with the subtotal above it.
"""

from typing import Optional

from src.models import InvoiceData


def net_from_gross(gross: float, tax_rate: float) -> float:
    """Strip VAT out of a VAT-inclusive amount."""
    return round(gross / (1 + tax_rate / 100), 2)


def compute_totals(invoice: InvoiceData, config=None) -> tuple[float, float, float]:
    """Return (subtotal, tax_amount, total) for an invoice holding net line prices."""
    if config is None:
        from src.config_loader import get_config

        config = get_config()
    subtotal = invoice.subtotal
    tax_amount = round(subtotal * config.tax_rate / 100, 2)
    return subtotal, tax_amount, round(subtotal + tax_amount, 2)


def prices_are_inclusive(invoice: InvoiceData, config=None) -> bool:
    """Did the amounts on this invoice arrive with VAT already inside them?

    What the speaker said wins; the company default only fills the silence.
    """
    if config is None:
        from src.config_loader import get_config

        config = get_config()
    if invoice.prices_include_tax is not None:
        return bool(invoice.prices_include_tax)
    return bool(config.prices_include_tax)


def normalize_prices(invoice: InvoiceData, config=None) -> InvoiceData:
    """Convert VAT-inclusive line prices to net, in place. Safe to call twice.

    Each line is converted on its own and then the last line absorbs any rounding drift,
    so the invoice still totals exactly the gross figure the customer was quoted. Without
    that, three lines of "60 € IVA incluido" can add back up to 179.99.
    """
    if config is None:
        from src.config_loader import get_config

        config = get_config()

    if not invoice.items or invoice.prices_normalized:
        invoice.prices_normalized = True
        return invoice

    inclusive = prices_are_inclusive(invoice, config)
    invoice.prices_include_tax = inclusive
    invoice.prices_normalized = True

    if not inclusive:
        return invoice

    rate = config.tax_rate
    gross_total = round(sum(item.total for item in invoice.items), 2)

    for item in invoice.items:
        item.total = net_from_gross(item.total, rate)
        item.unit_price = (
            round(item.total / item.quantity, 2) if item.quantity else item.total
        )

    # Push the rounding difference into the last line so the gross total is exact.
    target_net = net_from_gross(gross_total, rate)
    drift = round(target_net - sum(i.total for i in invoice.items), 2)
    if drift and invoice.items:
        last = invoice.items[-1]
        last.total = round(last.total + drift, 2)
        last.unit_price = (
            round(last.total / last.quantity, 2) if last.quantity else last.total
        )

    return invoice


def format_money(value: float, config=None) -> str:
    """Spanish number formatting: 1.250,50 €"""
    if config is None:
        from src.config_loader import get_config

        config = get_config()
    formatted = f"{value:,.2f}".replace(",", "@").replace(".", ",").replace("@", ".")
    return f"{formatted} {config.currency_symbol}"


def summary_lines(invoice: InvoiceData, config=None) -> str:
    """The base / VAT / total block shown in Telegram and on the review page."""
    if config is None:
        from src.config_loader import get_config

        config = get_config()
    subtotal, tax, total = compute_totals(invoice, config)
    note = ""
    if invoice.prices_include_tax:
        note = " _(precios dictados con IVA incluido)_"
    return (
        f"Base imponible: {format_money(subtotal, config)}\n"
        f"IVA ({config.tax_rate:g}%): {format_money(tax, config)}\n"
        f"*TOTAL: {format_money(total, config)}*{note}"
    )
