"""The working-time record: staff clock in and out from Telegram.

Spanish law (art. 34.9 Estatuto de los Trabajadores) requires every company with
employees to record, each day, when each one starts and finishes work; to keep the
record four years; and to show it to the workers, their representatives and the Labour
Inspectorate. The new regulation being approved requires it to be digital, reliable and
unalterable, with any correction traceable.

So:
  - an employee presses a button in Telegram (/fichar): the time is the server's,
    stamped as it happens -- nobody types a time;
  - each entry is chained to the one before by a SHA-256 fingerprint, and the table
    refuses updates and deletions (db.py triggers);
  - a forgotten clock-out is fixed by an ADDED correction: who made it, when, why.
    The day's hours use it; the record shows it for what it is.

Breaks (pausas) are recorded too and not counted as work time.
"""

import hashlib
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Optional

from src import db

IN, OUT, BREAK_START, BREAK_END = "in", "out", "break_start", "break_end"
LABELS = {IN: "Entrada", OUT: "Salida", BREAK_START: "Inicio de pausa",
          BREAK_END: "Fin de pausa"}


def _now() -> datetime:
    return datetime.now().astimezone().replace(microsecond=0)


def _fingerprint(previous: str, user_id: int, kind: str, at: str, source: str,
                 note: str, corrected_by, reason: str) -> str:
    text = (f"previous={previous}&user={user_id}&kind={kind}&at={at}&source={source}"
            f"&note={note or ''}&corrected_by={corrected_by or ''}&reason={reason or ''}")
    return hashlib.sha256(text.encode("utf-8")).hexdigest().upper()


def _record(user_id: int, kind: str, at: datetime, source: str, note: Optional[str] = None,
            corrected_by: Optional[int] = None, reason: Optional[str] = None) -> dict:
    stamp = at.isoformat(timespec="seconds")
    with db.transaction() as conn:
        row = conn.execute("SELECT hash FROM time_entries ORDER BY id DESC LIMIT 1").fetchone()
        previous = row["hash"] if row else ""
        digest = _fingerprint(previous, user_id, kind, stamp, source, note, corrected_by,
                              reason)
        cur = conn.execute(
            "INSERT INTO time_entries (user_id, kind, at, source, note, corrected_by, "
            "reason, previous_hash, hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (user_id, kind, stamp, source, note, corrected_by, reason, previous, digest))
    return {"id": cur.lastrowid, "kind": kind, "at": stamp}


def entries(user_id: int, start: date, end: date) -> list[dict]:
    rows = db.connect().execute(
        "SELECT * FROM time_entries WHERE user_id = ? AND substr(at, 1, 10) BETWEEN ? AND ? "
        "ORDER BY at, id", (user_id, start.isoformat(), end.isoformat())).fetchall()
    return [dict(r) for r in rows]


def last_entry(user_id: int) -> Optional[dict]:
    row = db.connect().execute(
        "SELECT * FROM time_entries WHERE user_id = ? ORDER BY at DESC, id DESC LIMIT 1",
        (user_id,)).fetchone()
    return dict(row) if row else None


def _left_open(last: Optional[dict]) -> Optional[date]:
    """The day of an entry left open on an earlier day (a forgotten clock-out)."""
    if last is None or last["kind"] == OUT:
        return None
    day = datetime.fromisoformat(last["at"]).date()
    return day if day < _now().date() else None


def state(user_id: int) -> str:
    """"out", "working" or "on_break" -- what the next button should offer.

    A day left open yesterday does not keep anyone "working" today: that would lock
    them out of clocking in this morning. The open day stays flagged for correcting.
    """
    last = last_entry(user_id)
    if last is None or last["kind"] == OUT or _left_open(last):
        return "out"
    return "on_break" if last["kind"] == BREAK_START else "working"


class ClockError(Exception):
    """A clock action that makes no sense now, explained for the chat."""


def clock(user_id: int, kind: str, source: str = "telegram",
          note: Optional[str] = None) -> dict:
    """Record a clock event now, if it follows from the current state."""
    current = state(user_id)
    allowed = {"out": {IN}, "working": {OUT, BREAK_START}, "on_break": {BREAK_END, OUT}}
    if kind not in allowed[current]:
        raise ClockError({
            "out": "No has empezado la jornada: pulsa «Empezar».",
            "working": "Ya estás trabajando.",
            "on_break": "Estás en pausa: primero «Volver de la pausa».",
        }[current])
    if kind == OUT and current == "on_break":
        _record(user_id, BREAK_END, _now(), source)
    forgotten = _left_open(last_entry(user_id)) if kind == IN else None
    entry = _record(user_id, kind, _now(), source, note)
    if forgotten:
        entry["warning"] = (f"El {forgotten.strftime('%d/%m')} no fichaste la salida: "
                            "pide a tu responsable que la corrija.")
    return entry


def correct(user_id: int, kind: str, at: datetime, by_user_id: int, reason: str) -> dict:
    """Add a correction (a forgotten entry), recorded as such -- never an edit."""
    if not (reason or "").strip():
        raise ClockError("Una corrección necesita un motivo.")
    if at.tzinfo is None:
        at = at.astimezone()      # a time typed in a form: this machine's local time
    if at > _now():
        raise ClockError("No se puede fichar en el futuro.")
    return _record(user_id, kind, at.replace(microsecond=0), "correction",
                   corrected_by=by_user_id, reason=reason.strip())


# ── Days and hours ───────────────────────────────────────────────────────────

@dataclass
class Day:
    day: date
    first_in: Optional[datetime] = None
    last_out: Optional[datetime] = None
    worked: timedelta = timedelta()
    breaks: timedelta = timedelta()
    open: bool = False                      # clocked in and never out
    corrected: bool = False
    events: list = field(default_factory=list)

    @property
    def hours(self) -> float:
        return round(self.worked.total_seconds() / 3600, 2)


def days(user_id: int, start: date, end: date, now: Optional[datetime] = None) -> list[Day]:
    """Each day's first entry, last exit, time worked (breaks excluded) and breaks."""
    now = now or _now()
    by_day: dict = {}
    for entry in entries(user_id, start, end):
        at = datetime.fromisoformat(entry["at"])
        by_day.setdefault(at.date(), []).append((at, entry))

    out = []
    for day in sorted(by_day):
        record = Day(day)
        working_since = break_since = None
        for at, entry in sorted(by_day[day], key=lambda p: p[0]):
            record.events.append(entry)
            record.corrected = record.corrected or entry["source"] == "correction"
            kind = entry["kind"]
            if kind == IN:
                record.first_in = record.first_in or at
                working_since = at
            elif kind == BREAK_START and working_since:
                record.worked += at - working_since
                working_since, break_since = None, at
            elif kind == BREAK_END:
                if break_since:
                    record.breaks += at - break_since
                break_since, working_since = None, at
            elif kind == OUT:
                if working_since:
                    record.worked += at - working_since
                if break_since:
                    record.breaks += at - break_since
                working_since = break_since = None
                record.last_out = at
        if working_since is not None:
            record.open = True
            if day == now.date():
                record.worked += now - working_since     # today, still at work
        out.append(record)
    return out


def hhmm(hours: float) -> str:
    minutes = round(hours * 60)
    return f"{minutes // 60}:{minutes % 60:02d}"


def verify_chain() -> tuple[bool, list[str]]:
    problems, previous = [], ""
    for row in db.connect().execute("SELECT * FROM time_entries ORDER BY id").fetchall():
        r = dict(row)
        if r["previous_hash"] != previous:
            problems.append(f"El fichaje {r['id']} no enlaza con el anterior.")
        expected = _fingerprint(r["previous_hash"], r["user_id"], r["kind"], r["at"],
                                r["source"], r["note"], r["corrected_by"], r["reason"])
        if expected != r["hash"]:
            problems.append(f"El fichaje {r['id']} no corresponde a su huella.")
        previous = r["hash"]
    return not problems, problems


def open_since(before: datetime) -> list[dict]:
    """Who clocked in earlier than `before` and has not clocked out: forgot, probably."""
    rows = db.connect().execute(
        "SELECT t.* FROM time_entries t JOIN (SELECT user_id, MAX(id) AS id FROM "
        "time_entries GROUP BY user_id) last ON last.id = t.id "
        "WHERE t.kind IN ('in', 'break_start', 'break_end') AND t.at < ?",
        (before.isoformat(timespec="seconds"),)).fetchall()
    return [dict(r) for r in rows]


def remind_forgotten(now: Optional[datetime] = None) -> list[int]:
    """The evening nudge: "you are still clocked in -- did you finish?". Returns users."""
    from src import accounts, telegram_api

    now = now or _now()
    reminded = []
    for entry in open_since(now - timedelta(hours=9)):
        user = accounts.get_user(entry["user_id"])
        if user and user.get("telegram_id"):
            telegram_api.send_message(
                user["telegram_id"],
                "⏰ Sigues con la jornada abierta desde las "
                f"{datetime.fromisoformat(entry['at']).strftime('%H:%M')}. "
                "¿Has terminado? Pulsa para fichar la salida.",
                [("🔴 Terminar la jornada", "clock:out")])
            reminded.append(user["id"])
    return reminded


# ── The monthly report ───────────────────────────────────────────────────────

def month_bounds(year: int, month: int) -> tuple[date, date]:
    first = date(year, month, 1)
    last = date(year + (month == 12), month % 12 + 1, 1) - timedelta(days=1)
    return first, last


def monthly_pdf(user_id: int, year: int, month: int, path: str) -> str:
    """The month for one employee, day by day, with space for both signatures."""
    from pathlib import Path
    from xml.sax.saxutils import escape

    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import cm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    from src import accounts
    from src.config_loader import get_config

    config = get_config()
    user = accounts.get_user(user_id)
    start, end = month_bounds(year, month)
    styles = getSampleStyleSheet()
    rows = [["Día", "Entrada", "Salida", "Pausas", "Horas", ""]]
    total = 0.0
    weekdays = ("lun", "mar", "mié", "jue", "vie", "sáb", "dom")
    for d in days(user_id, start, end):
        total += d.hours
        rows.append([
            f"{weekdays[d.day.weekday()]} {d.day.strftime('%d/%m')}",
            d.first_in.strftime("%H:%M") if d.first_in else "—",
            d.last_out.strftime("%H:%M") if d.last_out else ("abierta" if d.open else "—"),
            hhmm(d.breaks.total_seconds() / 3600) if d.breaks else "—",
            hhmm(d.hours),
            "corregido" if d.corrected else "",
        ])
    rows.append(["", "", "", "Total", hhmm(total), ""])

    Path(path).parent.mkdir(parents=True, exist_ok=True)
    doc = SimpleDocTemplate(path, pagesize=A4, leftMargin=2 * cm, rightMargin=2 * cm,
                            topMargin=2 * cm, bottomMargin=2 * cm)
    table = Table(rows, colWidths=[3.2 * cm, 2.4 * cm, 2.4 * cm, 2.4 * cm, 2.4 * cm, 3 * cm],
                  repeatRows=1)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1a3a5c")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("GRID", (0, 0), (-1, -2), 0.4, colors.HexColor("#d0d7de")),
        ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
        ("ALIGN", (1, 0), (-2, -1), "CENTER"),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
    ]))
    months = ("enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto",
              "septiembre", "octubre", "noviembre", "diciembre")
    who = escape(user.get("name") or user["email"])
    doc.build([
        Paragraph(f"<b>Registro de jornada — {months[month - 1]} de {year}</b>",
                  styles["Title"]),
        Paragraph(f"Empresa: {escape(config.name)} (NIF {escape(config.cif)})<br/>"
                  f"Trabajador/a: {who} ({escape(user['email'])})", styles["Normal"]),
        Spacer(1, 0.5 * cm), table, Spacer(1, 0.4 * cm),
        Paragraph("Registro diario de jornada conforme al art. 34.9 del Estatuto de los "
                  "Trabajadores. Las horas se fichan en el momento y el registro no se "
                  "puede modificar; las correcciones quedan anotadas con su motivo. Se "
                  "conserva cuatro años.", styles["Italic"]),
        Spacer(1, 1.6 * cm),
        Table([["Firma de la empresa", "Firma del trabajador/a"]],
              colWidths=[8.5 * cm, 8.5 * cm]),
    ])
    return path
