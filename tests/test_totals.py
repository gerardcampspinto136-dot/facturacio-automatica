"""VAT arithmetic and the checklist that decides an invoice is ready."""

import pytest

from src import checklist
from src.config_loader import get_config
from src.models import InvoiceData, InvoiceItem
from src.totals import (
    compute_totals,
    format_money,
    net_from_gross,
    normalize_prices,
    prices_are_inclusive,
)


def invoice(*items, **kw):
    return InvoiceData(
        client_name=kw.pop("client_name", "Talleres Puig"),
        client_email=kw.pop("client_email", "taller@tallerespuig.es"),
        client_id=kw.pop("client_id", "B12345678"),
        items=list(items) or [InvoiceItem("Reparación", 1, 100.0)],
        **kw,
    )


@pytest.fixture
def cfg():
    return get_config()


class TestVatDirection:
    def test_exclusive_adds_on_top(self, cfg):
        inv = invoice(InvoiceItem("Reparación", 1, 100.0), prices_include_tax=False)
        normalize_prices(inv, cfg)
        assert compute_totals(inv, cfg) == (100.0, 21.0, 121.0)

    def test_inclusive_strips_it_out(self, cfg):
        inv = invoice(InvoiceItem("Reparación", 1, 121.0), prices_include_tax=True)
        normalize_prices(inv, cfg)
        assert compute_totals(inv, cfg) == (100.0, 21.0, 121.0)

    def test_silence_follows_the_company_default(self, cfg):
        inv = invoice(prices_include_tax=None)
        assert prices_are_inclusive(inv, cfg) is bool(cfg.prices_include_tax)

    def test_what_the_speaker_said_beats_the_default(self, cfg):
        inv = invoice(prices_include_tax=True)
        assert prices_are_inclusive(inv, cfg) is True
        inv = invoice(prices_include_tax=False)
        assert prices_are_inclusive(inv, cfg) is False

    def test_net_from_gross(self):
        assert net_from_gross(121.0, 21) == 100.0
        assert net_from_gross(150.0, 21) == 123.97


class TestNormalisation:
    def test_running_twice_does_not_convert_twice(self, cfg):
        inv = invoice(InvoiceItem("Reparación", 1, 121.0), prices_include_tax=True)
        normalize_prices(inv, cfg)
        first = compute_totals(inv, cfg)
        normalize_prices(inv, cfg)
        assert compute_totals(inv, cfg) == first

    def test_inclusive_lines_still_total_the_quoted_gross(self, cfg):
        """Three lines of 60 must come back to exactly 180, not 179.99."""
        inv = invoice(
            InvoiceItem("Montaje", 1, 60.0),
            InvoiceItem("Desplazamiento", 1, 60.0),
            InvoiceItem("Material", 1, 60.0),
            prices_include_tax=True,
        )
        normalize_prices(inv, cfg)
        assert compute_totals(inv, cfg)[2] == 180.0

    def test_unit_price_stays_consistent_with_the_line_total(self, cfg):
        inv = invoice(InvoiceItem("Puertas", 2, 121.0, 242.0), prices_include_tax=True)
        normalize_prices(inv, cfg)
        item = inv.items[0]
        assert round(item.unit_price * item.quantity, 2) == item.total

    def test_empty_invoice_is_left_alone(self, cfg):
        inv = InvoiceData(client_name="X", client_email="x@y.es", items=[])
        normalize_prices(inv, cfg)
        assert inv.items == []


class TestFormatting:
    def test_spanish_thousands_and_decimals(self, cfg):
        assert format_money(1250.5, cfg).startswith("1.250,50")
        assert format_money(0.0, cfg).startswith("0,00")


class TestChecklist:
    def test_complete_invoice_needs_nothing(self):
        assert checklist.missing_fields(invoice()) == []

    def test_missing_email_is_reported(self):
        assert "client_email" in checklist.missing_fields(invoice(client_email=""))

    def test_missing_tax_id_is_reported(self):
        assert "client_id" in checklist.missing_fields(invoice(client_id=None))

    def test_missing_name_is_reported(self):
        assert "client_name" in checklist.missing_fields(invoice(client_name="  "))

    def test_no_items_is_reported(self):
        inv = invoice()
        inv.items = []
        assert "items" in checklist.missing_fields(inv)

    def test_zero_value_items_count_as_missing(self):
        assert "items" in checklist.missing_fields(invoice(InvoiceItem("Nada", 1, 0.0)))

    def test_name_is_asked_before_email(self):
        inv = invoice(client_name="", client_email="")
        assert checklist.next_question(inv)[0] == "client_name"

    @pytest.mark.parametrize("value,ok", [
        ("juan@ejemplo.es", True),
        ("juan.perez@sub.ejemplo.co.uk", True),
        ("no me acuerdo", False),
        ("juan@", False),
        ("@ejemplo.es", False),
        ("juan@ejemplo", False),
        ("", False),
        (None, False),
    ])
    def test_email_validation(self, value, ok):
        assert checklist.valid_email(value) is ok

    @pytest.mark.parametrize("value,ok", [
        ("12345678A", True),
        ("B12345678", True),
        ("X1234567L", True),
        ("pepito", False),
        ("123", False),
        ("", False),
        (None, False),
    ])
    def test_tax_id_validation(self, value, ok):
        assert checklist.valid_tax_id(value) is ok


class TestAnswers:
    def test_dictated_email_is_reassembled(self):
        inv = invoice(client_email="")
        ok, _ = checklist.apply_answer(inv, "client_email", "taller arroba tallerespuig punto es")
        assert ok and inv.client_email == "taller@tallerespuig.es"

    def test_email_inside_a_sentence_is_found(self):
        inv = invoice(client_email="")
        ok, _ = checklist.apply_answer(inv, "client_email", "Mi correo es juan@ejemplo.es, gracias")
        assert ok and inv.client_email == "juan@ejemplo.es"

    def test_catalan_dictation(self):
        inv = invoice(client_email="")
        ok, _ = checklist.apply_answer(inv, "client_email", "info arrova fusteria punt cat")
        assert ok and inv.client_email == "info@fusteria.cat"

    def test_rubbish_email_is_refused_with_an_explanation(self):
        inv = invoice(client_email="")
        ok, complaint = checklist.apply_answer(inv, "client_email", "no me acuerdo")
        assert not ok and "válido" in complaint

    def test_tax_id_is_normalised(self):
        inv = invoice(client_id=None)
        ok, _ = checklist.apply_answer(inv, "client_id", "b-12345678")
        assert ok and inv.client_id == "B12345678"

    def test_empty_answer_is_refused(self):
        inv = invoice()
        ok, complaint = checklist.apply_answer(inv, "client_email", "   ")
        assert not ok and complaint


class TestTaxIdIsPermissive:
    """A Spanish NIF is the common case, not the only legitimate one.

    Refusing anything that is not a Spanish NIF blocks every foreign client, and blocked
    a real test with the id "1234Z". The rule is now: accept anything that is plausibly
    an identifier, and warn when it does not look Spanish.
    """

    @pytest.mark.parametrize("value", [
        "1234Z", "12345678A", "B12345678", "X1234567L",
        "FR12345678901", "IE1234567T", "DE123456789",
    ])
    def test_plausible_identifiers_are_accepted(self, value):
        assert checklist.valid_tax_id(value)

    @pytest.mark.parametrize("value", ["pepito", "no me acuerdo", "123", "", None, "ABC"])
    def test_non_identifiers_are_still_refused(self, value):
        assert not checklist.valid_tax_id(value)

    @pytest.mark.parametrize("value,spanish", [
        ("12345678A", True), ("B12345678", True), ("X1234567L", True),
        ("1234Z", False), ("FR12345678901", False),
    ])
    def test_spanish_shape_is_detected_for_warning_only(self, value, spanish):
        assert checklist.looks_spanish_tax_id(value) is spanish

    def test_client_with_no_tax_id_can_say_so(self):
        inv = invoice(client_id=None)
        ok, _ = checklist.apply_answer(inv, "client_id", "no tiene")
        assert ok and inv.client_id == "SIN NIF"

    def test_saying_no_tax_id_satisfies_the_checklist(self):
        inv = invoice(client_id=None)
        checklist.apply_answer(inv, "client_id", "no tiene")
        assert "client_id" not in checklist.missing_fields(inv)

    def test_the_complaint_explains_the_alternatives(self):
        inv = invoice(client_id=None)
        ok, complaint = checklist.apply_answer(inv, "client_id", "pepito")
        assert not ok
        assert "IVA" in complaint and "no tiene" in complaint
