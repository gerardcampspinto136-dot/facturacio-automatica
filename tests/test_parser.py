"""Parser provider selection and the arithmetic applied to whatever the model returns.

These never call an API: the point is the code around the model, which is where a wrong
number would quietly reach a real invoice.
"""

import json

import pytest

from src import parser
from src.models import InvoiceData


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for key in ("LLM_PROVIDER", "ANTHROPIC_API_KEY", "GROQ_API_KEY"):
        monkeypatch.delenv(key, raising=False)


def fake_response(monkeypatch, payload):
    """Make the Groq path return `payload` without touching the network."""
    monkeypatch.setenv("GROQ_API_KEY", "test")
    monkeypatch.setattr(
        parser, "_complete_groq",
        lambda t: payload if isinstance(payload, str) else json.dumps(payload),
    )


class TestProviderChoice:
    def test_anthropic_preferred_when_its_key_is_present(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-x")
        monkeypatch.setenv("GROQ_API_KEY", "gsk-x")
        assert parser._which_provider() == "anthropic"

    def test_falls_back_to_groq_when_only_groq_is_configured(self, monkeypatch):
        monkeypatch.setenv("GROQ_API_KEY", "gsk-x")
        assert parser._which_provider() == "groq"

    def test_llm_provider_forces_the_choice(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-x")
        monkeypatch.setenv("GROQ_API_KEY", "gsk-x")
        monkeypatch.setenv("LLM_PROVIDER", "groq")
        assert parser._which_provider() == "groq"

    def test_forcing_is_case_insensitive(self, monkeypatch):
        monkeypatch.setenv("GROQ_API_KEY", "gsk-x")
        monkeypatch.setenv("LLM_PROVIDER", "GROQ")
        assert parser._which_provider() == "groq"

    def test_no_key_at_all_says_what_to_do(self, monkeypatch):
        with pytest.raises(RuntimeError, match="GROQ_API_KEY"):
            parser._which_provider()


class TestNumberCoercion:
    @pytest.mark.parametrize("raw,expected", [
        (150, 150.0),
        (24.5, 24.5),
        ("24.5", 24.5),
        ("24,5", 24.5),               # Spanish decimal comma
        ("1.250,50", 1250.5),         # Spanish thousands separator
        ("1250.50", 1250.5),
        ("45 €", 45.0),
        ("€45", 45.0),
        ("-30", -30.0),
        (None, 0.0),
        ("", 0.0),
        ("no aplica", 0.0),
    ])
    def test_coercion(self, raw, expected):
        assert parser._number(raw) == expected

    def test_default_is_returned_for_missing_values(self):
        assert parser._number(None, 1) == 1


class TestJsonExtraction:
    def test_plain_json(self):
        assert parser._extract_json('{"a": 1}', "groq") == {"a": 1}

    def test_json_wrapped_in_prose_or_fences(self):
        text = 'Here you go:\n```json\n{"a": 1}\n```\nHope that helps.'
        assert parser._extract_json(text, "groq") == {"a": 1}

    def test_no_json_names_the_provider(self):
        with pytest.raises(ValueError, match="groq"):
            parser._extract_json("I could not do that", "groq")


class TestItemArithmetic:
    def test_hours_times_rate_wins_over_a_wrong_total(self, monkeypatch):
        fake_response(monkeypatch, {
            "client_name": "Juan García",
            "client_email": "juan@ejemplo.es",
            "items": [{"description": "Trabajo", "hours": 3, "rate": 50,
                       "quantity": 1, "unit_price": 0, "total": 999}],
        })
        inv = parser.parse_invoice_from_transcript("...")
        assert (inv.items[0].quantity, inv.items[0].unit_price, inv.items[0].total) == (3, 50, 150)

    def test_total_is_derived_when_only_a_unit_price_is_given(self, monkeypatch):
        fake_response(monkeypatch, {
            "client_name": "X", "client_email": "x@y.es",
            "items": [{"description": "Fresa", "quantity": 2, "unit_price": 24.5, "total": 0}],
        })
        assert parser.parse_invoice_from_transcript("...").items[0].total == 49.0

    def test_unit_price_is_derived_when_only_a_total_is_given(self, monkeypatch):
        fake_response(monkeypatch, {
            "client_name": "X", "client_email": "x@y.es",
            "items": [{"description": "Piezas", "quantity": 4, "unit_price": 0, "total": 100}],
        })
        assert parser.parse_invoice_from_transcript("...").items[0].unit_price == 25.0

    def test_spanish_formatted_strings_survive(self, monkeypatch):
        fake_response(monkeypatch, {
            "client_name": "X", "client_email": "x@y.es",
            "items": [{"description": "Reforma", "quantity": 1,
                       "unit_price": "1.250,50", "total": "1.250,50"}],
        })
        assert parser.parse_invoice_from_transcript("...").subtotal == 1250.5

    def test_quantity_zero_never_divides_by_zero(self, monkeypatch):
        fake_response(monkeypatch, {
            "client_name": "X", "client_email": "x@y.es",
            "items": [{"description": "Raro", "quantity": 0, "unit_price": 0, "total": 80}],
        })
        assert parser.parse_invoice_from_transcript("...").items[0].total == 80


class TestInvoiceShape:
    def test_full_mapping(self, monkeypatch):
        fake_response(monkeypatch, {
            "client_name": "Talleres Mario S.L.",
            "client_email": "admin@talleresmario.es",
            "client_address": "Pol. Ind. Les Comes 14",
            "client_id": "B12345678",
            "notes": "Urgente",
            "items": [{"description": "Reparación", "quantity": 2, "unit_price": 150, "total": 300}],
        })
        inv = parser.parse_invoice_from_transcript("...")
        assert isinstance(inv, InvoiceData)
        assert inv.client_name == "Talleres Mario S.L."
        assert inv.client_id == "B12345678"
        assert inv.notes == "Urgente"
        assert inv.subtotal == 300.0

    def test_missing_optional_fields_become_none_not_the_string_none(self, monkeypatch):
        fake_response(monkeypatch, {"client_name": "X", "client_email": "x@y.es", "items": []})
        inv = parser.parse_invoice_from_transcript("...")
        assert inv.client_address is None
        assert inv.client_id is None
        assert inv.items == []

    def test_nulls_do_not_become_empty_strings(self, monkeypatch):
        fake_response(monkeypatch, {
            "client_name": None, "client_email": None, "client_address": None, "items": [],
        })
        inv = parser.parse_invoice_from_transcript("...")
        assert inv.client_name == ""
        assert inv.client_email == ""

    def test_item_without_a_description_gets_a_usable_default(self, monkeypatch):
        fake_response(monkeypatch, {
            "client_name": "X", "client_email": "x@y.es",
            "items": [{"quantity": 1, "unit_price": 50, "total": 50}],
        })
        assert parser.parse_invoice_from_transcript("...").items[0].description == "Servicio"

    def test_model_prose_around_the_json_is_tolerated(self, monkeypatch):
        fake_response(monkeypatch,
                      'Claro:\n{"client_name": "X", "client_email": "x@y.es", "items": []}\nListo.')
        assert parser.parse_invoice_from_transcript("...").client_name == "X"
