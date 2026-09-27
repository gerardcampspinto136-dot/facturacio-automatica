import os
from datetime import timedelta
from pathlib import Path
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
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
from src.totals import breakdown, rate_label, spanish_number

# Brand colour used throughout the invoice
BRAND_DARK = colors.HexColor("#1a3a5c")
BRAND_LIGHT = colors.HexColor("#f0f4f8")
GREY_LINE = colors.HexColor("#d0d7de")
TEXT_MUTED = colors.HexColor("#6e7781")


def _style(name: str, **kwargs) -> ParagraphStyle:
    base = getSampleStyleSheet()["Normal"]
    return ParagraphStyle(name, parent=base, **kwargs)


def _t(value) -> str:
    """Text for a Paragraph. ReportLab reads its input as markup, so a client called
    "Pérez <Reformas>" used to lose "<Reformas>" -- silently -- and a stray "<b" could
    stop the PDF being built at all, which stops the invoice being issued."""
    return escape(str(value or ""))


def _money(value: float) -> str:
    """Amounts as a Spanish invoice writes them: 1.250,50 -- not 1,250.50."""
    return spanish_number(value)


def _qr_block(invoice: InvoiceData, config):
    """The Verifactu QR for an issued invoice, or None (a draft has no number to check,
    and a quote is not an invoice)."""
    if not invoice.invoice_number or invoice.document == "quote":
        return None
    from src import verifactu

    return verifactu.qr_flowable(invoice, config)


def generate_invoice_pdf(invoice: InvoiceData, output_path: str,
                         valid_until=None) -> str:
    """Render an invoice -- or, when invoice.document is "quote", a quote -- to a PDF."""
    config = get_config()
    is_quote = invoice.document == "quote"
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    doc = SimpleDocTemplate(
        output_path,
        pagesize=A4,
        rightMargin=2 * cm,
        leftMargin=2 * cm,
        topMargin=2 * cm,
        bottomMargin=2 * cm,
        title=(f"Presupuesto {invoice.invoice_number}" if is_quote
               else f"Factura {invoice.invoice_number or 'borrador'}"),
        author=config.name,
    )

    elements = []

    # ── Not-a-real-invoice banner ────────────────────────────────────────────
    # This software is installed once per client company. Until their real details
    # replace the shipped placeholders, every invoice says so on its face, so a demo
    # PDF can never be mistaken for a document with fiscal value -- and so an
    # installation that was never configured is obvious at a glance.
    if config.is_placeholder:
        elements.append(
            Paragraph(
                "DOCUMENTO DE PRUEBA — SIN VALOR FISCAL<br/>"
                "<font size='7'>Los datos de la empresa emisora no están "
                "configurados (config/company.yaml)</font>",
                _style(
                    "TestBanner",
                    fontSize=11,
                    alignment=1,
                    textColor=colors.white,
                    backColor=colors.HexColor("#c0392b"),
                    borderPadding=6,
                    leading=14,
                ),
            )
        )
        elements.append(Spacer(1, 0.5 * cm))

    if not invoice.invoice_number and not is_quote:
        # A draft waiting for approval: it has no number yet, and must not pass for
        # an invoice if it is forwarded.
        elements.append(
            Paragraph(
                "BORRADOR — pendiente de aprobación, no es una factura",
                _style("DraftBanner", fontSize=10, alignment=1,
                       textColor=colors.HexColor("#8a5a00"),
                       backColor=colors.HexColor("#fff4d6"), borderPadding=5),
            )
        )
        elements.append(Spacer(1, 0.4 * cm))

    # ── Header: logo + company info ──────────────────────────────────────────
    logo_cell: object
    if config.logo_path and os.path.exists(config.logo_path):
        logo_cell = Image(config.logo_path, width=4.8 * cm, height=2.2 * cm, kind="proportional")
    else:
        logo_cell = Paragraph(
            f"<b>{_t(config.name)}</b>",
            _style("LogoText", fontSize=18, textColor=BRAND_DARK),
        )

    company_lines = [f"<b>{_t(config.name)}</b>", _t(config.address)]
    if config.phone:
        company_lines.append(f"Tel: {_t(config.phone)}")
    company_lines.append(f"NIF: {_t(config.cif)}")
    if config.email:
        company_lines.append(_t(config.email))
    company_cell = Paragraph("<br/>".join(company_lines),
                             _style("CompanyInfo", fontSize=9, alignment=TA_RIGHT,
                                    textColor=TEXT_MUTED))

    # The Verifactu QR goes first, at the top of the first page: the AEAT's
    # specification asks for it before any of the invoice's own content.
    qr = _qr_block(invoice, config)
    if qr is not None:
        header_tbl = Table([[qr, logo_cell, company_cell]],
                           colWidths=[4.6 * cm, 5.4 * cm, 7 * cm])
    else:
        header_tbl = Table([[logo_cell, company_cell]], colWidths=[9.5 * cm, 7.5 * cm])
    header_tbl.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 12),
    ]))
    elements.append(header_tbl)
    elements.append(HRFlowable(width="100%", thickness=2, color=BRAND_DARK, spaceAfter=10))

    # ── Invoice title + number ───────────────────────────────────────────────
    title_text = ("PRESUPUESTO" if is_quote else
                  "FACTURA RECTIFICATIVA" if invoice.rectifies else "FACTURA")
    # A draft waiting for approval has no number yet -- it is assigned on approval so a
    # discarded draft leaves no gap -- and must not look like an issued invoice.
    number_text = (f"N.º {_t(invoice.invoice_number)}" if invoice.invoice_number
                   else "BORRADOR — sin número")
    title_row = Table(
        [[
            Paragraph(f"<b>{title_text}</b>", _style("InvTitle", fontSize=22, textColor=BRAND_DARK)),
            Paragraph(
                f"<b>{number_text}</b>",
                _style("InvNum", fontSize=13, alignment=TA_RIGHT, textColor=BRAND_DARK),
            ),
        ]],
        colWidths=[9.5 * cm, 7.5 * cm],
    )
    title_row.setStyle(TableStyle([("BOTTOMPADDING", (0, 0), (-1, -1), 8)]))
    elements.append(title_row)

    if invoice.rectifies:
        elements.append(
            Paragraph(
                f"Rectifica y anula la factura <b>{_t(invoice.rectifies)}</b>"
                f"{_original_date(invoice.rectifies)}.",
                _style("Rectifies", fontSize=9, textColor=TEXT_MUTED),
            )
        )
        elements.append(Spacer(1, 0.2 * cm))

    # ── Client info + dates ──────────────────────────────────────────────────
    client_lines = [
        "<b>Facturar a:</b>",
        f"<b>{_t(invoice.client_name)}</b>",
    ]
    if invoice.client_address:
        client_lines.append(_t(invoice.client_address))
    if invoice.client_id:
        client_lines.append(f"NIF/CIF: {_t(invoice.client_id)}")
    if invoice.client_email:
        client_lines.append(_t(invoice.client_email))

    date_lines = [f"<b>Fecha:</b> {invoice.date.strftime('%d/%m/%Y')}"]
    if is_quote:
        if valid_until:
            date_lines.append(f"<b>Válido hasta:</b> {valid_until.strftime('%d/%m/%Y')}")
    elif not invoice.rectifies:
        due = invoice.due_date or (invoice.date + timedelta(days=config.payment_days))
        date_lines.append(f"<b>Vencimiento:</b> {due.strftime('%d/%m/%Y')}")
        date_lines.append(f"<b>Pago:</b> {_t(config.payment_terms)}")

    info_row = Table(
        [[
            Paragraph("<br/>".join(client_lines), _style("ClientInfo", fontSize=10, leading=16)),
            Paragraph("<br/>".join(date_lines),
                      _style("DateInfo", fontSize=10, alignment=TA_RIGHT, leading=16)),
        ]],
        colWidths=[9.5 * cm, 7.5 * cm],
    )
    info_row.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 16),
    ]))
    elements.append(info_row)

    # ── Items table ──────────────────────────────────────────────────────────
    sym = config.currency_symbol
    col_widths = [9 * cm, 2 * cm, 3 * cm, 3 * cm]
    rows = [["Descripción", "Cant.", f"Precio unit. ({sym})", f"Total ({sym})"]]
    # A description is a Paragraph so a long one wraps inside its column instead of
    # running across the prices.
    desc_style = _style("ItemDesc", fontSize=10, leading=13)

    for item in invoice.items:
        rows.append([
            Paragraph(_t(item.description), desc_style),
            f"{item.quantity:g}".replace(".", ","),
            _money(item.unit_price),
            _money(item.total),
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
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 7),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
    ]))
    elements.append(items_tbl)
    elements.append(Spacer(1, 0.4 * cm))

    # ── Totals ───────────────────────────────────────────────────────────────
    t = breakdown(invoice, config)
    totals_data = [
        ["", "Base imponible:", f"{_money(t.base)} {sym}"],
        ["", f"IVA ({rate_label(t.tax_rate)}%):", f"{_money(t.tax)} {sym}"],
    ]
    if t.irpf:
        totals_data.append(["", f"Retención IRPF ({rate_label(t.irpf_rate)}%):",
                            f"−{_money(t.irpf)} {sym}"])
    totals_data.append(["", "TOTAL A PAGAR:" if t.irpf else "TOTAL:",
                        f"{_money(t.total)} {sym}"])
    last = len(totals_data) - 1

    totals_tbl = Table(totals_data, colWidths=[9.5 * cm, 4.5 * cm, 3 * cm])
    totals_tbl.setStyle(TableStyle([
        ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
        ("FONTNAME", (0, 0), (-1, last - 1), "Helvetica"),
        ("FONTNAME", (0, last), (-1, last), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, last - 1), 10),
        ("FONTSIZE", (0, last), (-1, last), 12),
        ("TEXTCOLOR", (0, last), (-1, last), BRAND_DARK),
        ("LINEABOVE", (1, last), (-1, last), 1.5, BRAND_DARK),
        ("TOPPADDING", (0, last), (-1, last), 10),
    ]))
    elements.append(totals_tbl)

    # ── Notes ────────────────────────────────────────────────────────────────
    if invoice.notes:
        elements.append(Spacer(1, 0.6 * cm))
        elements.append(Paragraph(f"<b>Notas:</b> {_t(invoice.notes)}",
                                  _style("Notes", fontSize=9, textColor=TEXT_MUTED)))

    # ── Bank account ─────────────────────────────────────────────────────────
    if is_quote:
        elements.append(Spacer(1, 0.8 * cm))
        elements.append(Paragraph(
            "Este presupuesto no es una factura. Para aceptarlo, basta con responder "
            "al correo con el que lo ha recibido.",
            _style("QuoteNote", fontSize=8, textColor=TEXT_MUTED)))
    elif config.bank_account and not invoice.rectifies:
        elements.append(Spacer(1, 0.8 * cm))
        elements.append(HRFlowable(width="100%", thickness=0.5, color=GREY_LINE))
        elements.append(Spacer(1, 0.3 * cm))
        elements.append(
            Paragraph(
                f"Pago por transferencia a la cuenta IBAN {_t(config.bank_account)}"
                + (f" · Referencia: {_t(invoice.invoice_number)}"
                   if invoice.invoice_number else ""),
                _style("Bank", fontSize=8, textColor=TEXT_MUTED),
            )
        )

    doc.build(elements)
    return output_path


def _original_date(number: str) -> str:
    """" de fecha dd/mm/yyyy" for the invoice being rectified, when it can be found."""
    try:
        from src import store

        record = store.get_issued(number)
    except Exception:
        return ""
    if not record:
        return ""
    return f" de fecha {record['invoice'].date.strftime('%d/%m/%Y')}"
