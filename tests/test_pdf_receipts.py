"""Supplier invoices that arrive as PDF files.

Real PDFs are built here with ReportLab: one with a text layer (as software produces
them) and one that is only a picture (a scan). The model calls are stubbed; what is
tested is that each kind reaches the right reader with the right content.
"""

import json

import pytest
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

from src import receipts

MODEL_ANSWER = json.dumps({
    "supplier_name": "Endesa Energía S.A.U.", "supplier_tax_id": "A81948077",
    "reference": "PI26001234567", "date": "2026-09-05", "total": 84.70,
    "tax_amount": 14.70, "category": "suministros", "paid": False,
    "due_date": "2026-09-20", "confidence": 0.95,
})


@pytest.fixture
def digital_pdf(tmp_path):
    path = tmp_path / "factura_luz.pdf"
    page = canvas.Canvas(str(path), pagesize=A4)
    y = 800
    for line in ("ENDESA ENERGIA S.A.U.  CIF A81948077", "Factura PI26001234567",
                 "Fecha de emision: 05/09/2026", "Base imponible: 70,00 EUR",
                 "IVA 21%: 14,70 EUR", "TOTAL A PAGAR: 84,70 EUR",
                 "Fecha de cargo: 20/09/2026"):
        page.drawString(60, y, line)
        y -= 20
    page.save()
    return path


@pytest.fixture
def scanned_pdf(tmp_path):
    from PIL import Image

    picture = tmp_path / "scan.png"
    Image.new("RGB", (400, 600), "white").save(picture)
    path = tmp_path / "ticket_escaneado.pdf"
    page = canvas.Canvas(str(path), pagesize=A4)
    page.drawImage(str(picture), 50, 200, width=400, height=600)
    page.save()
    return path


def test_a_digital_pdf_is_read_from_its_text(digital_pdf, monkeypatch):
    seen = {}

    def text_model(text, hint):
        seen["text"] = text
        return MODEL_ANSWER

    monkeypatch.setenv("GROQ_API_KEY", "test")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.setattr(receipts, "_complete_text", text_model)
    monkeypatch.setattr(receipts, "_complete_groq",
                        lambda *a: pytest.fail("a digital PDF must not go to vision"))

    receipt = receipts.extract_receipt(str(digital_pdf))

    assert "TOTAL A PAGAR: 84,70" in seen["text"]
    assert (receipt.supplier_name, receipt.total, receipt.tax_amount) == \
        ("Endesa Energía S.A.U.", 84.70, 14.70)
    assert receipt.image_path == str(digital_pdf)


def test_a_scanned_pdf_goes_to_the_vision_model(scanned_pdf, monkeypatch):
    seen = {}

    def vision(b64, mime, hint):
        seen["mime"], seen["size"] = mime, len(b64)
        return MODEL_ANSWER

    monkeypatch.setenv("GROQ_API_KEY", "test")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.setattr(receipts, "_complete_groq", vision)
    monkeypatch.setattr(receipts, "_complete_text",
                        lambda *a: pytest.fail("a scan has no text to read"))

    receipt = receipts.extract_receipt(str(scanned_pdf))

    assert seen["mime"].startswith("image/") and seen["size"] > 100
    assert receipt.total == 84.70


def test_a_broken_pdf_says_what_to_do(tmp_path):
    broken = tmp_path / "roto.pdf"
    broken.write_bytes(b"not a pdf at all")
    with pytest.raises(receipts.ReceiptError, match="No he podido abrir ese PDF"):
        receipts.extract_receipt(str(broken))


def test_the_pdf_itself_is_archived_as_the_proof(digital_pdf, tmp_path, monkeypatch):
    monkeypatch.setattr(receipts, "RECEIPTS_DIR", tmp_path / "receipts")
    receipt = receipts.build_receipt(json.loads(MODEL_ANSWER))
    stored = receipts.archive_image(str(digital_pdf), receipt)
    assert stored.endswith(".pdf") and "Endesa" in stored
