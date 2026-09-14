"""The guard against shipping an installation that was never configured.

This software is installed once per client company, so the dangerous state is a copy
still carrying the placeholders it shipped with: invoices would go to real customers
with a made-up CIF on them. Those invoices are marked instead.
"""

import textwrap

import pytest

from src.config_loader import CompanyConfig

REAL = {
    "name": "Talleres Mario S.L.",
    "cif": "B12345678",
    "address": "Carrer Indústria 5, 08025 Barcelona",
}


def config_file(tmp_path, **company) -> CompanyConfig:
    fields = {**REAL, **company}
    body = "\n".join(f'  {k}: "{v}"' for k, v in fields.items())
    path = tmp_path / "company.yaml"
    path.write_text(
        "company:\n" + body + textwrap.dedent("""
        invoice:
          tax_rate: 21
        """),
        encoding="utf-8",
    )
    return CompanyConfig(str(path))


class TestPlaceholderDetection:
    def test_real_company_details_are_not_flagged(self, tmp_path):
        assert config_file(tmp_path).is_placeholder is False

    @pytest.mark.parametrize("company", [
        {"cif": "B00000000"},                      # the shipped placeholder
        {"cif": "B87654321"},                      # the older shipped placeholder
        {"cif": ""},                               # never filled in
        {"name": "EMPRESA DE PRUEBA, S.L."},
        {"name": "Empresa de ejemplo"},
        {"name": ""},
    ])
    def test_placeholders_are_caught(self, tmp_path, company):
        assert config_file(tmp_path, **company).is_placeholder is True

    def test_spacing_in_the_cif_does_not_hide_it(self, tmp_path):
        assert config_file(tmp_path, cif="B0000 0000").is_placeholder is True

    def test_lowercase_does_not_hide_it(self, tmp_path):
        assert config_file(tmp_path, cif="b00000000").is_placeholder is True


class TestInvoiceBanner:
    def render(self, tmp_path, config, monkeypatch) -> str:
        """Build a PDF with this config and return its text."""
        # Only needed to read a PDF back, so it stays out of requirements.txt.
        pymupdf = pytest.importorskip("pymupdf")

        from src import invoice_generator
        from src.models import InvoiceData, InvoiceItem

        monkeypatch.setattr(invoice_generator, "get_config", lambda: config)
        out = tmp_path / "invoice.pdf"
        invoice_generator.generate_invoice_pdf(
            InvoiceData(
                client_name="Cliente", client_email="c@x.es",
                items=[InvoiceItem("Servicio", 1, 100.0)],
                invoice_number="2026-0001",
            ),
            str(out),
        )
        return "".join(page.get_text() for page in pymupdf.open(str(out)))

    def test_an_unconfigured_install_stamps_every_invoice(self, tmp_path, monkeypatch):
        config = config_file(tmp_path, cif="B00000000")
        text = self.render(tmp_path, config, monkeypatch)
        assert "DOCUMENTO DE PRUEBA" in text
        assert "SIN VALOR FISCAL" in text

    def test_a_configured_install_produces_a_clean_invoice(self, tmp_path, monkeypatch):
        text = self.render(tmp_path, config_file(tmp_path), monkeypatch)
        assert "PRUEBA" not in text
        assert "Talleres Mario S.L." in text
        assert "B12345678" in text
