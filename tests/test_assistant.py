"""The assistant: messages sorted into invoice / expense / question, and questions
answered from the books.

The model call is stubbed: the router's job is only to name the topic and the period,
and every figure in an answer is computed here and checked against known data.
"""

from datetime import date, timedelta

import pytest

from src import accounts, answers, assistant, bills, catalog, contacts, conversation
from src import finalize, receipts
from src.models import InvoiceData, InvoiceItem

TODAY = date.today()


def invoice_on(day, base, client="Talleres Puig", **kw):
    return finalize.issue(InvoiceData(
        client_name=client, client_email="t@puig.es", client_id="B87654321",
        items=[InvoiceItem("Servicio", 1, base)], date=day, **kw))


# ── Sorting without the model ────────────────────────────────────────────────

class TestQuickIntent:
    @pytest.mark.parametrize("text", [
        "Factura para Talleres Puig, 300 euros", "Presupuesto para Pere Soler",
        "Fes-me una factura per a Can Joan", "hazme la factura a Juan García de 200 euros",
    ])
    def test_invoices_skip_the_model(self, text):
        assert assistant.quick_intent(text) == "invoice"

    @pytest.mark.parametrize("text", [
        "¿Cuánto he facturado este mes?", "cuánto me debe Talleres Puig",
        "Dime qué IVA me toca", "Quants diners em deuen?",
        "He pagado 45 euros de gasolina", "hola",
    ])
    def test_questions_and_the_rest_go_to_the_model(self, text):
        # "facturado" contains "factura": a question must not become an invoice.
        assert assistant.quick_intent(text) is None


# ── Periods ──────────────────────────────────────────────────────────────────

class TestPeriods:
    def test_this_month_so_far_and_last_month_whole(self):
        p = answers.period("this_month", today=date(2026, 9, 27))
        assert (p.start, p.end) == (date(2026, 9, 1), date(2026, 9, 27))
        assert (p.previous.start, p.previous.end) == (date(2026, 8, 1), date(2026, 8, 31))

    def test_last_quarter_in_january_is_the_previous_year(self):
        p = answers.period("last_quarter", today=date(2027, 1, 15))
        assert (p.start, p.end) == (date(2026, 10, 1), date(2026, 12, 31))

    def test_custom_and_fallback(self):
        p = answers.period("custom", "2026-08-01", "2026-08-31")
        assert (p.start, p.end) == (date(2026, 8, 1), date(2026, 8, 31))
        assert answers.period("nonsense", today=date(2026, 9, 27)).start == date(2026, 1, 1)


# ── Answers from the books ───────────────────────────────────────────────────

class TestAnswers:
    def test_revenue_this_month_with_last_month_alongside(self, offline):
        first = TODAY.replace(day=1)
        invoice_on(first, 1000.0)
        invoice_on(first, 500.0, client="Bar Pepe")
        invoice_on(first - timedelta(days=1), 300.0)       # last month
        text = answers.answer({"topic": "revenue", "period": "this_month"})
        assert "1.500,00 €" in text and "1.815,00 €" in text and "2 factura" in text
        assert "300,00 €" in text

    def test_revenue_for_one_client(self, offline):
        invoice_on(TODAY, 1000.0)
        invoice_on(TODAY, 500.0, client="Bar Pepe")
        text = answers.answer({"topic": "revenue", "period": "this_year", "name": "bar pepe"})
        assert "500,00 €" in text and "1.000" not in text

    def test_a_cancelled_invoice_does_not_count(self, offline):
        from src import rectify

        result = invoice_on(TODAY, 1000.0)
        rectify.rectify(result.number)
        assert answers.answer({"topic": "revenue", "period": "this_year"}) == \
            "No has facturado nada este año."

    def test_expenses_by_category(self):
        bills.create("Repsol", 60.5, subtotal=50.0, category="transporte")
        bills.create("Adobe", 24.2, subtotal=20.0, category="software")
        text = answers.answer({"topic": "expenses", "period": "this_year",
                               "category": "transporte"})
        assert "60,50 €" in text and "10,50 € de IVA deducible" in text

    def test_asking_about_gasolina_counts_the_transport_expenses(self):
        bills.create("Repsol", 60.5, subtotal=50.0, category="transporte")
        text = answers.answer({"topic": "expenses", "period": "this_year",
                               "category": "gasolina"})
        assert "60,50 €" in text and "gastos de transporte" in text

    def test_profit(self, offline):
        invoice_on(TODAY, 1000.0)
        bills.create("Coworking", 242.0, subtotal=200.0)
        text = answers.answer({"topic": "profit", "period": "this_year"})
        assert text.startswith("Ganas 800,00 €")

    def test_who_owes_me(self, offline):
        invoice_on(TODAY - timedelta(days=50), 100.0)       # 20 days overdue
        text = answers.answer({"topic": "receivables"})
        assert "Te deben 121,00 €" in text and "20 días de retraso" in text

    def test_nobody_owes_me(self):
        assert "Nadie te debe" in answers.answer({"topic": "receivables"})

    def test_one_client(self, offline):
        contacts.create(contacts.CLIENT, "Talleres Puig", email="t@puig.es")
        invoice_on(TODAY, 1000.0)
        text = answers.answer({"topic": "client", "name": "puig"})
        assert "Talleres Puig" in text and "1.000,00 €" in text and "Te debe 1.210,00 €" in text

    def test_stock_of_one_product(self):
        catalog.create("Tornillos M8", stock_qty=87)
        assert "87 ud de Tornillos M8" in answers.answer({"topic": "stock",
                                                          "name": "tornillos m8"})

    def test_help(self):
        assert "¿Cuánto he facturado" in answers.answer({"topic": "help"})

    def test_an_employee_is_not_told_what_they_may_not_see(self, offline):
        company = accounts.create_company("Talleres Mario S.L.")
        uid = accounts.create_user("pepe@x.es", accounts.EMPLOYEE, company_id=company,
                                   permissions=["stock.view"])
        invoice_on(TODAY - timedelta(days=50), 100.0)
        text = answers.answer({"topic": "receivables"}, accounts.load_context(uid))
        assert "te falta el permiso" in text and "121" not in text


# ── Expenses said out loud ───────────────────────────────────────────────────

class TestSpokenExpenses:
    def test_no_vat_is_assumed_without_a_document(self):
        r = assistant.expense_receipt({"expense": {"supplier_name": "Repsol", "total": 45,
                                                   "category": "transporte"}})
        assert (r.total, r.subtotal, r.tax_amount) == (45.0, 45.0, 0.0)
        assert any("mándame la foto" in f for f in r.flags)

    def test_a_stated_rate_is_used(self):
        r = assistant.expense_receipt({"expense": {"supplier_name": "Bar Pepe",
                                                   "total": 22.0, "tax_rate": 10}})
        assert r.tax_amount == 2.0


# ── In the chat ──────────────────────────────────────────────────────────────

class TestChat:
    @pytest.fixture(autouse=True)
    def owner(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "5001")
        conversation._sessions.clear()
        receipts._sessions.clear()

    def route_as(self, monkeypatch, routed):
        monkeypatch.setattr(assistant, "route", lambda text, today=None: routed)

    def test_a_question_is_answered(self, offline, monkeypatch):
        from src import bot
        from test_telegram_access import say

        invoice_on(TODAY, 1000.0)
        self.route_as(monkeypatch, {"intent": "question", "topic": "revenue",
                                    "period": "this_year"})
        chat = say(bot.handle_text, 5001, text="¿Cuánto he facturado este año?")
        assert "Has facturado 1.000,00 € sin IVA este año" in chat.text

    def test_an_expense_goes_to_the_usual_confirmation(self, monkeypatch):
        from src import bot
        from test_telegram_access import press, say

        self.route_as(monkeypatch, {"intent": "expense", "expense": {
            "supplier_name": "Repsol", "total": 45, "category": "transporte"}})
        chat = say(bot.handle_text, 5001, text="He pagado 45 euros de gasolina en Repsol")
        assert "Repsol" in chat.text and "exp:save" in chat.buttons()

        press(5001, "exp:save", chat)
        assert bills.list_all()[0]["supplier_name"] == "Repsol"

    def test_something_else_gets_examples(self, monkeypatch):
        from src import bot
        from test_telegram_access import say

        self.route_as(monkeypatch, {"intent": "other"})
        assert "Por ejemplo" in say(bot.handle_text, 5001, text="hola").text

    def test_if_the_router_fails_it_is_still_an_invoice(self, monkeypatch):
        from src import bot
        from test_telegram_access import say

        def broken(text, today=None):
            raise RuntimeError("router down")

        monkeypatch.setattr(assistant, "route", broken)
        monkeypatch.setattr(bot, "parse_invoice_from_transcript", lambda t: InvoiceData(
            client_name="Juan", client_email="j@j.es", client_id="12345678Z",
            items=[InvoiceItem("Servicio", 1, 100.0)]))
        chat = say(bot.handle_text, 5001, text="cóbrale 100 euros a Juan")
        assert "Revisa la factura" in chat.text

    def test_an_employee_without_bills_permission_cannot_file_one(self, monkeypatch):
        from src import bot
        from test_telegram_access import linked_employee, say

        company = accounts.create_company("Talleres Mario S.L.")
        linked_employee(company, ["invoices.create"])
        self.route_as(monkeypatch, {"intent": "expense", "expense": {
            "supplier_name": "Repsol", "total": 45}})
        chat = say(bot.handle_text, 7002, text="He pagado 45 euros de gasolina")
        assert "No tienes permiso para anotar gastos" in chat.text
        assert bills.list_all() == []
