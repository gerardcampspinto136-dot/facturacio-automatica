from dataclasses import dataclass, field
from typing import Optional
from datetime import date


@dataclass
class InvoiceItem:
    description: str
    quantity: float = 1.0
    unit_price: float = 0.0
    total: float = 0.0

    def __post_init__(self):
        if self.total == 0.0 and self.unit_price > 0:
            self.total = round(self.quantity * self.unit_price, 2)


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

    @property
    def subtotal(self) -> float:
        return round(sum(item.total for item in self.items), 2)
