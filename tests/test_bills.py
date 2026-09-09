"""Supplier bills, the money owed out, and the digest that reports on both directions."""

from datetime import date, timedelta

import pytest

from src import bills, contacts, notify, store
from src.models import InvoiceData, InvoiceItem


def days_ago(n):
    return date.today() - timedelta(days=n)


def in_days(n):
    return date.today() + timedelta(days=n)


class TestCreatingBills:
    def test_minimal_bill_needs_only_a_name_and_an_amount(self):
        bid = bills.create("Ferretería Puig", 242.0)
        b = bills.get(bid)
        assert b["supplier_name"] == "Ferretería Puig"
        assert b["total"] == 242.0
        assert b["paid"] is False
        assert b["due_date"] == in_days(30).isoformat()

    def test_unknown_supplier_is_added_to_the_contact_book(self):
        bills.create("Ferretería Puig", 100.0)
        assert contacts.find_by_name("Ferretería Puig", contacts.SUPPLIER) is not None

    def test_existing_supplier_is_reused_not_duplicated(self):
        sid = contacts.create(contacts.SUPPLIER, "Ferretería Puig S.L.")
        bid = bills.create("Puig", 100.0)
        assert bills.get(bid)["supplier_id"] == sid
        assert bills.get(bid)["supplier_name"] == "Ferretería Puig S.L."
        assert len(contacts.list_all(contacts.SUPPLIER)) == 1

    def test_due_date_follows_the_supplier_payment_terms(self):
        contacts.create(contacts.SUPPLIER, "Suministros Vall", payment_terms_days=60)
        bid = bills.create("Suministros Vall", 500.0)
        assert bills.get(bid)["due_date"] == in_days(60).isoformat()

    def test_explicit_due_date_wins(self):
        bid = bills.create("Ferretería Puig", 100.0, due_date=in_days(3))
        assert bills.get(bid)["due_date"] == in_days(3).isoformat()

    def test_tax_is_derived_from_whichever_half_is_given(self):
        a = bills.create("Proveedor A", 121.0, subtotal=100.0)
        assert bills.get(a)["tax_amount"] == 21.0

        b = bills.create("Proveedor B", 121.0, tax_amount=21.0)
        assert bills.get(b)["subtotal"] == 100.0

    def test_link_supplier_can_be_declined_for_one_off_purchases(self):
        bid = bills.create("Gasolinera cualquiera", 60.0, link_supplier=False)
        assert bills.get(bid)["supplier_id"] is None
        assert contacts.list_all(contacts.SUPPLIER) == []

    def test_supplier_name_is_required(self):
        with pytest.raises(ValueError):
            bills.create("   ", 100.0)


class TestPayingBills:
    def test_marking_paid_and_back(self):
        bid = bills.create("Ferretería Puig", 100.0)
        bills.mark_paid(bid)
        assert bills.get(bid)["paid"] is True
        assert bills.get(bid)["paid_at"] == date.today().isoformat()

        bills.mark_unpaid(bid)
        assert bills.get(bid)["paid"] is False

    def test_paid_bills_leave_the_unpaid_list(self):
        a = bills.create("Proveedor A", 100.0)
        bills.create("Proveedor B", 200.0)
        bills.mark_paid(a)
        assert [b["supplier_name"] for b in bills.list_all(unpaid_only=True)] == ["Proveedor B"]

    def test_unknown_bill_raises(self):
        with pytest.raises(KeyError):
            bills.mark_paid(999)
        with pytest.raises(KeyError):
            bills.update(999, notes="x")

    def test_delete_removes_it(self):
        bid = bills.create("Error de tecleo", 9999.0)
        bills.delete(bid)
        assert bills.get(bid) is None


class TestDueSoon:
    def setup_bills(self):
        bills.create("Vencida", 100.0, bill_date=days_ago(40), due_date=days_ago(10))
        bills.create("Hoy", 200.0, due_date=date.today())
        bills.create("Esta semana", 300.0, due_date=in_days(5))
        bills.create("El mes que viene", 400.0, due_date=in_days(45))

    def test_includes_overdue_and_the_next_week_only(self):
        self.setup_bills()
        names = [b["supplier_name"] for b in bills.due_soon(within_days=7)]
        assert names == ["Vencida", "Hoy", "Esta semana"]

    def test_flags_overdue_and_counts_the_days(self):
        self.setup_bills()
        due = {b["supplier_name"]: b for b in bills.due_soon()}
        assert due["Vencida"]["overdue"] is True
        assert due["Vencida"]["days_until_due"] == -10
        assert due["Hoy"]["overdue"] is False
        assert due["Hoy"]["days_until_due"] == 0

    def test_paid_bills_are_never_due(self):
        bid = bills.create("Vencida pero pagada", 100.0, due_date=days_ago(10))
        bills.mark_paid(bid)
        assert bills.due_soon() == []

    def test_soonest_first(self):
        self.setup_bills()
        assert bills.due_soon(within_days=60)[0]["supplier_name"] == "Vencida"


class TestTotals:
    def test_total_owed_counts_only_unpaid(self):
        bills.create("A", 100.0)
        bills.create("B", 250.5)
        paid = bills.create("C", 999.0)
        bills.mark_paid(paid)
        assert bills.total_owed() == 350.5

    def test_total_overdue_is_a_subset(self):
        bills.create("Vencida", 100.0, due_date=days_ago(5))
        bills.create("Futura", 250.0, due_date=in_days(20))
        assert bills.total_owed() == 350.0
        assert bills.total_owed(overdue_only=True) == 100.0

    def test_grouped_by_supplier_biggest_first(self):
        bills.create("Ferretería Puig", 100.0)
        bills.create("Ferretería Puig", 50.0)
        bills.create("Suministros Vall", 400.0)

        grouped = bills.by_supplier()
        assert grouped[0]["supplier_name"] == "Suministros Vall"
        assert grouped[0]["amount"] == 400.0
        assert grouped[1]["bills"] == 2
        assert grouped[1]["amount"] == 150.0

    def test_nothing_owed_is_zero_not_none(self):
        assert bills.total_owed() == 0


class TestMoneyDigest:
    def issue_unpaid(self, number, days_ago_issued=0):
        inv = InvoiceData(
            client_name="Talleres Mario S.L.",
            client_email="m@t.es",
            items=[InvoiceItem("Reparación", 1, 300.0)],
            date=days_ago(days_ago_issued),
        )
        inv.invoice_number = number
        store.record_issued(inv, due_days=30)

    def test_silent_when_there_is_nothing_to_say(self):
        assert notify.build_money_digest([], []) is None

    def test_reports_bills_with_their_urgency(self):
        bills.create("Ferretería Puig", 242.0, due_date=days_ago(3), reference="F-2026/88")
        bills.create("Suministros Vall", 100.0, due_date=date.today())

        text = notify.build_money_digest(bills.due_soon(), [])
        assert "PAGOS A PROVEEDORES" in text
        assert "342,00" in text
        assert "F-2026/88" in text
        assert "VENCIDA hace 3 día(s)" in text
        assert "vence HOY" in text

    def test_reports_unpaid_invoices_and_their_lateness(self):
        self.issue_unpaid("2026-0001", days_ago_issued=60)
        text = notify.build_money_digest([], store.list_unpaid())
        assert "FACTURAS SIN COBRAR" in text
        assert "2026-0001" in text
        assert "30 día(s) de retraso" in text

    def test_says_so_when_nothing_is_overdue(self):
        self.issue_unpaid("2026-0001")
        text = notify.build_money_digest([], store.list_unpaid())
        assert "Ninguna vencida todavía" in text

    def test_both_directions_in_one_message(self):
        bills.create("Ferretería Puig", 242.0, due_date=date.today())
        self.issue_unpaid("2026-0001")
        text = notify.build_money_digest(bills.due_soon(), store.list_unpaid())
        assert "PAGOS A PROVEEDORES" in text
        assert "FACTURAS SIN COBRAR" in text

    def test_long_lists_are_truncated_rather_than_flooding_telegram(self):
        for i in range(14):
            bills.create(f"Proveedor {i}", 10.0, due_date=date.today())
        text = notify.build_money_digest(bills.due_soon(), [])
        assert "… y 4 más" in text

    def test_amounts_use_spanish_formatting(self):
        bills.create("Grande", 12345.6, due_date=date.today())
        text = notify.build_money_digest(bills.due_soon(), [])
        assert "12.345,60" in text
