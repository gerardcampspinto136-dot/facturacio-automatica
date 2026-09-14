"""Issuing an invoice moves stock, and says what it moved.

Sheets, Gmail and the PDF builder are stubbed out: this is about the wiring between
finalize_invoice and the catalog, which is what puts "quedan 80 ud" in the chat.
"""

import sys
import types

import pytest


@pytest.fixture(autouse=True)
def stub_outside_world(monkeypatch, tmp_path):
    """Replace everything that would reach the network or the filesystem."""
    sheets = types.ModuleType("src.sheets")
    sheets.add_invoice_to_sheet = lambda invoice: None

    email_sender = types.ModuleType("src.email_sender")
    email_sender.send_invoice_email = lambda invoice, path: None
    email_sender.send_email = lambda *a, **k: None

    generator = types.ModuleType("src.invoice_generator")
    generator.generate_invoice_pdf = lambda invoice, path: path

    for name, module in (
        ("src.sheets", sheets),
        ("src.email_sender", email_sender),
        ("src.invoice_generator", generator),
    ):
        monkeypatch.setitem(sys.modules, name, module)

    # finalize imported these by name at import time, so rebind them there too.
    import src.finalize as finalize

    monkeypatch.setattr(finalize, "add_invoice_to_sheet", sheets.add_invoice_to_sheet)
    monkeypatch.setattr(finalize, "send_invoice_email", email_sender.send_invoice_email)
    monkeypatch.setattr(finalize, "generate_invoice_pdf", generator.generate_invoice_pdf)
    return finalize


def an_invoice(*items, number=None):
    from src.models import InvoiceData

    return InvoiceData(
        client_name="Talleres Mario",
        client_email="",          # keeps the email path out of it
        items=list(items),
        invoice_number=number,
    )


def test_issuing_deducts_stock_and_reports_it(stub_outside_world):
    from src import catalog
    from src.models import InvoiceItem

    catalog.create("Tornillos M8", stock_qty=100, unit_price=0.25)
    invoice = an_invoice(InvoiceItem("20 tornillos M8 inoxidables", 20, 0.25))

    stub_outside_world.finalize_invoice(invoice)

    assert catalog.find_by_name("Tornillos M8")["stock_qty"] == 80
    assert [(m["name"], m["balance"]) for m in invoice.stock_movements] == [
        ("Tornillos M8", 80)
    ]


def test_a_service_only_invoice_reports_no_movement(stub_outside_world):
    from src import catalog
    from src.models import InvoiceItem

    catalog.create("Mano de obra", track_stock=0)
    invoice = an_invoice(InvoiceItem("4 horas de mano de obra", 4, 45.0))

    stub_outside_world.finalize_invoice(invoice)
    assert invoice.stock_movements == []


def test_the_movement_is_tagged_with_the_number_that_was_assigned(stub_outside_world):
    from src import catalog
    from src.models import InvoiceItem

    product = catalog.create("Tornillos M8", stock_qty=100)
    invoice = an_invoice(InvoiceItem("20 tornillos M8", 20, 0.25))

    stub_outside_world.finalize_invoice(invoice)

    assert invoice.invoice_number  # assigned during finalize
    assert catalog.history(product)[0]["ref"] == invoice.invoice_number


def test_low_stock_is_flagged_on_the_movement(stub_outside_world):
    from src import catalog
    from src.models import InvoiceItem

    catalog.create("Tornillos M8", stock_qty=25, reorder_point=20)
    invoice = an_invoice(InvoiceItem("20 tornillos M8", 20, 0.25))

    stub_outside_world.finalize_invoice(invoice)
    assert invoice.stock_movements[0]["low"] is True


def test_stock_is_not_moved_twice_for_one_invoice(stub_outside_world):
    from src import catalog
    from src.models import InvoiceItem

    product = catalog.create("Tornillos M8", stock_qty=100)
    invoice = an_invoice(InvoiceItem("20 tornillos M8", 20, 0.25))

    stub_outside_world.finalize_invoice(invoice)
    assert catalog.get(product)["stock_qty"] == 80
    assert len([m for m in catalog.history(product) if m["reason"] == catalog.SALE]) == 1
