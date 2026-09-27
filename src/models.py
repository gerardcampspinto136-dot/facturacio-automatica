from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional
from datetime import date


def round_money(value) -> float:
    """Round to the cent the way an invoice does: half up, on the exact decimal value.

    Python's round() works on binary floats, where 1255.50 x 21% is 263.654999...:
    it gives 263.65 where every accountant writes 263.66. A cent off is enough for the
    client's bookkeeping not to match the invoice.
    """
    return float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def percent_of(amount, rate) -> float:
    """rate% of amount, computed in decimal and rounded to the cent."""
    exact = Decimal(str(amount)) * Decimal(str(rate)) / Decimal(100)
    return float(exact.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


@dataclass
class InvoiceItem:
    description: str
    quantity: float = 1.0
    unit_price: float = 0.0
    total: float = 0.0

    def __post_init__(self):
        if self.total == 0.0 and self.unit_price > 0:
            self.total = round_money(Decimal(str(self.quantity))
                                     * Decimal(str(self.unit_price)))


@dataclass
class InvoiceData:
    client_name: str
    client_email: str
    items: list

    client_address: Optional[str] = None
    client_id: Optional[str] = None
    invoice_number: Optional[str] = None
    date: date = field(default_factory=date.today)
    notes: Optional[str] = None
    # When set, this is a rectifying (contra) invoice cancelling the given invoice number.
    rectifies: Optional[str] = None
    # True  -> the amounts as dictated already contain VAT
    # False -> VAT is added on top
    # None  -> the speaker did not say; the company default decides
    prices_include_tax: Optional[bool] = None
    # Set once line prices have been converted to net, so it never happens twice.
    prices_normalized: bool = False
    # Link to the stored client record, when one was matched.
    contact_id: Optional[int] = None
    # The VAT and IRPF-withholding percentages for THIS invoice. None means "the
    # company default", resolved and frozen when the invoice is issued -- so changing
    # the default later never rewrites an invoice already sent.
    tax_rate: Optional[float] = None
    irpf_rate: Optional[float] = None
    # Why an invoice at 0% carries no VAT: a key of src/exemptions.REASONS.
    vat_reason: Optional[str] = None
    # When the client has to pay by. Set when the invoice is issued, from the payment
    # terms in force then, so a PDF rebuilt later still shows the same date.
    due_date: Optional[date] = None
    # "invoice", or "quote" when what was dictated is a presupuesto.
    document: str = "invoice"
    # For an invoice made from an accepted quote: the quote's number.
    quote_number: Optional[str] = None
    # Stock moved when this invoice was issued, so the bot can report it in the chat.
    # Filled in by finalize_invoice; not part of the invoice document itself.
    stock_movements: list = field(default_factory=list)

    @property
    def subtotal(self) -> float:
        return round_money(sum(Decimal(str(item.total)) for item in self.items))
