"""The invoicing dialogue: what it asks for, what it refuses, and what it remembers.

The model is stubbed here so the flow is tested deterministically; the same scenarios are
also run against the real model by the stress script.
"""

import pytest

from src import checklist, contacts, conversation
from src.conversation import AWAIT_CONFIRM, AWAIT_CONTACT, Session
from src.models import InvoiceData, InvoiceItem


def invoice(**kw):
    return InvoiceData(
        client_name=kw.pop("client_name", "Talleres Puig"),
        client_email=kw.pop("client_email", ""),
        client_id=kw.pop("client_id", None),
        items=kw.pop("items", [InvoiceItem("Reparación", 1, 300.0)]),
        **kw,
    )


def texts(replies):
    return " ".join(r.text for r in replies).lower()


class TestYesNo:
    @pytest.mark.parametrize("text", [
        "sí", "si", "vale", "correcto", "envíala", "sí, envíala",
        "la factura es correcta, envíala", "perfecto, adelante", "ok",
    ])
    def test_agreement(self, text):
        assert conversation.says_yes(text)

    @pytest.mark.parametrize("text", [
        "no", "no, déjalo", "cancela", "no lo envíes", "para", "descártala",
    ])
    def test_refusal(self, text):
        assert not conversation.says_yes(text)
        assert conversation.says_no(text)

    def test_a_no_containing_a_yes_word_is_still_a_no(self):
        # "no lo envíes" contains "envíes"; it must not read as approval.
        assert not conversation.says_yes("no lo envíes")


class TestChecklistDialogue:
    def test_incomplete_invoice_is_not_presented_for_sending(self):
        s = Session()
        s.start(invoice())
        assert s.awaiting == "client_email"

    def test_questions_come_one_at_a_time(self):
        s = Session()
        s.start(invoice())
        assert s.awaiting == "client_email"
        s.handle_text("taller@tallerespuig.es")
        assert s.awaiting == "client_id"
        s.handle_text("B12345678")
        assert s.awaiting == AWAIT_CONFIRM

    def test_a_bad_answer_repeats_the_same_question(self):
        s = Session()
        s.start(invoice())
        replies = s.handle_text("no me acuerdo")
        assert s.awaiting == "client_email"
        assert "email" in texts(replies)

    def test_complete_invoice_goes_straight_to_confirmation(self):
        s = Session()
        s.start(invoice(client_email="taller@tallerespuig.es", client_id="B12345678"))
        assert s.awaiting == AWAIT_CONFIRM

    def test_the_summary_shows_the_figures(self):
        s = Session()
        replies = s.start(
            invoice(client_email="taller@tallerespuig.es", client_id="B12345678")
        )
        body = texts(replies)
        assert "base imponible" in body and "iva" in body and "total" in body

    def test_confirmation_offers_send_and_discard(self):
        s = Session()
        replies = s.start(
            invoice(client_email="taller@tallerespuig.es", client_id="B12345678")
        )
        actions = {data for _, data in replies[-1].buttons}
        assert {"approve", "cancel", "toggle_tax"} <= actions

    def test_cancelling_says_nothing_was_sent(self):
        s = Session()
        s.start(invoice(client_email="t@t.es", client_id="B12345678"))
        replies = s.cancel()
        assert not s.active
        assert "no se ha enviado" in texts(replies)

    def test_talking_with_no_invoice_in_progress_is_handled(self):
        assert "no hay ninguna factura" in texts(Session().handle_text("sí"))


class TestStoredClients:
    def test_a_known_client_fills_in_the_blanks(self):
        contacts.create(contacts.CLIENT, "Talleres Puig",
                        email="taller@tallerespuig.es", tax_id="B12345678",
                        address="Pol. Les Comes 14")
        s = Session()
        s.start(invoice())
        assert s.awaiting == AWAIT_CONFIRM
        assert s.invoice.client_email == "taller@tallerespuig.es"
        assert s.invoice.client_id == "B12345678"
        assert s.invoice.contact_id is not None

    def test_dictated_details_are_not_overwritten_by_the_stored_ones(self):
        contacts.create(contacts.CLIENT, "Talleres Puig", email="viejo@tallerespuig.es",
                        tax_id="B12345678")
        s = Session()
        s.start(invoice(client_email="nuevo@tallerespuig.es"))
        assert s.invoice.client_email == "nuevo@tallerespuig.es"

    def test_an_unknown_client_is_saved_after_issuing(self):
        s = Session()
        s.start(invoice(client_email="taller@tallerespuig.es", client_id="B12345678"))
        assert s.contact_is_new()
        s.save_contact()
        stored = contacts.find_by_name("Talleres Puig")
        assert stored and stored["email"] == "taller@tallerespuig.es"

    def test_a_known_client_is_not_saved_twice(self):
        contacts.create(contacts.CLIENT, "Talleres Puig",
                        email="taller@tallerespuig.es", tax_id="B12345678")
        s = Session()
        s.start(invoice())
        assert not s.contact_is_new()
        assert s.save_contact() is None
        assert len(contacts.list_all(contacts.CLIENT)) == 1


class TestDuplicateNames:
    def setup_two(self):
        a = contacts.create(contacts.CLIENT, "Gerard Camps", tax_id="12345678A",
                            email="gerard@empresa-a.es")
        b = contacts.create(contacts.CLIENT, "Gerard Camps", tax_id="87654321B",
                            email="gerard@empresa-b.es")
        return a, b

    def test_it_asks_instead_of_guessing(self):
        self.setup_two()
        s = Session()
        replies = s.start(invoice(client_name="Gerard Camps"))
        assert s.awaiting == AWAIT_CONTACT
        assert "gerard@empresa-a.es" in texts(replies)
        assert "gerard@empresa-b.es" in texts(replies)

    def test_each_candidate_gets_a_button(self):
        self.setup_two()
        s = Session()
        replies = s.start(invoice(client_name="Gerard Camps"))
        assert len(replies[0].buttons) == 2

    def test_choosing_by_number(self):
        self.setup_two()
        s = Session()
        s.start(invoice(client_name="Gerard Camps"))
        s.handle_text("2")
        assert s.invoice.client_email == "gerard@empresa-b.es"

    def test_choosing_by_button(self):
        a, _ = self.setup_two()
        s = Session()
        s.start(invoice(client_name="Gerard Camps"))
        s.pick_contact(a)
        assert s.invoice.client_email == "gerard@empresa-a.es"

    def test_an_unclear_choice_asks_again(self):
        self.setup_two()
        s = Session()
        s.start(invoice(client_name="Gerard Camps"))
        s.handle_text("el de siempre")
        assert s.awaiting == AWAIT_CONTACT

    def test_out_of_range_number_asks_again(self):
        self.setup_two()
        s = Session()
        s.start(invoice(client_name="Gerard Camps"))
        s.handle_text("7")
        assert s.awaiting == AWAIT_CONTACT

    def test_one_exact_match_beats_partial_ones(self):
        contacts.create(contacts.CLIENT, "Bosch")
        contacts.create(contacts.CLIENT, "Fusteria Bosch Germans")
        assert len(contacts.find_candidates("Bosch")) == 1


class TestVatToggle:
    def make(self):
        s = Session()
        s.start(invoice(
            client_email="t@t.es", client_id="B12345678",
            items=[InvoiceItem("Reparación", 1, 121.0)], prices_include_tax=True,
        ))
        return s

    def test_toggling_off_and_on_is_lossless(self):
        from src.totals import compute_totals

        s = self.make()
        before = compute_totals(s.invoice)
        s.toggle_tax()
        s.toggle_tax()
        after = compute_totals(s.invoice)
        assert all(abs(a - b) < 0.02 for a, b in zip(before, after))

    def test_toggling_returns_to_the_confirmation_step(self):
        s = self.make()
        s.toggle_tax()
        assert s.awaiting == AWAIT_CONFIRM
