"""The working-time record: clock in and out, never edited, corrections traceable.

Times are the server's, so the tests move a fake clock rather than typing times --
exactly as an employee cannot choose the time they clock in at.
"""

import sqlite3
from datetime import date, datetime, timedelta

import pytest

from src import accounts, db, timeclock


@pytest.fixture
def company():
    return accounts.create_company("Talleres Mario S.L.", tax_id="B12345678")


@pytest.fixture
def pepe(company):
    return accounts.create_user("pepe@talleres.es", accounts.EMPLOYEE, company_id=company,
                                name="Pepe García", permissions=["invoices.view"])


@pytest.fixture
def clock_at(monkeypatch):
    """Set the server's clock: clock_at(8, 0) is 08:00 today."""
    def set_time(hour, minute=0, day=None):
        moment = datetime.combine(day or date.today(),
                                  datetime.min.time()).replace(hour=hour, minute=minute)
        monkeypatch.setattr(timeclock, "_now", lambda: moment.astimezone())
        return moment
    return set_time


class TestClocking:
    def test_a_normal_day_with_a_lunch_break(self, pepe, clock_at):
        clock_at(8, 0);   timeclock.clock(pepe, timeclock.IN)
        clock_at(14, 0);  timeclock.clock(pepe, timeclock.BREAK_START)
        clock_at(15, 0);  timeclock.clock(pepe, timeclock.BREAK_END)
        clock_at(18, 30); timeclock.clock(pepe, timeclock.OUT)

        day = timeclock.days(pepe, date.today(), date.today())[0]
        assert (day.first_in.hour, day.last_out.hour, day.last_out.minute) == (8, 18, 30)
        assert day.hours == 9.5 and day.breaks == timedelta(hours=1)
        assert timeclock.hhmm(day.hours) == "9:30"

    def test_only_what_makes_sense_now(self, pepe, clock_at):
        clock_at(8)
        with pytest.raises(timeclock.ClockError, match="No has empezado"):
            timeclock.clock(pepe, timeclock.OUT)
        timeclock.clock(pepe, timeclock.IN)
        with pytest.raises(timeclock.ClockError, match="Ya estás trabajando"):
            timeclock.clock(pepe, timeclock.IN)

    def test_finishing_during_a_break_closes_the_break(self, pepe, clock_at):
        clock_at(8);  timeclock.clock(pepe, timeclock.IN)
        clock_at(12); timeclock.clock(pepe, timeclock.BREAK_START)
        clock_at(13); timeclock.clock(pepe, timeclock.OUT)
        day = timeclock.days(pepe, date.today(), date.today())[0]
        assert day.hours == 4.0 and not day.open

    def test_forgetting_to_clock_out_does_not_lock_you_out_the_next_day(self, pepe,
                                                                        clock_at):
        yesterday = date.today() - timedelta(days=1)
        clock_at(8, day=yesterday)
        timeclock.clock(pepe, timeclock.IN)            # ...and never clocked out
        clock_at(8)
        assert timeclock.state(pepe) == "out"
        entry = timeclock.clock(pepe, timeclock.IN)
        assert "no fichaste la salida" in entry["warning"]
        assert timeclock.days(pepe, yesterday, yesterday)[0].open

    def test_a_day_still_open_counts_until_now(self, pepe, clock_at):
        clock_at(8); timeclock.clock(pepe, timeclock.IN)
        now = clock_at(11)
        day = timeclock.days(pepe, date.today(), date.today(), now.astimezone())[0]
        assert day.open and day.hours == 3.0


class TestUnalterable:
    def test_an_entry_cannot_be_edited_or_deleted(self, pepe, clock_at):
        clock_at(8); timeclock.clock(pepe, timeclock.IN)
        for sql in ("UPDATE time_entries SET at = '2020-01-01T00:00:00'",
                    "DELETE FROM time_entries"):
            with pytest.raises(sqlite3.DatabaseError):
                with db.transaction() as conn:
                    conn.execute(sql)

    def test_the_chain_holds_and_catches_tampering(self, pepe, clock_at):
        clock_at(8); timeclock.clock(pepe, timeclock.IN)
        clock_at(17); timeclock.clock(pepe, timeclock.OUT)
        assert timeclock.verify_chain() == (True, [])

        conn = db.connect()
        conn.execute("DROP TRIGGER trg_time_frozen")
        conn.execute("UPDATE time_entries SET at = replace(at, 'T17', 'T19')")
        intact, problems = timeclock.verify_chain()
        assert not intact and problems

    def test_a_forgotten_exit_is_corrected_not_rewritten(self, company, pepe, clock_at):
        boss = accounts.create_user("jefe@talleres.es", accounts.ADMIN, company_id=company)
        clock_at(8, day=date.today() - timedelta(days=1))
        timeclock.clock(pepe, timeclock.IN)
        clock_at(9)
        forgotten = datetime.combine(date.today() - timedelta(days=1),
                                     datetime.min.time()).replace(hour=17)
        timeclock.correct(pepe, timeclock.OUT, forgotten, boss, "olvidó fichar la salida")

        yesterday = date.today() - timedelta(days=1)
        day = timeclock.days(pepe, yesterday, yesterday)[0]
        assert day.hours == 9.0 and day.corrected
        correction = timeclock.entries(pepe, yesterday, yesterday)[-1]
        assert (correction["corrected_by"], correction["reason"]) == \
            (boss, "olvidó fichar la salida")

    def test_a_correction_needs_a_reason_and_cannot_be_in_the_future(self, company, pepe,
                                                                     clock_at):
        clock_at(9)
        with pytest.raises(timeclock.ClockError, match="motivo"):
            timeclock.correct(pepe, timeclock.OUT, datetime.now() - timedelta(hours=1),
                              pepe, "")
        with pytest.raises(timeclock.ClockError, match="futuro"):
            timeclock.correct(pepe, timeclock.OUT, datetime.now() + timedelta(days=1),
                              pepe, "x")


class TestReminderAndReport:
    def test_the_evening_nudge_for_a_forgotten_exit(self, pepe, clock_at, telegram_outbox):
        accounts.link_telegram(accounts.create_telegram_code(pepe), 7002)
        clock_at(8); timeclock.clock(pepe, timeclock.IN)
        now = clock_at(20)
        assert timeclock.remind_forgotten(now.astimezone()) == [pepe]
        sent = [p for m, p in telegram_outbox if m == "sendMessage"]
        assert sent[-1]["chat_id"] == 7002 and "clock:out" in str(sent[-1])

    def test_the_monthly_pdf(self, pepe, clock_at, tmp_path):
        pymupdf = pytest.importorskip("pymupdf")
        clock_at(8); timeclock.clock(pepe, timeclock.IN)
        clock_at(16); timeclock.clock(pepe, timeclock.OUT)
        path = timeclock.monthly_pdf(pepe, date.today().year, date.today().month,
                                     str(tmp_path / "r.pdf"))
        text = pymupdf.open(path)[0].get_text()
        assert "Registro de jornada" in text and "Pepe García" in text
        assert "8:00" in text and "Firma del trabajador" in text


class TestSurfaces:
    def test_fichar_in_the_chat(self, pepe, clock_at, monkeypatch):
        from src import bot
        from test_telegram_access import press, say

        accounts.link_telegram(accounts.create_telegram_code(pepe), 7002)
        chat = say(bot.cmd_fichar, 7002)
        assert "No estás fichado" in chat.text and "clock:in" in chat.buttons()

        clock_at(8, 3)
        after = press(7002, "clock:in")
        assert "Entrada fichada a las 08:03" in after.text
        assert "clock:out" in after.buttons()

    def test_the_owner_chat_is_told_to_link_an_account(self, monkeypatch):
        from src import bot
        from test_telegram_access import say

        monkeypatch.setenv("TELEGRAM_CHAT_ID", "5001")
        assert "tu propia cuenta" in say(bot.cmd_fichar, 5001).text

    def test_the_web_page_an_employee_sees_only_their_own(self, company, pepe, clock_at,
                                                          monkeypatch):
        import base64
        import json

        import itsdangerous
        from fastapi.testclient import TestClient

        monkeypatch.delenv("WEB_DEV_NO_AUTH", raising=False)
        monkeypatch.setenv("SESSION_SECRET", "test-secret")
        from src.web import app as web_app

        other = accounts.create_user("ana@talleres.es", accounts.EMPLOYEE,
                                     company_id=company, name="Ana")
        clock_at(8); timeclock.clock(other, timeclock.IN)
        client = TestClient(web_app.app, follow_redirects=False)
        signer = itsdangerous.TimestampSigner("test-secret")
        data = base64.b64encode(json.dumps({"user": "pepe@talleres.es"}).encode())
        client.cookies.set("session", signer.sign(data).decode())

        page = client.get(f"/timeclock?who={other}").text     # asks for Ana's
        assert "Pepe García" in page and "Ana" not in page.split("page-sub")[1][:80]

        client.post("/timeclock/clock", data={"kind": "in"})
        assert timeclock.state(pepe) == "working"
