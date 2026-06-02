import os
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import (
    HRFlowable,
    Image,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from src.config_loader import get_config
from src.models import InvoiceData

# Brand colour used throughout the invoice
BRAND_DARK = colors.HexColor("#1a3a5c")
BRAND_LIGHT = colors.HexColor("#f0f4f8")
GREY_LINE = colors.HexColor("#d0d7de")
TEXT_MUTED = colors.HexColor("#6e7781")


def _style(name: str, **kwargs) -> ParagraphStyle:
    base = getSampleStyleSheet()["Normal"]
    return ParagraphStyle(name, parent=base, **kwargs)


def generate_invoice_pdf(invoice: InvoiceData, output_path: str) -> str:
    config = get_config()
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    doc = SimpleDocTemplate(
        output_path,
        pagesize=A4,
        rightMargin=2 * cm,
        leftMargin=2 * cm,
        topMargin=2 * cm,
        bottomMargin=2 * cm,
    )

    elements = []

    # ── Header: logo + company info ──────────────────────────────────────────
    logo_logo_cell: object
    if config.logo_path and os.path.exists(config.logo_path):
        logo_logo_cell = Image(config.logo_path, width=5 * cm, height=2.2 * cm, kind="proportional")
    else:
        logo_logo_cell = Paragraph(
            f"<b>{config.name}</b>",
            _style("LogoText", fontSize=18, textColor=BRAND_DARK),
        )

    company_block = (
        f"<b>{config.name}</b><br/>"
        f"{config.address}<br/>"
        f"Tel: {config.phone}<br/>"
        f"CIF: {config.cif}<br/>"
        f"{config.email}"
    )
    company_cell = Paragraph(company_block, _style("CompanyInfo", fontSize=9, alignment=TA_RIGHT, textColor=TEXT_MUTED))

    header_tbl = Table([[logo_logo_cell, company_cell]], colWidths=[9.5 * cm, 7.5 * cm])
    header_tbl.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 12),
    ]))
    elements.append(header_tbl)
    elements.append(HRFlowable(width="100%", thickness=2, color=BRAND_DARK, spaceAfter=10))

    # ── Invoice title + number ───────────────────────────────────────────────
    title_row = Table(
        [[
            Paragraph("<b>FACTURA</b>", _style("InvTitle", fontSize=22, textColor=BRAND_DARK)),
            Paragraph(
                f"<b>N.º {invoice.invoice_number}</b>",
                _style("InvNum", fontSize=13, alignment=TA_RIGHT, textColor=BRAND_DARK),
            ),
        ]],
        colWidths=[9.5 * cm, 7.5 * cm],
    )
    title_row.setStyle(TableStyle([("BOTTOMPADDING", (0, 0), (-1, -1), 8)]))
    elements.append(title_row)

    # ── Client info + date ───────────────────────────────────────────────────
    invoice_date = invoice.date.strftime("%d/%m/%Y")
    client_lines = [
        "<b>Facturar a:</b>",
        f"<b>{invoice.client_name}</b>",
    ]
    if invoice.client_address:
        client_lines.append(invoice.client_address)
    if invoice.client_id:
        client_lines.append(f"NIF/CIF: {invoice.client_id}")
    client_lines.append(invoice.client_email)

    info_row = Table(
        [[
            Paragraph("<br/>".join(client_lines), _style("ClientInfo", fontSize=10, leading=16)),
            Paragraph(
                f"<b>Fecha:</b> {invoice_date}<br/><b>Pago:</b> {config.payment_terms}",
                _style("DateInfo", fontSize=10, alignment=TA_RIGHT, leading=16),
            ),
        ]],
        colWidths=[9.5 * cm, 7.5 * cm],
    )
    info_row.setStyle(TableStyle([
        ("TOPPADDING", (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 16),
    ]))
    elements.append(info_row)

    # ── Items table ──────────────────────────────────────────────────────────
    sym = config.currency_symbol
    col_widths = [9 * cm, 2 * cm, 3 * cm, 3 * cm]
    rows = [["Descripción", "Cant.", f"Precio unit. ({sym})", f"Total ({sym})"]]

    for item in invoice.items:
        rows.append([
            item.description,
            f"{item.quantity:g}",
            f"{item.unit_price:,.2f}",
            f"{item.total:,.2f}",
        ])

    items_tbl = Table(rows, colWidths=col_widths, repeatRows=1)
    items_tbl.setStyle(TableStyle([
        # Header
        ("BACKGROUND", (0, 0), (-1, 0), BRAND_DARK),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, 0), 9),
        # Body
        ("FONTNAME", (0, 1), (-1, -1), "Helvetica"),
        ("FONTSIZE", (0, 1), (-1, -1), 10),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, BRAND_LIGHT]),
        ("GRID", (0, 0), (-1, -1), 0.4, GREY_LINE),
        ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
        ("ALIGN", (0, 0), (0, -1), "LEFT"),
        ("TOPPADDING", (0, 0), (-1, -1), 7),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
    ]))
    elements.append(items_tbl)
    elements.append(Spacer(1, 0.4 * cm))

    # ── Totals ───────────────────────────────────────────────────────────────
    subtotal = invoice.subtotal
    tax_amount = round(subtotal * config.tax_rate / 100, 2)
    total = round(subtotal + tax_amount, 2)

    totals_data = [
        ["", "Base imponible:", f"{subtotal:,.2f} {sym}"],
        ["", f"IVA ({config.tax_rate}%):", f"{tax_amount:,.2f} {sym}"],
        ["", "TOTAL:", f"{total:,.2f} {sym}"],
    ]
    totals_tbl = Table(totals_data, colWidths=[10.5 * cm, 4 * cm, 2.5 * cm])
    totals_tbl.setStyle(TableStyle([
        ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
        ("FONTNAME", (0, 0), (-1, 1), "Helvetica"),
        ("FONTNAME", (0, 2), (-1, 2), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, 1), 10),
        ("FONTSIZE", (0, 2), (-1, 2), 12),
        ("TEXTCOLOR", (0, 2), (-1, 2), BRAND_DARK),
        ("LINEABOVE", (1, 2), (-1, 2), 1.5, BRAND_DARK),
        ("TOPPADDING", (0, 2), (-1, 2), 10),
    ]))
    elements.append(totals_tbl)

    # ── Notes ────────────────────────────────────────────────────────────────
    if invoice.notes:
        elements.append(Spacer(1, 0.6 * cm))
        elements.append(Paragraph(f"<b>Notas:</b> {invoice.notes}", _style("Notes", fontSize=9, textColor=TEXT_MUTED)))

    # ── Bank account ─────────────────────────────────────────────────────────
    if config.bank_account:
        elements.append(Spacer(1, 1 * cm))
        elements.append(HRFlowable(width="100%", thickness=0.5, color=GREY_LINE))
        elements.append(Spacer(1, 0.3 * cm))
        elements.append(
            Paragraph(
                f"Datos bancarios: {config.bank_account}",
                _style("Bank", fontSize=8, textColor=TEXT_MUTED),
            )
        )

    doc.build(elements)
    return output_path
