import os

import gspread

from src.config_loader import get_config
from src.google_auth import get_credentials
from src.models import InvoiceData

_HEADERS = [
    "N.º Factura", "Fecha", "Cliente", "Email", "Dirección",
    "NIF/CIF", "Base imponible", "IVA", "Total", "Notas",
]


def add_invoice_to_sheet(invoice: InvoiceData) -> None:
    creds = get_credentials()
    gc = gspread.authorize(creds)

    spreadsheet_id = os.getenv("SPREADSHEET_ID")
    if not spreadsheet_id:
        raise ValueError("SPREADSHEET_ID is not set in .env")

    sheet_name = os.getenv("SPREADSHEET_NAME", "Facturas")
    spreadsheet = gc.open_by_key(spreadsheet_id)

    try:
        ws = spreadsheet.worksheet(sheet_name)
    except gspread.exceptions.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title=sheet_name, rows=1000, cols=len(_HEADERS))
        ws.append_row(_HEADERS)
        _format_header_row(ws)

    config = get_config()
    subtotal = invoice.subtotal
    tax_amount = round(subtotal * config.tax_rate / 100, 2)
    total = round(subtotal + tax_amount, 2)

    ws.append_row([
        invoice.invoice_number,
        invoice.date.strftime("%d/%m/%Y"),
        invoice.client_name,
        invoice.client_email,
        invoice.client_address or "",
        invoice.client_id or "",
        subtotal,
        tax_amount,
        total,
        invoice.notes or "",
    ])


def _format_header_row(ws: gspread.Worksheet) -> None:
    ws.format("A1:J1", {
        "backgroundColor": {"red": 0.1, "green": 0.227, "blue": 0.361},
        "textFormat": {"bold": True, "foregroundColor": {"red": 1, "green": 1, "blue": 1}},
    })
