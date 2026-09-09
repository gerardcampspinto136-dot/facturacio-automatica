"""Tests for the product catalog, stock movements and the contact book."""

import pytest

from src import catalog, contacts
from src.models import InvoiceData, InvoiceItem


# ── Contacts ─────────────────────────────────────────────────────────────────

class TestContacts:
    def test_create_and_read_back(self):
        cid = contacts.create(
            contacts.CLIENT,
            "Talleres Mario S.L.",
            tax_id="B12345678",
            email="admin@talleresmario.es",
            payment_terms_days=60,
        )
        c = contacts.get(cid)
        assert c["name"] == "Talleres Mario S.L."
        assert c["tax_id"] == "B12345678"
        assert c["payment_terms_days"] == 60
        assert c["active"] == 1

    def test_payment_terms_default_to_thirty_days(self):
        cid = contacts.create(contacts.CLIENT, "Sin condiciones")
        assert contacts.get(cid)["payment_terms_days"] == 30

    def test_name_is_required(self):
        with pytest.raises(ValueError):
            contacts.create(contacts.CLIENT, "   ")

    def test_kind_is_validated(self):
        with pytest.raises(ValueError):
            contacts.create("employee", "Alguien")

    def test_clients_and_suppliers_are_listed_separately(self):
        contacts.create(contacts.CLIENT, "Cliente A")
        contacts.create(contacts.SUPPLIER, "Proveedor B")

        assert [c["name"] for c in contacts.list_all(contacts.CLIENT)] == ["Cliente A"]
        assert [c["name"] for c in contacts.list_all(contacts.SUPPLIER)] == ["Proveedor B"]
        assert len(contacts.list_all()) == 2

    def test_same_tax_id_can_be_client_and_supplier(self):
        contacts.create(contacts.CLIENT, "Empresa X", tax_id="B99999999")
        contacts.create(contacts.SUPPLIER, "Empresa X", tax_id="B99999999")
        assert len(contacts.list_all()) == 2

    def test_duplicate_tax_id_within_a_kind_is_rejected(self):
        import sqlite3

        contacts.create(contacts.CLIENT, "Empresa X", tax_id="B99999999")
        with pytest.raises(sqlite3.IntegrityError):
            contacts.create(contacts.CLIENT, "Empresa X duplicada", tax_id="B99999999")

    def test_delete_hides_without_destroying(self):
        cid = contacts.create(contacts.CLIENT, "Antiguo cliente")
        contacts.delete(cid)
        assert contacts.list_all(contacts.CLIENT) == []
        assert contacts.get(cid)["name"] == "Antiguo cliente"
        assert len(contacts.list_all(contacts.CLIENT, include_inactive=True)) == 1

    def test_update_unknown_contact_raises(self):
        with pytest.raises(KeyError):
            contacts.update(999, email="x@y.es")


class TestContactMatching:
    def setup_contacts(self):
        contacts.create(contacts.CLIENT, "Talleres Mario S.L.", email="mario@t.es")
        contacts.create(contacts.CLIENT, "Talleres Nuria S.L.", email="nuria@t.es")
        contacts.create(contacts.CLIENT, "Fusteria Bosch")

    def test_exact_match_ignores_case(self):
        self.setup_contacts()
        assert contacts.find_by_name("fusteria bosch")["email"] is None

    def test_unique_partial_match_resolves(self):
        self.setup_contacts()
        assert contacts.find_by_name("Mario")["email"] == "mario@t.es"

    def test_ambiguous_partial_match_refuses_to_guess(self):
        self.setup_contacts()
        assert contacts.find_by_name("Talleres") is None

    def test_no_match_returns_none(self):
        self.setup_contacts()
        assert contacts.find_by_name("Nadie") is None
        assert contacts.find_by_name("") is None

    def test_find_or_create_reuses_the_match(self):
        first = contacts.create(contacts.CLIENT, "Fusteria Bosch")
        assert contacts.find_or_create(contacts.CLIENT, "Bosch") == first
        assert len(contacts.list_all(contacts.CLIENT)) == 1

    def test_find_or_create_adds_when_unknown(self):
        contacts.find_or_create(contacts.CLIENT, "Cliente nuevo", email="n@x.es")
        assert contacts.find_by_name("Cliente nuevo")["email"] == "n@x.es"


# ── Catalog ──────────────────────────────────────────────────────────────────

class TestCatalog:
    def test_create_and_read_back(self):
        pid = catalog.create(
            "Fresa de widia 8mm",
            sku="FW-8",
            unit_price=24.5,
            cost_price=11.0,
            unit="ud",
            reorder_point=5,
            stock_qty=20,
        )
        p = catalog.get(pid)
        assert p["name"] == "Fresa de widia 8mm"
        assert p["sku"] == "FW-8"
        assert p["unit_price"] == 24.5
        assert p["stock_qty"] == 20

    def test_opening_stock_is_recorded_as_a_movement(self):
        pid = catalog.create("Fresa", stock_qty=20)
        moves = catalog.history(pid)
        assert len(moves) == 1
        assert moves[0]["delta"] == 20
        assert moves[0]["reason"] == catalog.COUNT

    def test_services_ignore_stock(self):
        pid = catalog.create("Mano de obra", unit_price=45.0, track_stock=0)
        catalog.move(pid, -3, reason=catalog.SALE)
        assert catalog.get(pid)["stock_qty"] == 0
        assert catalog.history(pid) == []

    def test_name_is_required(self):
        with pytest.raises(ValueError):
            catalog.create("  ")

    def test_delete_hides_without_destroying(self):
        pid = catalog.create("Descatalogado")
        catalog.delete(pid)
        assert catalog.list_all() == []
        assert len(catalog.list_all(include_inactive=True)) == 1

    def test_find_by_sku_or_name(self):
        catalog.create("Fresa de widia 8mm", sku="FW-8")
        assert catalog.find_by_name("FW-8")["sku"] == "FW-8"
        assert catalog.find_by_name("widia")["sku"] == "FW-8"

    def test_ambiguous_product_name_refuses_to_guess(self):
        catalog.create("Fresa de widia 8mm")
        catalog.create("Fresa de widia 10mm")
        assert catalog.find_by_name("Fresa de widia") is None


class TestStock:
    def test_movements_accumulate_and_record_the_balance(self):
        pid = catalog.create("Fresa", stock_qty=10)
        assert catalog.move(pid, -3, reason=catalog.SALE, ref="2026-0001") == 7
        assert catalog.move(pid, 20, reason=catalog.PURCHASE) == 27
        assert catalog.get(pid)["stock_qty"] == 27

        moves = catalog.history(pid)
        assert [m["balance"] for m in moves] == [27, 7, 10]
        assert moves[1]["ref"] == "2026-0001"

    def test_stock_may_go_negative(self):
        pid = catalog.create("Fresa", stock_qty=1)
        assert catalog.move(pid, -4, reason=catalog.SALE) == -3

    def test_physical_count_records_the_difference(self):
        pid = catalog.create("Fresa", stock_qty=10)
        assert catalog.set_level(pid, 8, note="Recuento anual") == 8
        last = catalog.history(pid)[0]
        assert last["delta"] == -2
        assert last["note"] == "Recuento anual"

    def test_unknown_product_raises(self):
        with pytest.raises(KeyError):
            catalog.move(999, -1)

    def test_low_stock_lists_the_shortest_first(self):
        catalog.create("Casi agotada", stock_qty=1, reorder_point=10)   # -9
        catalog.create("Justo en el punto", stock_qty=5, reorder_point=5)  # 0
        catalog.create("De sobra", stock_qty=99, reorder_point=5)
        catalog.create("Sin punto de pedido", stock_qty=0, reorder_point=0)

        assert [p["name"] for p in catalog.low_stock()] == [
            "Casi agotada",
            "Justo en el punto",
        ]

    def test_stock_value_is_at_cost(self):
        catalog.create("A", stock_qty=10, cost_price=2.5)
        catalog.create("B", stock_qty=4, cost_price=10.0)
        catalog.create("Servicio", track_stock=0, cost_price=99.0)
        assert catalog.stock_value() == 65.0


class TestInvoiceStockLink:
    def invoice(self, *items):
        return InvoiceData(
            client_name="Talleres Mario",
            client_email="m@t.es",
            items=list(items),
            invoice_number="2026-0001",
        )

    def test_selling_decrements_matched_lines_only(self):
        fresa = catalog.create("Fresa de widia 8mm", stock_qty=10)
        catalog.apply_invoice(
            self.invoice(
                InvoiceItem("Fresa de widia 8mm", 3, 24.5),
                InvoiceItem("Desplazamiento a taller", 1, 40.0),
            )
        )
        assert catalog.get(fresa)["stock_qty"] == 7
        assert catalog.history(fresa)[0]["ref"] == "2026-0001"

    def test_returns_the_products_that_fell_low(self):
        catalog.create("Fresa de widia 8mm", stock_qty=10, reorder_point=8)
        catalog.create("Broca HSS 5mm", stock_qty=50, reorder_point=8)

        low = catalog.apply_invoice(
            self.invoice(
                InvoiceItem("Fresa de widia 8mm", 3, 24.5),
                InvoiceItem("Broca HSS 5mm", 1, 3.0),
            )
        )
        assert [p["name"] for p in low] == ["Fresa de widia 8mm"]

    def test_rectifying_puts_the_stock_back(self):
        fresa = catalog.create("Fresa de widia 8mm", stock_qty=10)
        sale = self.invoice(InvoiceItem("Fresa de widia 8mm", 3, 24.5))
        catalog.apply_invoice(sale)
        catalog.apply_invoice(sale, ref="R-2026-0001", sign=1)
        assert catalog.get(fresa)["stock_qty"] == 10

    def test_unmatched_invoice_reports_nothing(self):
        assert catalog.apply_invoice(self.invoice(InvoiceItem("Consultoría", 1, 500.0))) == []
