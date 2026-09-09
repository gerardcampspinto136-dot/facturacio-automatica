"""Regression tests for the invoice store and numbering.

These lock in the behaviour the JSON-file version had, so the move to SQLite is provably
a change of storage and not a change of meaning.
"""

from datetime import date, timedelta

import pytest

from src import store
from src.invoice_number import get_next_invoice_number, peek_invoice_number
from src.models import InvoiceData, InvoiceItem


def make_invoice(name="Talleres Mario S.L.", email="admin@talleresmario.es", **kw):
    return InvoiceData(
        client_name=name,
        client_email=email,
        items=kw.pop("items", [InvoiceItem("Reparación de fresadora", 2, 150.0)]),
        client_address=kw.pop("client_address", "Pol. Ind. Les Comes 14, Igualada"),
        client_id=kw.pop("client_id", "B12345678"),
        **kw,
    )


# ── Pending queue ────────────────────────────────────────────────────────────

class TestPending:
    def test_round_trip_preserves_every_field(self):
        original = make_invoice(notes="Urgente")
        token = store.add_pending(original, "data/invoices/draft.pdf")

        payload = store.get_pending(token)
        assert payload is not None
        assert payload["token"] == token
        assert payload["draft_path"] == "data/invoices/draft.pdf"

        inv = payload["invoice"]
        assert inv.client_name == original.client_name
        assert inv.client_email == original.client_email
        assert inv.client_address == original.client_address
        assert inv.client_id == original.client_id
        assert inv.notes == "Urgente"
        assert inv.date == original.date
        assert len(inv.items) == 1
        assert inv.items[0].description == "Reparación de fresadora"
        assert inv.items[0].quantity == 2
        assert inv.items[0].unit_price == 150.0
        assert inv.items[0].total == 300.0

    def test_pending_has_no_number_yet(self):
        token = store.add_pending(make_invoice(), "d.pdf")
        assert store.get_pending(token)["invoice"].invoice_number is None

    def test_unknown_token_returns_none(self):
        assert store.get_pending("nope") is None

    def test_list_is_oldest_first_and_count_agrees(self):
        store.add_pending(make_invoice(name="Primera"), "a.pdf")
        store.add_pending(make_invoice(name="Segunda"), "b.pdf")
        store.add_pending(make_invoice(name="Tercera"), "c.pdf")

        names = [p["invoice"].client_name for p in store.list_pending()]
        assert names == ["Primera", "Segunda", "Tercera"]
        assert store.count_pending() == 3

    def test_update_replaces_items_rather_than_appending(self):
        token = store.add_pending(make_invoice(), "d.pdf")
        edited = make_invoice(
            name="Talleres Mario S.L.",
            items=[InvoiceItem("Mano de obra", 3, 45.0), InvoiceItem("Piezas", 1, 88.5)],
        )
        store.update_pending(token, edited, draft_path="new.pdf")

        payload = store.get_pending(token)
        assert len(payload["invoice"].items) == 2
        assert payload["draft_path"] == "new.pdf"
        assert payload["invoice"].items[1].total == 88.5

    def test_update_keeps_draft_path_when_not_given(self):
        token = store.add_pending(make_invoice(), "keep.pdf")
        store.update_pending(token, make_invoice(name="Otro"))
        assert store.get_pending(token)["draft_path"] == "keep.pdf"

    def test_update_unknown_token_raises(self):
        with pytest.raises(KeyError):
            store.update_pending("nope", make_invoice())

    def test_remove_deletes_the_draft_file(self, tmp_path):
        draft = tmp_path / "draft.pdf"
        draft.write_bytes(b"%PDF-1.4")
        token = store.add_pending(make_invoice(), str(draft))

        store.remove_pending(token)

        assert store.get_pending(token) is None
        assert store.count_pending() == 0
        assert not draft.exists()

    def test_remove_is_forgiving_of_unknown_tokens(self):
        store.remove_pending("nope")  # must not raise


# ── Numbering ────────────────────────────────────────────────────────────────

class TestNumbering:
    def test_sequence_is_gap_free(self):
        year = date.today().year
        assert get_next_invoice_number() == f"{year}-0001"
        assert get_next_invoice_number() == f"{year}-0002"
        assert get_next_invoice_number() == f"{year}-0003"

    def test_series_have_independent_counters(self):
        year = date.today().year
        get_next_invoice_number()
        get_next_invoice_number()
        assert get_next_invoice_number("R") == f"R-{year}-0001"
        assert get_next_invoice_number() == f"{year}-0003"

    def test_peek_does_not_consume(self):
        year = date.today().year
        assert peek_invoice_number() == f"{year}-0001"
        assert peek_invoice_number() == f"{year}-0001"
        assert get_next_invoice_number() == f"{year}-0001"
        assert peek_invoice_number() == f"{year}-0002"

    def test_reviewing_a_draft_does_not_burn_a_number(self):
        """The point of manual mode: numbers are consumed on approval, not on capture."""
        year = date.today().year
        for i in range(5):
            store.add_pending(make_invoice(name=f"Cliente {i}"), "d.pdf")
        assert get_next_invoice_number() == f"{year}-0001"


# ── Issued record ────────────────────────────────────────────────────────────

class TestIssued:
    def test_record_and_read_back(self):
        inv = make_invoice()
        inv.invoice_number = "2026-0007"
        store.record_issued(inv)

        record = store.get_issued("2026-0007")
        assert record is not None
        assert record["invoice"].client_name == inv.client_name
        assert record["invoice"].invoice_number == "2026-0007"
        assert record["rectified_by"] is None
        assert record["issued_at"]

    def test_unknown_number_returns_none(self):
        assert store.get_issued("2026-9999") is None

    def test_list_is_newest_first(self):
        for n in ("2026-0001", "2026-0002", "2026-0003"):
            inv = make_invoice()
            inv.invoice_number = n
            store.record_issued(inv)
        numbers = [r["invoice"].invoice_number for r in store.list_issued()]
        assert numbers == ["2026-0003", "2026-0002", "2026-0001"]

    def test_mark_rectified(self):
        inv = make_invoice()
        inv.invoice_number = "2026-0001"
        store.record_issued(inv)

        store.mark_rectified("2026-0001", "R-2026-0001")
        assert store.get_issued("2026-0001")["rectified_by"] == "R-2026-0001"

    def test_mark_rectified_unknown_raises(self):
        with pytest.raises(KeyError):
            store.mark_rectified("2026-9999", "R-2026-0001")

    def test_approving_a_draft_keeps_one_row(self):
        token = store.add_pending(make_invoice(), "d.pdf")
        number = get_next_invoice_number()
        store.approve_pending(token, number)

        assert store.count_pending() == 0
        assert store.get_pending(token) is None
        record = store.get_issued(number)
        assert record is not None
        assert record["invoice"].items[0].description == "Reparación de fresadora"
        assert record["due_date"]


# ── Payments ─────────────────────────────────────────────────────────────────

class TestPayments:
    def issue(self, number, days_ago=0):
        inv = make_invoice(date=date.today() - timedelta(days=days_ago))
        inv.invoice_number = number
        store.record_issued(inv, due_days=30)
        return inv

    def test_new_invoice_is_a_receivable(self):
        self.issue("2026-0001")
        unpaid = store.list_unpaid()
        assert [u["invoice"].invoice_number for u in unpaid] == ["2026-0001"]

    def test_paying_removes_it_from_the_list(self):
        self.issue("2026-0001")
        store.mark_paid("2026-0001", amount=363.0)
        assert store.list_unpaid() == []
        assert store.get_issued("2026-0001")["paid_at"] == date.today().isoformat()

    def test_mark_unpaid_reverses_it(self):
        self.issue("2026-0001")
        store.mark_paid("2026-0001")
        store.mark_unpaid("2026-0001")
        assert len(store.list_unpaid()) == 1

    def test_overdue_only_uses_the_due_date(self):
        self.issue("2026-0001", days_ago=60)   # due 30 days ago
        self.issue("2026-0002", days_ago=5)    # not due yet

        overdue = store.list_unpaid(overdue_only=True)
        assert [o["invoice"].invoice_number for o in overdue] == ["2026-0001"]
        assert overdue[0]["days_overdue"] == 30

    def test_oldest_due_first(self):
        self.issue("2026-0001", days_ago=10)
        self.issue("2026-0002", days_ago=90)
        assert [u["invoice"].invoice_number for u in store.list_unpaid()] == [
            "2026-0002",
            "2026-0001",
        ]

    def test_credit_notes_are_not_receivables(self):
        inv = make_invoice()
        inv.invoice_number = "R-2026-0001"
        inv.rectifies = "2026-0001"
        store.record_issued(inv)
        assert store.list_unpaid() == []

    def test_cancelled_invoices_are_not_receivables(self):
        self.issue("2026-0001")
        store.mark_rectified("2026-0001", "R-2026-0001")
        assert store.list_unpaid() == []

    def test_paying_unknown_invoice_raises(self):
        with pytest.raises(KeyError):
            store.mark_paid("2026-9999")
