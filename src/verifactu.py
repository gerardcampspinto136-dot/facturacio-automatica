"""Verifactu: the chained, tamper-evident register of every invoice, and its QR.

Spain's anti-fraud invoicing rules (RD 1007/2023, Orden HAC/1177/2024) apply to this
software's clients from 1 January 2027 (companies paying Impuesto de Sociedades) and
1 July 2027 (everyone else). What they require of the invoicing software:

1. **A billing record for every invoice issued** ("registro de facturación de alta"),
   written when the invoice is, holding a SHA-256 fingerprint ("huella") computed over
   the invoice's key fields *and the previous record's fingerprint*. The records form a
   chain: altering or deleting any invoice breaks every fingerprint after it, so a
   rewritten history cannot go unnoticed.
2. **A QR code on the invoice** -- at the top, 30-40 mm, error correction M, headed
   "QR tributario:" -- pointing at the AEAT's page for checking it.
3. **Immutability**: issued invoices and their records are never edited or deleted
   (enforced by triggers, see db.py).
4. Either sending every record to the AEAT as it is made ("VERI*FACTU" mode), or
   signing each record electronically and keeping an event log ("no VERI*FACTU").

What is built here is 1-3, to the letter of the AEAT's published technical documents
("Detalle de las especificaciones técnicas para la generación de la huella o hash de
los registros", v0.1.2, and the QR specification), and tested against their worked
examples. **Not built yet: 4** -- the submission needs each client's digital
certificate to test with. Until then the installation is a "no VERI*FACTU" system, so
the QR uses the ValidarQRNoVerifactu address and the "VERI*FACTU" legend -- which
asserts the record reached the AEAT -- is deliberately NOT printed.
"""

import hashlib
import re
from datetime import datetime
from typing import Optional
from urllib.parse import quote

from src import db

ALTA = "alta"
ANULACION = "anulacion"

# The QR's destination, per the AEAT QR specification, section 5.
QR_URLS = {
    ("verifactu", "production"): "https://www2.agenciatributaria.gob.es/wlpl/TIKE-CONT/ValidarQR",
    ("verifactu", "test"): "https://prewww2.aeat.es/wlpl/TIKE-CONT/ValidarQR",
    ("no_verifactu", "production"):
        "https://www2.agenciatributaria.gob.es/wlpl/TIKE-CONT/ValidarQRNoVerifactu",
    ("no_verifactu", "test"): "https://prewww2.aeat.es/wlpl/TIKE-CONT/ValidarQRNoVerifactu",
}

# Records are sent to the AEAT only once the submission milestone exists.
SENDING_TO_AEAT = False


# ── The fingerprint ──────────────────────────────────────────────────────────

def _clean(value) -> str:
    """A field value as it goes into the fingerprint: spaces at either end removed."""
    return str(value if value is not None else "").strip()


def amount(value: float) -> str:
    """An amount as written in the record: a dot and two decimals ("123.10")."""
    return f"{float(value):.2f}"


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest().upper()


def hash_alta(issuer_nif: str, number: str, issue_date: str, invoice_type: str,
              tax_total: str, amount_total: str, previous_hash: str,
              generated_at: str) -> str:
    """The fingerprint of a registration record (RegistroAlta), field order per spec 3.a."""
    fields = (
        ("IDEmisorFactura", issuer_nif),
        ("NumSerieFactura", number),
        ("FechaExpedicionFactura", issue_date),
        ("TipoFactura", invoice_type),
        ("CuotaTotal", tax_total),
        ("ImporteTotal", amount_total),
        ("Huella", previous_hash),
        ("FechaHoraHusoGenRegistro", generated_at),
    )
    return _sha256("&".join(f"{name}={_clean(value)}" for name, value in fields))


def hash_anulacion(issuer_nif: str, number: str, issue_date: str, previous_hash: str,
                   generated_at: str) -> str:
    """The fingerprint of a cancellation record (RegistroAnulacion), spec 3.b."""
    fields = (
        ("IDEmisorFacturaAnulada", issuer_nif),
        ("NumSerieFacturaAnulada", number),
        ("FechaExpedicionFacturaAnulada", issue_date),
        ("Huella", previous_hash),
        ("FechaHoraHusoGenRegistro", generated_at),
    )
    return _sha256("&".join(f"{name}={_clean(value)}" for name, value in fields))


def clean_nif(value: Optional[str]) -> str:
    return re.sub(r"[\s.\-]", "", (value or "")).upper()


def _now() -> str:
    """The generation time with its UTC offset, as the record requires ("+02:00")."""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def invoice_type(client_id: Optional[str], rectifies: Optional[str],
                 rectified_type: Optional[str] = None) -> str:
    """F1 a full invoice; F2 one whose recipient is not identified by tax id; R1 a
    rectifying invoice, R5 when what it rectifies was itself an F2."""
    if rectifies:
        return "R5" if rectified_type == "F2" else "R1"
    cleaned = clean_nif(client_id)
    if not cleaned or cleaned == "SINNIF":
        return "F2"
    return "F1"


# ── Writing records ──────────────────────────────────────────────────────────

def _previous_hash(conn) -> str:
    row = conn.execute(
        "SELECT hash FROM verifactu_records ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return row["hash"] if row else ""


def register_issued(conn, number: str) -> dict:
    """Write the registration record for an invoice just issued, in the SAME transaction.

    Called by finalize.record() and rectify.rectify() inside the transaction that
    consumed the number: the invoice and its record exist together or not at all, and
    BEGIN IMMEDIATE serialises the chain so two invoices can never claim the same
    predecessor.
    """
    from src.config_loader import get_config
    from src.store import _row_to_invoice
    from src.totals import breakdown

    row = conn.execute(
        "SELECT * FROM invoices WHERE number = ? AND status = 'issued'", (number,)
    ).fetchone()
    if row is None:
        raise KeyError(f"Issued invoice {number} not found")
    invoice = _row_to_invoice(conn, row)
    config = get_config()
    totals = breakdown(invoice, config)

    rectified_type = None
    if invoice.rectifies:
        original = conn.execute(
            "SELECT invoice_type FROM verifactu_records WHERE invoice_number = ? "
            "AND kind = ? ORDER BY id DESC LIMIT 1", (invoice.rectifies, ALTA)
        ).fetchone()
        rectified_type = original["invoice_type"] if original else None

    record = {
        "kind": ALTA,
        "invoice_number": number,
        "issuer_nif": clean_nif(config.cif),
        "issue_date": invoice.date.strftime("%d-%m-%Y"),
        "invoice_type": invoice_type(invoice.client_id, invoice.rectifies, rectified_type),
        "tax_total": amount(totals.tax),
        "amount_total": amount(totals.gross),
        "previous_hash": _previous_hash(conn),
        "generated_at": _now(),
    }
    record["hash"] = hash_alta(
        record["issuer_nif"], number, record["issue_date"], record["invoice_type"],
        record["tax_total"], record["amount_total"], record["previous_hash"],
        record["generated_at"],
    )
    conn.execute(
        "INSERT INTO verifactu_records (kind, invoice_number, issuer_nif, issue_date, "
        "invoice_type, tax_total, amount_total, previous_hash, generated_at, hash) "
        "VALUES (:kind, :invoice_number, :issuer_nif, :issue_date, :invoice_type, "
        ":tax_total, :amount_total, :previous_hash, :generated_at, :hash)",
        record,
    )
    return record


def record_for(number: str) -> Optional[dict]:
    row = db.connect().execute(
        "SELECT * FROM verifactu_records WHERE invoice_number = ? AND kind = ? "
        "ORDER BY id DESC LIMIT 1", (number, ALTA)
    ).fetchone()
    return dict(row) if row else None


def list_records(limit: int = 200) -> list[dict]:
    rows = db.connect().execute(
        "SELECT * FROM verifactu_records ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(r) for r in rows]


# ── Checking the chain ───────────────────────────────────────────────────────

def verify_chain() -> tuple[bool, list[str]]:
    """Recompute every fingerprint and every link. Returns (intact, problems).

    This is what an inspector's tool would do. It catches a record edited behind the
    triggers' back (straight in the file), one removed from the middle, and an issued
    invoice whose amounts no longer match what was registered for it.
    """
    from src import store
    from src.totals import breakdown

    problems: list[str] = []
    previous = ""
    rows = db.connect().execute("SELECT * FROM verifactu_records ORDER BY id").fetchall()
    for row in rows:
        r = dict(row)
        if r["previous_hash"] != previous:
            problems.append(f"{r['invoice_number']}: no enlaza con el registro anterior "
                            "(falta o se ha alterado un registro).")
        if r["kind"] == ALTA:
            expected = hash_alta(r["issuer_nif"], r["invoice_number"], r["issue_date"],
                                 r["invoice_type"], r["tax_total"], r["amount_total"],
                                 r["previous_hash"], r["generated_at"])
        else:
            expected = hash_anulacion(r["issuer_nif"], r["invoice_number"],
                                      r["issue_date"], r["previous_hash"],
                                      r["generated_at"])
        if expected != r["hash"]:
            problems.append(f"{r['invoice_number']}: la huella no corresponde a sus datos.")
        if r["kind"] == ALTA:
            issued = store.get_issued(r["invoice_number"])
            if issued is None:
                problems.append(f"{r['invoice_number']}: registrada pero la factura "
                                "ya no existe.")
            else:
                t = breakdown(issued["invoice"])
                if (amount(t.tax), amount(t.gross)) != (r["tax_total"], r["amount_total"]):
                    problems.append(f"{r['invoice_number']}: los importes de la factura "
                                    "no coinciden con los registrados.")
        previous = r["hash"]
    return not problems, problems


# ── The QR ───────────────────────────────────────────────────────────────────

def environment(config) -> str:
    """'test' for an installation still on the example company, else 'production'."""
    forced = (getattr(config, "verifactu_environment", "") or "").strip().lower()
    if forced in ("test", "production"):
        return forced
    return "test" if config.is_placeholder else "production"


def qr_url(issuer_nif: str, number: str, issue_date: str, total: str, *,
           verifactu: bool = SENDING_TO_AEAT, env: str = "production") -> str:
    """The URL the QR encodes: base + the four parameters, URL-encoded as UTF-8."""
    base = QR_URLS[("verifactu" if verifactu else "no_verifactu", env)]
    params = (("nif", issuer_nif), ("numserie", number), ("fecha", issue_date),
              ("importe", total))
    return base + "?" + "&".join(f"{k}={quote(str(v), safe='')}" for k, v in params)


def qr_url_for(invoice, config=None) -> Optional[str]:
    from src.config_loader import get_config
    from src.totals import breakdown

    config = config or get_config()
    if not invoice.invoice_number:
        return None
    record = record_for(invoice.invoice_number)
    if record:
        # Exactly what was registered: a PDF rebuilt later must carry the same QR.
        nif, date_, total = record["issuer_nif"], record["issue_date"], record["amount_total"]
    else:
        nif = clean_nif(config.cif)
        date_ = invoice.date.strftime("%d-%m-%Y")
        total = amount(breakdown(invoice, config).gross)
    return qr_url(nif, invoice.invoice_number, date_, total, env=environment(config))


def qr_flowable(invoice, config):
    """The QR as it goes on the PDF: "QR tributario:" above a 35 mm code.

    Returns None when the QR is switched off (verifactu.qr: false in company.yaml).
    """
    if not getattr(config, "verifactu_qr", True):
        return None
    url = qr_url_for(invoice, config)
    if not url:
        return None

    from reportlab.graphics.barcode.qr import QrCodeWidget
    from reportlab.graphics.shapes import Drawing
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, Table, TableStyle

    size = 35 * mm  # within the 30-40 mm the order requires
    widget = QrCodeWidget(url, barLevel="M", barBorder=0)
    x0, y0, x1, y1 = widget.getBounds()
    drawing = Drawing(size, size, transform=[size / (x1 - x0), 0, 0,
                                             size / (y1 - y0), 0, 0])
    drawing.add(widget)

    label = ParagraphStyle("QrLabel", fontName="Helvetica", fontSize=10, alignment=1)
    rows = [[Paragraph("QR tributario:", label)], [drawing]]
    if SENDING_TO_AEAT:
        rows.append([Paragraph("VERI*FACTU", label)])
    table = Table(rows, colWidths=[size + 8 * mm])
    table.setStyle(TableStyle([
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        # The spec asks for at least 2 mm of clear space around the code; 4 here.
        ("TOPPADDING", (0, 1), (-1, 1), 4 * mm),
        ("BOTTOMPADDING", (0, 1), (-1, 1), 4 * mm),
    ]))
    return table
