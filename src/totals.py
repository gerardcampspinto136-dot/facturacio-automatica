"""VAT and IRPF arithmetic -- the single place that decides what an invoice adds up to.

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

The rates themselves belong to the invoice once it is issued (`invoice.tax_rate`,
`invoice.irpf_rate`); the company default only fills in an invoice that has none yet.
Otherwise changing the default from 21 to 10 would silently re-total every invoice
already sent.

IRPF withholding (retención) is what a professional's client keeps back and pays to
Hacienda on their behalf: 15% normally, 7% in the first years. It is not a tax on the
invoice, so it does not touch the VAT; it only lowers what the client transfers.
"""

from dataclasses import dataclass
from typing import Optional

from src.models import InvoiceData


def _config(config):
    if config is None:
        from src.config_loader import get_config

        config = get_config()
    return config


def vat_rate(invoice: InvoiceData, config=None) -> float:
    """The VAT % this invoice is at: its own, else the company default."""
    if invoice.tax_rate is not None:
        return float(invoice.tax_rate)
    return float(_config(config).tax_rate)


def irpf_rate(invoice: InvoiceData, config=None) -> float:
    """The IRPF withholding % on this invoice: its own, else the company default (0)."""
    if invoice.irpf_rate is not None:
        return float(invoice.irpf_rate)
    return float(getattr(_config(config), "irpf_rate", 0) or 0)


def rate_label(rate: float) -> str:
    """21.0 -> "21", 5.2 -> "5,2": how a percentage is written on a Spanish invoice."""
    return f"{rate:g}".replace(".", ",")


@dataclass(frozen=True)
class Totals:
    base: float
    tax_rate: float
    tax: float
    irpf_rate: float
    irpf: float
    gross: float    # base + VAT: the invoice amount for VAT purposes
    total: float    # what the client actually pays: gross minus the IRPF withheld


def breakdown(invoice: InvoiceData, config=None) -> Totals:
    """Everything an invoice adds up to, for an invoice holding net line prices."""
    config = _config(config)
    base = invoice.subtotal
    t_rate = vat_rate(invoice, config)
    i_rate = irpf_rate(invoice, config)
    tax = round(base * t_rate / 100, 2)
    irpf = round(base * i_rate / 100, 2)
    gross = round(base + tax, 2)
    return Totals(base, t_rate, tax, i_rate, irpf, gross, round(gross - irpf, 2))


def net_from_gross(gross: float, tax_rate: float) -> float:
    """Strip VAT out of a VAT-inclusive amount."""
    return round(gross / (1 + tax_rate / 100), 2)


def compute_totals(invoice: InvoiceData, config=None) -> tuple[float, float, float]:
    """Return (subtotal, tax_amount, total to pay) for an invoice holding net line prices."""
    t = breakdown(invoice, config)
    return t.base, t.tax, t.total


def prices_are_inclusive(invoice: InvoiceData, config=None) -> bool:
    """Did the amounts on this invoice arrive with VAT already inside them?

    What the speaker said wins; the company default only fills the silence.
    """
    if invoice.prices_include_tax is not None:
        return bool(invoice.prices_include_tax)
    return bool(_config(config).prices_include_tax)


def normalize_prices(invoice: InvoiceData, config=None) -> InvoiceData:
    """Convert VAT-inclusive line prices to net, in place. Safe to call twice.

    Each line is converted on its own and then the last line absorbs any rounding drift,
    so the invoice still totals exactly the gross figure the customer was quoted. Without
    that, three lines of "60 € IVA incluido" can add back up to 179.99.
    """
    config = _config(config)

    if not invoice.items or invoice.prices_normalized:
        invoice.prices_normalized = True
        return invoice

    inclusive = prices_are_inclusive(invoice, config)
    invoice.prices_include_tax = inclusive
    invoice.prices_normalized = True

    if not inclusive:
        return invoice

    rate = vat_rate(invoice, config)
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


def spanish_number(value: float, decimals: int = 2) -> str:
    """1250.5 -> "1.250,50": thousands with dots, decimals with a comma."""
    formatted = f"{value:,.{decimals}f}"
    return formatted.replace(",", "@").replace(".", ",").replace("@", ".")


def format_money(value: float, config=None) -> str:
    """Spanish number formatting: 1.250,50 €"""
    config = _config(config)
    return f"{spanish_number(value)} {config.currency_symbol}"


def summary_lines(invoice: InvoiceData, config=None) -> str:
    """The base / VAT / total block shown in Telegram and on the review page."""
    config = _config(config)
    t = breakdown(invoice, config)
    note = ""
    if invoice.prices_include_tax:
        note = " _(precios dictados con IVA incluido)_"
    lines = [
        f"Base imponible: {format_money(t.base, config)}",
        f"IVA ({rate_label(t.tax_rate)}%): {format_money(t.tax, config)}",
    ]
    if t.irpf:
        lines.append(f"Retención IRPF ({rate_label(t.irpf_rate)}%): "
                     f"−{format_money(t.irpf, config)}")
        lines.append(f"*TOTAL A PAGAR: {format_money(t.total, config)}*{note}")
    else:
        lines.append(f"*TOTAL: {format_money(t.total, config)}*{note}")
    return "\n".join(lines)
