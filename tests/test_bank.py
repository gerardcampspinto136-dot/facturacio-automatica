"""Bank reconciliation: statements in, invoices and bills marked paid.

The statements are built here in the shapes banks really produce: Norma 43 records laid
out by the Cuaderno 43 positions, a CSV with the account details above the header and
Spanish number formats, and an Excel with separate charge and credit columns.
"""

import asyncio
from datetime import date, timedelta
from pathlib import Path

import pytest

from src import bank, bills, finalize, store
from src.models import InvoiceData, InvoiceItem

TODAY = date.today()


def invoice(base, client="Talleres Puig S.L.", days_ago=10, **kw):
    return finalize.issue(InvoiceData(
        client_name=client, client_email="x@x.es", client_id=kw.pop("nif", "B87654321"),
        items=[InvoiceItem("Servicio", 1, base)],
        date=TODAY - timedelta(days=days_ago), **kw))


def n43(movements) -> str:
    """A Norma 43 file: header 11, a 22 (+ optional 23) per movement, 33 and 88."""
    def yymmdd(d):
        return d.strftime("%y%m%d")

    lines = ["11" + "2100" + "0001" + "0200012345" + yymmdd(TODAY) + yymmdd(TODAY)
             + "2" + "00000000100000" + "978" + "3" + "TALLERES MARIO SL".ljust(26) + "   "]
    for day, amount, ref, extra in movements:
        sign = "2" if amount > 0 else "1"
        line = ("22" + "    " + "0001" + yymmdd(day) + yymmdd(day) + "02" + "099" + sign
                + f"{round(abs(amount) * 100):014d}" + "0000000000"
                + ref[:12].ljust(12) + ref[12:28].ljust(16))
        assert len(line) == 80
        lines.append(line)
        if extra:
            lines.append(("23" + "01" + extra[:38].ljust(38) + extra[38:76].ljust(38)))
    lines.append("33" + " " * 78)
    lines.append("88" + "9" * 18 + " " * 60)
    return "\r\n".join(lines) + "\r\n"


@pytest.fixture
def statement(tmp_path):
    def write(name, content, binary=False):
        path = tmp_path / name
        if binary:
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="latin-1")
        return str(path)
    return write


# ── Reading the formats ──────────────────────────────────────────────────────

class TestFormats:
    def test_norma_43(self, statement):
        path = statement("extracto.n43", n43([
            (TODAY, 363.00, "TRANSFERENCIA", "DE TALLERES PUIG SL FRA 2026-0001"),
            (TODAY, -84.70, "RECIBO ENDESA", "ENDESA ENERGIA SAU"),
        ]))
        moves = bank.parse_file(path)
        assert [(m.amount, m.date) for m in moves] == [(363.0, TODAY), (-84.7, TODAY)]
        assert "TALLERES PUIG" in moves[0].description

    def test_a_csv_with_the_account_details_above_the_header(self, statement):
        path = statement("movimientos.csv", (
            "Cuenta;ES21 0081 0298 1100 0123 4567\n"
            "Titular;TALLERES MARIO SL\n\n"
            "Fecha;Fecha valor;Movimiento;Más datos;Importe;Saldo\n"
            f"{TODAY:%d/%m/%Y};{TODAY:%d/%m/%Y};TRANSF DE TALLERES PUIG;FRA 2026-0001;"
            "1.363,50;2.500,00\n"
            f"{TODAY:%d/%m/%Y};{TODAY:%d/%m/%Y};RECIBO TELEFONICA;;-45,99;1.136,50\n"))
        moves = bank.parse_file(path)
        assert [m.amount for m in moves] == [1363.5, -45.99]
        assert "FRA 2026-0001" in moves[0].description and moves[0].balance == 2500.0

    def test_an_excel_with_charge_and_credit_columns(self, statement, tmp_path):
        from openpyxl import Workbook

        wb = Workbook()
        ws = wb.active
        ws.append(["Extracto de cuenta"])
        ws.append(["F. Operación", "Concepto", "Cargo", "Abono", "Saldo"])
        ws.append([TODAY, "TRANSFERENCIA BAR PEPE", None, 121.0, 900.0])
        ws.append([TODAY, "COMISION MANTENIMIENTO", 12.0, None, 888.0])
        path = tmp_path / "extracto.xlsx"
        wb.save(path)
        moves = bank.parse_file(str(path))
        assert [m.amount for m in moves] == [121.0, -12.0]

    def test_something_that_is_not_a_statement(self, statement):
        path = statement("notas.txt", "hola\nesto no es un extracto\n")
        with pytest.raises(bank.StatementError):
            bank.parse_file(path)

    def test_the_old_xls_says_how_to_convert_it(self, statement):
        path = statement("viejo.xls", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\0" * 64, True)
        with pytest.raises(bank.StatementError, match=".xlsx"):
            bank.parse_file(path)


# ── Matching ─────────────────────────────────────────────────────────────────

class TestMatching:
    def test_the_invoice_number_in_the_transfer_marks_it_paid(self, offline, statement):
        number = invoice(300.0).number                         # 363,00 to pay
        report = bank.import_and_reconcile(statement("e.n43", n43([
            (TODAY, 363.0, "TRANSFERENCIA", f"FACTURA {number}")])))
        assert len(report.applied) == 1
        assert store.get_issued(number)["paid_at"] == TODAY.isoformat()

    def test_the_client_name_and_a_unique_amount_are_enough(self, offline, statement):
        number = invoice(300.0).number
        report = bank.import_and_reconcile(statement("e.n43", n43([
            (TODAY, 363.0, "TRANSFERENCIA", "DE TALLERES PUIG SL")])))
        assert report.applied and store.get_issued(number)["paid_at"]

    def test_the_amount_alone_is_only_a_proposal(self, offline, statement):
        number = invoice(300.0).number
        report = bank.import_and_reconcile(statement("e.n43", n43([
            (TODAY, 363.0, "INGRESO", "EFECTIVO")])))
        assert report.proposed and not report.applied
        assert store.get_issued(number)["paid_at"] is None

    def test_two_invoices_of_the_same_amount_the_number_decides(self, offline, statement):
        first = invoice(300.0).number
        second = invoice(300.0, client="Bar Pepe").number
        bank.import_and_reconcile(statement("e.n43", n43([
            (TODAY, 363.0, "TRANSF", f"PAGO {second}")])))
        assert store.get_issued(second)["paid_at"] and not store.get_issued(first)["paid_at"]

    def test_a_payment_before_the_invoice_existed_is_not_it(self, offline, statement):
        invoice(300.0, days_ago=0)
        report = bank.import_and_reconcile(statement("e.n43", n43([
            (TODAY - timedelta(days=5), 363.0, "TRANSF", "DE TALLERES PUIG SL")])))
        assert not report.applied and report.unmatched_in

    def test_a_supplier_bill_is_paid_by_its_charge(self, statement):
        bill_id = bills.create("Ferretería Puig", 242.61, reference="F-2026/88")
        bank.import_and_reconcile(statement("e.n43", n43([
            (TODAY, -242.61, "TRANSF A", "FERRETERIA PUIG F-2026/88")])))
        assert bills.get(bill_id)["paid"]

    def test_importing_the_same_statement_twice_adds_nothing(self, offline, statement):
        content = n43([(TODAY, 50.0, "INGRESO", "VARIOS")])
        first = bank.import_and_reconcile(statement("a.n43", content))
        second = bank.import_and_reconcile(statement("b.n43", content))
        assert (first.added, second.added, second.duplicates) == (1, 0, 1)


# ── Charges nobody recorded ──────────────────────────────────────────────────

class TestMissingExpenses:
    def test_a_charge_with_no_bill_is_listed_and_can_be_filed(self, statement):
        report = bank.import_and_reconcile(statement("e.n43", n43([
            (TODAY, -84.70, "RECIBO ENDESA", "ENDESA ENERGIA SAU 1234567")])))
        movement = report.unmatched_out[0]
        bill_id = bank.record_as_expense(movement.id, category="suministros")
        bill = bills.get(bill_id)
        assert (bill["total"], bill["paid"], bill["category"]) == (84.70, True, "suministros")
        assert bank.pending_movements() == []

    def test_a_first_guess_at_the_supplier(self):
        assert bank.guess_supplier("RECIBO ENDESA ENERGIA SAU 1234567") == \
            "Endesa Energia Sau"
        assert bank.guess_supplier("COMPRA TARJ. 5402XXXX REPSOL ESTACION") == \
            "Repsol Estacion"


# ── From the panel and the chat ──────────────────────────────────────────────

class TestSurfaces:
    def test_upload_and_confirm_on_the_web(self, offline, statement, monkeypatch):
        import base64
        import json

        import itsdangerous
        from fastapi.testclient import TestClient

        from src import accounts

        monkeypatch.delenv("WEB_DEV_NO_AUTH", raising=False)
        monkeypatch.setenv("SESSION_SECRET", "test-secret")
        from src.web import app as web_app

        company = accounts.create_company("Talleres Mario S.L.")
        accounts.create_user("jefe@talleres.es", accounts.ADMIN, company_id=company)
        client = TestClient(web_app.app, follow_redirects=False)
        signer = itsdangerous.TimestampSigner("test-secret")
        data = base64.b64encode(json.dumps({"user": "jefe@talleres.es"}).encode())
        client.cookies.set("session", signer.sign(data).decode())

        number = invoice(300.0).number
        content = n43([(TODAY, 363.0, "INGRESO", "EFECTIVO")]).encode("latin-1")
        response = client.post("/bank/import", files={"statement": ("e.n43", content)})
        assert response.status_code == 303
        page = client.get("/bank").text
        assert "¿Es esto?" in page and number in page

        movement = bank.pending_movements()[0]
        client.post(f"/bank/{movement.id}/apply", data={"kind": "invoice", "ref": number})
        assert store.get_issued(number)["paid_at"]

    def test_send_the_statement_to_the_bot(self, offline, statement, monkeypatch):
        from types import SimpleNamespace

        from src import bot
        from test_telegram_access import Chat, Message, press, update_for

        monkeypatch.setenv("TELEGRAM_CHAT_ID", "5001")
        number = invoice(300.0).number
        path = statement("e.n43", n43([(TODAY, 363.0, "INGRESO", "EFECTIVO")]))

        class File:
            async def download_to_drive(self, target):
                Path(target).write_bytes(Path(path).read_bytes())

        async def get_file(file_id):
            return File()

        chat = Chat()
        update = update_for(5001, chat)
        update.message.document = SimpleNamespace(file_name="extracto.n43", file_id="x")
        context = SimpleNamespace(args=[], bot=SimpleNamespace(get_file=get_file))
        asyncio.run(bot.handle_document(update, context))

        assert "Extracto importado" in chat.text and "por confirmar" in chat.text
        confirm = next(b for b in chat.buttons() if b.startswith("bank:ok:"))
        press(5001, confirm)
        assert store.get_issued(number)["paid_at"]
