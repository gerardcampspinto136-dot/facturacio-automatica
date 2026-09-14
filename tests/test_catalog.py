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

        moves = catalog.apply_invoice(
            self.invoice(
                InvoiceItem("Fresa de widia 8mm", 3, 24.5),
                InvoiceItem("Broca HSS 5mm", 1, 3.0),
            )
        )
        assert [m["name"] for m in moves if m["low"]] == ["Fresa de widia 8mm"]
        # Every movement is reported, not just the ones that fell low, so the bot can
        # tell the user what it took off.
        assert {m["name"]: m["balance"] for m in moves} == {
            "Fresa de widia 8mm": 7, "Broca HSS 5mm": 49,
        }

    def test_rectifying_puts_the_stock_back(self):
        fresa = catalog.create("Fresa de widia 8mm", stock_qty=10)
        sale = self.invoice(InvoiceItem("Fresa de widia 8mm", 3, 24.5))
        catalog.apply_invoice(sale)
        catalog.apply_invoice(sale, ref="R-2026-0001", sign=1)
        assert catalog.get(fresa)["stock_qty"] == 10

    def test_unmatched_invoice_reports_nothing(self):
        assert catalog.apply_invoice(self.invoice(InvoiceItem("Consultoría", 1, 500.0))) == []


class TestMatchingSpokenLines:
    """Finding the catalog product a dictated invoice line is talking about.

    The old matcher only worked when the spoken words were a substring of the catalog
    name, which is backwards for dictation: real lines are longer and messier than the
    product name, so stock was never deducted for anything said naturally.
    """

    def setup_products(self):
        return {
            "tornillos": catalog.create("Tornillos M8", stock_qty=100),
            "taladro": catalog.create("Taladro percutor", stock_qty=5),
            "obra": catalog.create("Mano de obra", track_stock=0),
        }

    def test_a_line_longer_than_the_product_name_still_matches(self):
        self.setup_products()
        for spoken in (
            "20 tornillos M8",
            "Tornillos inox M8 caja",
            "3 cajas de tornillos M8",
            "taladro percutor 750W",
            "Taladro percutor marca Bosch",
        ):
            assert catalog.find_in_text(spoken) is not None, spoken

    def test_accents_and_case_do_not_matter(self):
        catalog.create("Instalación eléctrica", stock_qty=3)
        assert catalog.find_in_text("INSTALACION ELECTRICA")["name"] == "Instalación eléctrica"
        assert catalog.find_in_text("instalacion electrica de la nave") is not None

    def test_the_most_specific_product_wins(self):
        catalog.create("Tornillos", stock_qty=50)
        specific = catalog.create("Tornillos M8", stock_qty=100)
        assert catalog.find_in_text("20 tornillos M8")["id"] == specific
        assert catalog.find_in_text("20 tornillos")["name"] == "Tornillos"

    def test_two_equally_specific_products_are_refused(self):
        catalog.create("Fresa widia 8mm")
        catalog.create("Fresa widia 10mm")
        assert catalog.find_in_text("Fresa widia") is None

    def test_an_unrelated_line_matches_nothing(self):
        self.setup_products()
        assert catalog.find_in_text("Desplazamiento a taller") is None
        assert catalog.find_in_text("") is None
        assert catalog.find_in_text("500") is None

    def test_a_partial_product_name_still_works(self):
        # The old behaviour has to keep working: "widia" finding "Fresa de widia 8mm".
        catalog.create("Fresa de widia 8mm", sku="FW-8")
        assert catalog.find_in_text("widia")["sku"] == "FW-8"
        assert catalog.find_in_text("FW-8")["sku"] == "FW-8"

    def test_a_number_alone_never_identifies_a_product(self):
        catalog.create("Broca 10mm", stock_qty=20)
        assert catalog.find_in_text("10") is None


class TestStockOnDictatedInvoices:
    def invoice(self, *items):
        return InvoiceData(
            client_name="Talleres Mario", client_email="m@t.es",
            items=list(items), invoice_number="2026-0001",
        )

    def test_a_naturally_dictated_line_deducts_stock(self):
        pid = catalog.create("Tornillos M8", stock_qty=100, unit_price=0.25)
        moves = catalog.apply_invoice(
            self.invoice(InvoiceItem("20 tornillos M8 inoxidables", 20, 0.25))
        )
        assert catalog.get(pid)["stock_qty"] == 80
        assert moves[0]["quantity"] == 20
        assert moves[0]["balance"] == 80
        assert moves[0]["delta"] == -20

    def test_services_are_never_touched(self):
        catalog.create("Mano de obra", track_stock=0)
        assert catalog.apply_invoice(
            self.invoice(InvoiceItem("3 horas de mano de obra", 3, 45.0))
        ) == []

    def test_rectifying_a_dictated_invoice_puts_the_stock_back(self):
        pid = catalog.create("Tornillos M8", stock_qty=100)
        sale = self.invoice(InvoiceItem("20 tornillos M8 inoxidables", 20, 0.25))
        catalog.apply_invoice(sale)
        back = catalog.apply_invoice(sale, ref="R-2026-0001", sign=1)
        assert catalog.get(pid)["stock_qty"] == 100
        assert back[0]["delta"] == 20

    def test_the_movement_is_traceable_to_the_invoice(self):
        pid = catalog.create("Tornillos M8", stock_qty=100)
        catalog.apply_invoice(self.invoice(InvoiceItem("20 tornillos M8", 20, 0.25)))
        entry = catalog.history(pid)[0]
        assert entry["ref"] == "2026-0001"
        assert entry["reason"] == catalog.SALE
        assert entry["delta"] == -20

    def test_low_stock_is_flagged_on_the_movement(self):
        catalog.create("Tornillos M8", stock_qty=25, reorder_point=10)
        moves = catalog.apply_invoice(self.invoice(InvoiceItem("20 tornillos M8", 20, 0.25)))
        assert moves[0]["low"]
        assert moves[0]["balance"] == 5

    def test_selling_more_than_there_is_still_records_the_sale(self):
        # Deliberate: a shop that sold something it had not registered is a real
        # situation, and blocking the invoice would be worse than recording it.
        pid = catalog.create("Tornillos M8", stock_qty=5)
        catalog.apply_invoice(self.invoice(InvoiceItem("20 tornillos M8", 20, 0.25)))
        assert catalog.get(pid)["stock_qty"] == -15


class TestPluralsAndMeasurements:
    """Dictation says "3 brocas"; the catalog says "Broca". That one letter used to
    stop stock being deducted entirely."""

    def test_a_plural_line_matches_a_singular_product(self):
        catalog.create("Broca widia 10mm", stock_qty=30)
        assert catalog.find_in_text("3 brocas widia de 10mm")["name"] == "Broca widia 10mm"

    def test_a_singular_line_matches_a_plural_product(self):
        catalog.create("Tornillos M8", stock_qty=100)
        assert catalog.find_in_text("un tornillo M8")["name"] == "Tornillos M8"

    def test_es_plurals_fold_too(self):
        catalog.create("Panel solar", stock_qty=4)
        assert catalog.find_in_text("2 paneles solares")["name"] == "Panel solar"

    def test_a_measurement_said_as_two_words_still_matches(self):
        catalog.create("Broca widia 10mm", stock_qty=30)
        assert catalog.find_in_text("3 brocas widia 10 mm") is not None

    def test_measurements_still_tell_two_sizes_apart(self):
        catalog.create("Broca widia 10mm", stock_qty=30)
        catalog.create("Broca widia 8mm", stock_qty=30)
        assert catalog.find_in_text("brocas widia 8mm")["name"] == "Broca widia 8mm"
        # Without a size there is no way to choose, so it refuses.
        assert catalog.find_in_text("brocas widia") is None
