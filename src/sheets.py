import os

import gspread

from src.config_loader import get_config
from src.google_auth import get_credentials
from src.models import InvoiceData
from src.totals import breakdown

# The first ten columns are the original layout; anything added later goes on the
# end, so a sheet a client has been filling for months keeps its columns in place.
_HEADERS = [
    "N.º Factura", "Fecha", "Cliente", "Email", "Dirección",
    "NIF/CIF", "Base imponible", "IVA", "Total", "Notas",
    "Tipo IVA %", "Retención IRPF", "Total a pagar", "Rectifica a",
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
        _extend_headers(ws)
    except gspread.exceptions.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title=sheet_name, rows=1000, cols=len(_HEADERS))
        ws.append_row(_HEADERS)
        _format_header_row(ws)

    t = breakdown(invoice, get_config())
    ws.append_row([
        invoice.invoice_number,
        invoice.date.strftime("%d/%m/%Y"),
        invoice.client_name,
        invoice.client_email,
        invoice.client_address or "",
        invoice.client_id or "",
        t.base,
        t.tax,
        t.gross,
        invoice.notes or "",
        t.tax_rate,
        t.irpf,
        t.total,
        invoice.rectifies or "",
    ])


def _extend_headers(ws: gspread.Worksheet) -> None:
    """Give an older sheet the newer column headings, without touching its data."""
    current = ws.row_values(1)
    if len(current) >= len(_HEADERS) or not current:
        return
    missing = _HEADERS[len(current):]
    start = gspread.utils.rowcol_to_a1(1, len(current) + 1)
    end = gspread.utils.rowcol_to_a1(1, len(_HEADERS))
    if ws.col_count < len(_HEADERS):
        ws.add_cols(len(_HEADERS) - ws.col_count)
    ws.update(values=[missing], range_name=f"{start}:{end}")


def _format_header_row(ws: gspread.Worksheet) -> None:
    end = gspread.utils.rowcol_to_a1(1, len(_HEADERS))
    ws.format(f"A1:{end}", {
        "backgroundColor": {"red": 0.1, "green": 0.227, "blue": 0.361},
        "textFormat": {"bold": True, "foregroundColor": {"red": 1, "green": 1, "blue": 1}},
    })
