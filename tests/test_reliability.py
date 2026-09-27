"""The invoice as printed, the reminders' arithmetic and timing, and the AI retries.

Each test here pins a defect that was real: amounts printed English-style, text in
angle brackets vanishing from the PDF, receivables reported without their VAT, weekly
reminders that never fired on a computer restarted daily, and a dictation lost to the
free tier's "over capacity".
"""

from datetime import date, datetime, timedelta

import pytest

from src import finalize, notify, parser, scheduler, store
from src.models import InvoiceData, InvoiceItem


def pdf_text(path) -> str:
    pymupdf = pytest.importorskip("pymupdf")
    return "".join(page.get_text() for page in pymupdf.open(str(path)))


def an_invoice(**kw):
    return InvoiceData(
        client_name=kw.pop("client_name", "Talleres Puig"),
        client_email=kw.pop("client_email", "taller@puig.es"),
        client_id=kw.pop("client_id", "B87654321"),
        items=kw.pop("items", [InvoiceItem("Reparación", 1, 1250.5)]),
        **kw,
    )


# ── The PDF ──────────────────────────────────────────────────────────────────

class TestPdf:
    def build(self, tmp_path, invoice) -> str:
        from src.invoice_generator import generate_invoice_pdf

        out = tmp_path / "f.pdf"
        generate_invoice_pdf(invoice, str(out))
        return pdf_text(out)

    def test_amounts_are_written_the_spanish_way(self, tmp_path):
        text = self.build(tmp_path, an_invoice(invoice_number="2026-0001"))
        assert "1.250,50" in text
        assert "1,250.50" not in text

    def test_angle_brackets_and_ampersands_survive(self, tmp_path):
        text = self.build(tmp_path, an_invoice(
            invoice_number="2026-0001", client_name="Pérez & Hijos <Reformas>",
            notes="Entrega <urgente> & frágil"))
        assert "Pérez & Hijos <Reformas>" in text
        assert "<urgente>" in text

    def test_a_long_description_is_kept_whole(self, tmp_path):
        long = "Instalación completa de " + "tubería de cobre y accesorios, " * 12
        text = self.build(tmp_path, an_invoice(
            invoice_number="2026-0001", items=[InvoiceItem(long, 1, 100.0)]))
        assert "Instalación completa" in text and "accesorios" in text

    def test_the_vat_rate_reads_21_not_21_point_0(self, tmp_path):
        text = self.build(tmp_path, an_invoice(invoice_number="2026-0001", tax_rate=21.0))
        assert "IVA (21%)" in text and "21.0" not in text

    def test_the_due_date_is_printed(self, tmp_path):
        inv = an_invoice(invoice_number="2026-0001",
                         due_date=date(2026, 10, 27))
        assert "Vencimiento: 27/10/2026" in self.build(tmp_path, inv)

    def test_irpf_withholding_is_printed_and_subtracted(self, tmp_path):
        text = self.build(tmp_path, an_invoice(
            invoice_number="2026-0001", items=[InvoiceItem("Consultoría", 1, 1000.0)],
            irpf_rate=15))
        assert "Retención IRPF (15%)" in text
        assert "−150,00" in text
        assert "TOTAL A PAGAR" in text and "1.060,00" in text

    def test_the_bank_line_says_iban_and_the_reference(self, tmp_path):
        text = self.build(tmp_path, an_invoice(invoice_number="2026-0007"))
        assert "IBAN" in text and "Referencia: 2026-0007" in text


# ── Rounding to the cent ─────────────────────────────────────────────────────

class TestRounding:
    def test_half_a_cent_rounds_up_like_an_accountant(self):
        from src.totals import breakdown

        # 1.255,50 x 21% is exactly 263,655: the invoice must say 263,66.
        t = breakdown(an_invoice(items=[InvoiceItem("x", 1, 1255.5)], tax_rate=21,
                                 irpf_rate=15))
        assert (t.tax, t.irpf) == (263.66, 188.33)
        assert t.total == round(1255.5 + 263.66 - 188.33, 2)

    def test_a_line_total_rounds_half_up(self):
        assert InvoiceItem("x", 3, 0.335).total == 1.01

    def test_the_printed_parts_always_add_up_to_the_total(self):
        from src.totals import breakdown

        for base in (0.05, 19.99, 333.33, 1255.5, 9999.99):
            t = breakdown(an_invoice(items=[InvoiceItem("x", 1, base)], irpf_rate=7))
            assert round(t.base + t.tax - t.irpf, 2) == t.total


# ── The money digest ─────────────────────────────────────────────────────────

class TestDigest:
    def test_what_clients_owe_includes_the_vat(self, offline):
        inv = an_invoice(items=[InvoiceItem("Servicio", 1, 100.0)],
                         date=date.today() - timedelta(days=60))
        finalize.issue(inv)
        text = notify.build_money_digest([], store.list_unpaid())
        assert "121,00" in text
        assert "100,00" not in text

    def test_irpf_withheld_is_not_owed(self, offline):
        finalize.issue(an_invoice(items=[InvoiceItem("Consultoría", 1, 1000.0)],
                                  irpf_rate=15))
        assert "1.060,00" in notify.build_money_digest([], store.list_unpaid())


# ── Riding out a busy free tier ──────────────────────────────────────────────

class _Status(Exception):
    def __init__(self, status, text="error"):
        super().__init__(text)
        self.status_code = status


class TestRetries:
    @pytest.fixture(autouse=True)
    def no_waiting(self, monkeypatch):
        monkeypatch.setattr(parser, "_sleep", lambda s: None)

    def test_busy_then_fine(self):
        calls = []

        def call(model):
            calls.append(model)
            if len(calls) < 3:
                raise _Status(503, "over capacity")
            return "ok"

        assert parser.with_retries(call, ("big", "small")) == "ok"
        assert calls == ["big", "big", "big"]

    def test_stays_busy_so_the_smaller_model_takes_it(self):
        def call(model):
            if model == "big":
                raise _Status(503, "over capacity")
            return "from small"

        assert parser.with_retries(call, ("big", "small")) == "from small"

    def test_a_withdrawn_model_is_skipped_at_once(self):
        calls = []

        def call(model):
            calls.append(model)
            if model == "big":
                raise _Status(404, "model not found")
            return "ok"

        parser.with_retries(call, ("big", "small"))
        assert calls == ["big", "small"]

    def test_a_bad_key_is_not_retried(self):
        calls = []

        def call(model):
            calls.append(model)
            raise _Status(401, "invalid api key")

        with pytest.raises(_Status):
            parser.with_retries(call, ("big", "small"))
        assert calls == ["big"]

    def test_all_busy_ends_in_a_sentence(self):
        with pytest.raises(parser.ParseError, match="saturado"):
            parser.with_retries(lambda m: (_ for _ in ()).throw(_Status(503)),
                                ("big", "small"))

    def test_a_daily_quota_says_how_long_to_wait(self):
        quota = _Status(429, "Rate limit reached. Please try again in 8m27.1s.")
        with pytest.raises(parser.ParseError, match="8 minutos"):
            parser.with_retries(lambda m: (_ for _ in ()).throw(quota), ("big",))


# ── Reminders that actually arrive ───────────────────────────────────────────

class TestSchedule:
    MORNING = datetime(2026, 9, 28, 9, 30)

    def test_a_job_that_never_ran_goes_out_in_the_morning(self):
        assert scheduler.is_due(None, timedelta(weeks=1), self.MORNING, hour=9)

    def test_never_before_the_hour(self):
        assert not scheduler.is_due(None, timedelta(days=1),
                                    self.MORNING.replace(hour=7), hour=9)

    def test_never_at_night(self):
        assert not scheduler.is_due(None, timedelta(days=1),
                                    self.MORNING.replace(hour=22), hour=9)

    def test_a_daily_job_does_not_creep_later(self):
        yesterday = self.MORNING - timedelta(days=1) + timedelta(minutes=15)
        assert scheduler.is_due(yesterday, timedelta(days=1), self.MORNING, hour=9)

    def test_a_weekly_job_waits_its_week(self):
        three_days_ago = self.MORNING - timedelta(days=3)
        assert not scheduler.is_due(three_days_ago, timedelta(weeks=1),
                                    self.MORNING, hour=9)

    def test_a_switched_off_job_never_runs(self):
        assert not scheduler.is_due(None, None, self.MORNING, hour=9)

    def test_a_computer_off_all_week_catches_up_on_monday(self, monkeypatch):
        ran = []
        monkeypatch.setattr(scheduler, "jobs", lambda: [
            ("money_digest", timedelta(weeks=1), lambda: ran.append("money")),
        ])
        scheduler.mark_run("money_digest", self.MORNING - timedelta(days=9))

        assert scheduler.tick(self.MORNING) == ["money_digest"]
        assert ran == ["money"]
        # ...and not again ten minutes later.
        assert scheduler.tick(self.MORNING + timedelta(minutes=10)) == []

    def test_the_last_run_survives_a_restart(self):
        scheduler.mark_run("low_stock", self.MORNING)
        assert scheduler.last_run("low_stock") == self.MORNING

    def test_a_failing_job_is_not_retried_all_day(self, monkeypatch):
        calls = []

        def broken():
            calls.append(1)
            raise RuntimeError("Telegram caído")

        monkeypatch.setattr(scheduler, "jobs", lambda: [
            ("money_digest", timedelta(days=1), broken)])
        scheduler.tick(self.MORNING)
        scheduler.tick(self.MORNING + timedelta(minutes=10))
        assert calls == [1]


# ── Google Sheets keeps its old columns in place ─────────────────────────────

def test_an_old_sheet_gets_the_new_headings_on_the_end():
    from src import sheets

    class FakeSheet:
        col_count = 10

        def __init__(self):
            self.updates = []

        def row_values(self, _row):
            return sheets._HEADERS[:10]

        def add_cols(self, n):
            self.col_count += n

        def update(self, values, range_name):
            self.updates.append((range_name, values))

    ws = FakeSheet()
    sheets._extend_headers(ws)
    assert ws.updates == [("K1:N1", [sheets._HEADERS[10:]])]
