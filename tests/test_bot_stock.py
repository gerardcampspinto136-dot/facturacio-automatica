"""The stock commands as the user actually types them into Telegram.

The handlers are driven with stand-in Update/Context objects rather than a network, so
what is tested here is the part that really breaks: pulling a price and a quantity off
the end of a product name that itself contains spaces and digits.
"""

import asyncio

import pytest

from src import bot, catalog


class FakeMessage:
    def __init__(self):
        self.replies: list[str] = []

    async def reply_text(self, text, **_kwargs):
        self.replies.append(text)
        return self

    @property
    def last(self) -> str:
        return self.replies[-1] if self.replies else ""


class FakeUpdate:
    def __init__(self):
        self.message = FakeMessage()


class FakeContext:
    def __init__(self, *args):
        self.args = list(args)


def run(handler, *args) -> FakeMessage:
    update, context = FakeUpdate(), FakeContext(*args)
    asyncio.run(handler(update, context))
    return update.message


# ── Pulling numbers off the end of a name ────────────────────────────────────

class TestArgumentParsing:
    def test_name_with_spaces_and_a_price_and_quantity(self):
        assert bot._trailing_numbers(["Tornillos", "M8", "0,25", "100"], 2) == (
            "Tornillos M8", [0.25, 100.0]
        )

    def test_a_name_ending_in_digits_keeps_them(self):
        # "Broca 10mm" is the name; only 7,80 and 30 are numbers.
        name, numbers = bot._trailing_numbers(["Broca", "10mm", "7,80", "30"], 2)
        assert name == "Broca 10mm"
        assert numbers == [7.80, 30.0]

    def test_one_number_only(self):
        assert bot._trailing_numbers(["Mano", "de", "obra", "45"], 2) == (
            "Mano de obra", [45.0]
        )

    def test_a_euro_sign_is_tolerated(self):
        assert bot._trailing_numbers(["Cable", "12€"], 2) == ("Cable", [12.0])

    def test_no_numbers_at_all(self):
        assert bot._trailing_numbers(["Tornillos", "M8"], 2) == ("Tornillos M8", [])

    def test_nothing_at_all(self):
        assert bot._trailing_numbers([], 2) == ("", [])


# ── /producto ────────────────────────────────────────────────────────────────

class TestProducto:
    def test_creating_a_product_with_opening_stock(self):
        message = run(bot.cmd_producto, "Tornillos", "M8", "0,25", "100")

        product = catalog.find_by_name("Tornillos M8")
        assert product["unit_price"] == 0.25
        assert product["stock_qty"] == 100
        assert product["track_stock"] == 1
        assert "Tornillos M8" in message.last

    def test_a_price_with_no_quantity_creates_a_service(self):
        run(bot.cmd_producto, "Mano", "de", "obra", "45")

        product = catalog.find_by_name("Mano de obra")
        assert product["track_stock"] == 0
        assert product["unit_price"] == 45.0

    def test_the_opening_stock_is_recorded_as_a_movement(self):
        run(bot.cmd_producto, "Tornillos", "M8", "0,25", "100")
        product = catalog.find_by_name("Tornillos M8")
        assert catalog.history(product["id"])[0]["delta"] == 100

    def test_a_duplicate_name_is_refused_and_points_at_entrada(self):
        catalog.create("Tornillos M8", stock_qty=10)
        message = run(bot.cmd_producto, "Tornillos", "M8", "0,25", "100")
        assert "/entrada" in message.last
        assert len(catalog.list_all()) == 1

    def test_no_arguments_lists_the_catalog(self):
        catalog.create("Tornillos M8", unit_price=0.25, stock_qty=100)
        assert "Tornillos M8" in run(bot.cmd_producto).last

    def test_no_arguments_and_no_catalog_explains_how_to_add_one(self):
        assert "/producto" in run(bot.cmd_producto).last

    def test_a_name_with_no_price_shows_the_usage(self):
        message = run(bot.cmd_producto, "Tornillos")
        assert "Uso:" in message.last
        assert catalog.list_all() == []


# ── /entrada and /salida ─────────────────────────────────────────────────────

class TestEntradaSalida:
    def test_receiving_stock_adds_to_it(self):
        pid = catalog.create("Tornillos M8", stock_qty=100)
        message = run(bot.cmd_entrada, "Tornillos", "M8", "50")

        assert catalog.get(pid)["stock_qty"] == 150
        assert "150" in message.last

    def test_receiving_is_recorded_as_a_purchase(self):
        pid = catalog.create("Tornillos M8", stock_qty=100)
        run(bot.cmd_entrada, "Tornillos", "M8", "50")
        assert catalog.history(pid)[0]["reason"] == catalog.PURCHASE

    def test_a_plural_name_finds_the_product(self):
        pid = catalog.create("Broca widia 10mm", stock_qty=30)
        run(bot.cmd_entrada, "brocas", "widia", "10mm", "20")
        assert catalog.get(pid)["stock_qty"] == 50

    def test_taking_stock_out_subtracts(self):
        pid = catalog.create("Tornillos M8", stock_qty=100)
        run(bot.cmd_salida, "Tornillos", "M8", "3")
        assert catalog.get(pid)["stock_qty"] == 97

    def test_reaching_the_reorder_point_is_flagged(self):
        catalog.create("Tornillos M8", stock_qty=25, reorder_point=20)
        message = run(bot.cmd_salida, "Tornillos", "M8", "10")
        assert "punto de pedido" in message.last

    def test_an_unknown_product_says_so_and_does_not_invent_one(self):
        message = run(bot.cmd_entrada, "Cosa", "rara", "5")
        assert "No encuentro" in message.last
        assert catalog.list_all() == []

    def test_missing_quantity_shows_the_usage(self):
        catalog.create("Tornillos M8", stock_qty=100)
        assert "Uso:" in run(bot.cmd_entrada, "Tornillos", "M8").last

    def test_a_negative_quantity_is_read_as_the_amount_received(self):
        # "/entrada X -5" almost certainly means five arrived, not minus five.
        pid = catalog.create("Tornillos M8", stock_qty=100)
        run(bot.cmd_entrada, "Tornillos", "M8", "-5")
        assert catalog.get(pid)["stock_qty"] == 105


# ── /inventario ──────────────────────────────────────────────────────────────

class TestInventario:
    def test_counting_sets_the_level_and_reports_the_difference(self):
        pid = catalog.create("Tornillos M8", stock_qty=100)
        message = run(bot.cmd_inventario, "Tornillos", "M8", "87")

        assert catalog.get(pid)["stock_qty"] == 87
        assert "-13" in message.last

    def test_the_count_is_recorded_as_a_count(self):
        pid = catalog.create("Tornillos M8", stock_qty=100)
        run(bot.cmd_inventario, "Tornillos", "M8", "87")
        assert catalog.history(pid)[0]["reason"] == catalog.COUNT


# ── /stock ───────────────────────────────────────────────────────────────────

class TestStockListing:
    def test_an_empty_catalog_explains_how_to_start(self):
        assert "/producto" in run(bot.cmd_stock).last

    def test_low_stock_is_listed_first(self):
        catalog.create("De sobra", stock_qty=99, reorder_point=5)
        catalog.create("Casi agotado", stock_qty=1, reorder_point=10)

        text = run(bot.cmd_stock).last
        assert text.index("Casi agotado") < text.index("De sobra")
        assert "Por reponer" in text

    def test_services_are_not_listed_as_stock(self):
        catalog.create("Mano de obra", track_stock=0)
        assert "Mano de obra" not in run(bot.cmd_stock).last

    def test_stock_value_is_hidden_when_no_cost_is_known(self):
        # Showing "0,00 €" over a full shelf reads like a bug.
        catalog.create("Tornillos M8", stock_qty=100, unit_price=0.25)
        assert "Valor del stock" not in run(bot.cmd_stock).last

    def test_stock_value_is_shown_once_costs_are_known(self):
        catalog.create("Tornillos M8", stock_qty=100, cost_price=0.10)
        assert "Valor del stock" in run(bot.cmd_stock).last
