"""Bank reconciliation: the statement marks what was paid, so nobody has to.

Every week someone opens the bank, reads down the movements and ticks invoices as paid
one by one -- or doesn't, and the reminders chase clients who already paid. Here the
statement is uploaded (or sent to the bot) and:

  money in   is matched to an unpaid invoice: same amount, and the invoice number or
             the client's name in the transfer's text -> marked paid on its own; same
             amount only -> proposed, for a human to confirm;
  money out  is matched to an unpaid supplier bill the same way;
  the rest   is listed -- and a charge with no bill behind it is usually an expense
             nobody recorded (the phone, the insurance, the bank's fees): one click
             files it, so its VAT and cost reach the quarter's returns.

Formats: Norma 43 (the Spanish banks' standard statement file, "Cuaderno 43") and the
CSV or Excel export every bank's website offers, whatever its columns are called.
Importing the same statement twice adds nothing: each movement is fingerprinted.
"""

import csv
import hashlib
import io
import logging
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Optional

from src import db, store

logger = logging.getLogger(__name__)


class StatementError(Exception):
    """A file that cannot be read as a statement, explained for the person."""


@dataclass
class Movement:
    date: date
    amount: float                # positive: money in; negative: money out
    description: str
    value_date: Optional[date] = None
    balance: Optional[float] = None
    id: Optional[int] = None

    @property
    def fingerprint(self) -> str:
        key = f"{self.date}|{self.amount:.2f}|{_fold(self.description)}|{self.balance}"
        return hashlib.sha1(key.encode("utf-8")).hexdigest()


def _fold(text: str) -> str:
    """Lowercase, no accents, single spaces: how descriptions are compared."""
    decomposed = unicodedata.normalize("NFD", (text or "").lower())
    plain = "".join(c for c in decomposed if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", plain).strip()


# ── Reading statements ───────────────────────────────────────────────────────

def _n43_date(text: str) -> date:
    return date(2000 + int(text[0:2]), int(text[2:4]), int(text[4:6]))


def parse_norma43(content: str) -> list[Movement]:
    """Norma 43: fixed-width 80-character records. 22 = a movement, 23 = its text."""
    movements: list[Movement] = []
    current: Optional[Movement] = None
    for raw in content.splitlines():
        line = raw.rstrip("\r\n")
        if len(line) < 2:
            continue
        kind = line[:2]
        if kind == "22" and len(line) >= 42:
            sign = -1 if line[27] == "1" else 1          # 1 = debe (charge), 2 = haber
            amount = sign * int(line[28:42]) / 100
            reference = " ".join(p for p in (line[52:64].strip(), line[64:80].strip()) if p)
            current = Movement(date=_n43_date(line[10:16]), amount=round(amount, 2),
                               description=reference, value_date=_n43_date(line[16:22]))
            movements.append(current)
        elif kind == "23" and current is not None:
            extra = " ".join(p for p in (line[4:42].strip(), line[42:80].strip()) if p)
            if extra:
                current.description = f"{current.description} {extra}".strip()
    if not movements:
        raise StatementError("El archivo parece Norma 43 pero no tiene movimientos.")
    return movements


_DATE_WORDS = ("fecha", "f. operacion", "f.operacion", "date", "data")
_VALUE_WORDS = ("valor",)
_TEXT_WORDS = ("concepto", "descripcion", "movimiento", "detalle", "observaciones",
               "mas datos", "descripció", "concepte", "description", "referencia")
_AMOUNT_WORDS = ("importe", "cantidad", "amount", "import")
_OUT_WORDS = ("cargo", "debe", "gasto", "debit")
_IN_WORDS = ("abono", "haber", "ingreso", "credit")
_BALANCE_WORDS = ("saldo", "balance")


def _money(value) -> Optional[float]:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = re.sub(r"[^\d,.\-]", "", str(value))
    if not re.search(r"\d", text):
        return None
    if "," in text and "." in text:
        text = text.replace(".", "").replace(",", ".") if text.rfind(",") > text.rfind(".") \
            else text.replace(",", "")
    elif "," in text:
        text = text.replace(",", ".")
    try:
        return float(text)
    except ValueError:
        return None


def _date(value) -> Optional[date]:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d", "%d/%m/%y", "%d-%m-%y", "%d.%m.%Y"):
        try:
            return datetime.strptime(text[:10], fmt).date()
        except ValueError:
            continue
    return None


def _columns(header: list) -> Optional[dict]:
    """Which column is which, from a header row. None if it is not the header."""
    names = [_fold(str(h or "")) for h in header]
    found: dict = {"text": []}
    for i, name in enumerate(names):
        if not name:
            continue
        if any(w in name for w in _BALANCE_WORDS):
            found.setdefault("balance", i)
        elif any(w in name for w in _VALUE_WORDS) and "date" not in found:
            found.setdefault("value_date", i)
        elif any(name.startswith(w) or w == name for w in _DATE_WORDS):
            found.setdefault("date", i)
        elif any(w in name for w in _OUT_WORDS):
            found.setdefault("out", i)
        elif any(w in name for w in _IN_WORDS):
            found.setdefault("in", i)
        elif any(w in name for w in _AMOUNT_WORDS):
            found.setdefault("amount", i)
        elif any(w in name for w in _TEXT_WORDS):
            found["text"].append(i)
    if "date" not in found and "value_date" in found:
        found["date"] = found.pop("value_date")
    has_amount = "amount" in found or ("in" in found or "out" in found)
    return found if "date" in found and has_amount else None


def parse_rows(rows: list[list]) -> list[Movement]:
    """A table from a CSV or a spreadsheet, with a header row somewhere near the top."""
    columns, start = None, 0
    for i, row in enumerate(rows[:30]):
        columns = _columns(row)
        if columns:
            start = i + 1
            break
    if not columns:
        raise StatementError(
            "No encuentro las columnas de fecha e importe. Descarga el extracto en "
            "formato Norma 43, Excel o CSV desde la web de tu banco.")

    def cell(row, key):
        index = columns.get(key)
        return row[index] if index is not None and index < len(row) else None

    movements = []
    for row in rows[start:]:
        if not row or all(c in (None, "") for c in row):
            continue
        day = _date(cell(row, "date"))
        if day is None:
            continue
        if "amount" in columns:
            amount = _money(cell(row, "amount"))
        else:
            money_in = _money(cell(row, "in")) or 0.0
            money_out = _money(cell(row, "out")) or 0.0
            amount = abs(money_in) - abs(money_out)
        if amount is None or amount == 0:
            continue
        text = " ".join(str(row[i]).strip() for i in columns["text"]
                        if i < len(row) and row[i] not in (None, ""))
        movements.append(Movement(
            date=day, amount=round(amount, 2), description=text,
            value_date=_date(cell(row, "value_date")),
            balance=_money(cell(row, "balance")),
        ))
    if not movements:
        raise StatementError("El extracto no tiene movimientos que pueda leer.")
    return movements


def parse_file(path: str) -> list[Movement]:
    """Read a statement: Norma 43, CSV or Excel, told apart by content, not by name."""
    source = Path(path)
    data = source.read_bytes()
    if data[:2] == b"PK":                                   # an .xlsx is a ZIP
        from openpyxl import load_workbook

        sheet = load_workbook(io.BytesIO(data), read_only=True, data_only=True).active
        return parse_rows([list(r) for r in sheet.iter_rows(values_only=True)])
    if data[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":       # old binary .xls
        raise StatementError("Ese es el Excel antiguo (.xls). Ábrelo y guárdalo como "
                             ".xlsx, o descarga el extracto en CSV o Norma 43.")
    for encoding in ("utf-8-sig", "latin-1"):
        try:
            text = data.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    lines = [l for l in text.splitlines() if l.strip()]
    if lines and all(l[:2].isdigit() for l in lines[:3]) and lines[0].startswith("11"):
        return parse_norma43(text)
    delimiter = ";" if text.count(";") > text.count(",") else (
        "\t" if text.count("\t") > text.count(",") else ",")
    rows = list(csv.reader(io.StringIO(text), delimiter=delimiter))
    return parse_rows(rows)


# ── Storing them ─────────────────────────────────────────────────────────────

def _row(row) -> Movement:
    return Movement(
        id=row["id"], date=date.fromisoformat(row["date"]), amount=row["amount"],
        description=row["description"], balance=row["balance"],
        value_date=date.fromisoformat(row["value_date"]) if row["value_date"] else None,
    )


def import_movements(movements: list[Movement], source: str = "") -> tuple[int, int]:
    """Store what is new. Returns (added, already there)."""
    added = 0
    with db.transaction() as conn:
        for m in movements:
            cur = conn.execute(
                "INSERT OR IGNORE INTO bank_movements (fingerprint, date, value_date, "
                "amount, description, balance, source) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (m.fingerprint, m.date.isoformat(),
                 m.value_date.isoformat() if m.value_date else None, m.amount,
                 m.description, m.balance, source),
            )
            added += cur.rowcount
    return added, len(movements) - added


def pending_movements() -> list[Movement]:
    rows = db.connect().execute(
        "SELECT * FROM bank_movements WHERE status = 'new' ORDER BY date, id").fetchall()
    return [_row(r) for r in rows]


def get_movement(movement_id: int) -> Optional[Movement]:
    row = db.connect().execute(
        "SELECT * FROM bank_movements WHERE id = ?", (movement_id,)).fetchone()
    return _row(row) if row else None


# ── Matching ─────────────────────────────────────────────────────────────────

@dataclass
class Candidate:
    kind: str                    # "invoice" or "bill"
    ref: str                     # invoice number, or bill id
    label: str
    score: int
    reasons: list[str] = field(default_factory=list)


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]{3,}", _fold(text))
            if w not in {"sl", "sa", "slu", "sociedad", "limitada", "the", "s.l"}}


def _name_matches(name: str, description: str) -> bool:
    wanted = _words(name)
    return bool(wanted) and len(wanted & _words(description)) >= min(2, len(wanted))


def candidates(movement: Movement) -> list[Candidate]:
    """What this movement could be paying, best first."""
    from src import bills
    from src.totals import compute_totals

    text = _fold(movement.description)
    squashed = re.sub(r"[\s\-/]", "", text)
    out: list[Candidate] = []

    if movement.amount > 0:
        for record in store.list_unpaid():
            invoice = record["invoice"]
            if abs(compute_totals(invoice)[2] - movement.amount) > 0.005:
                continue
            if invoice.date and movement.date < invoice.date:
                continue  # paid before it was issued: not this one
            found = Candidate("invoice", invoice.invoice_number,
                              f"{invoice.invoice_number} — {invoice.client_name}",
                              10, ["mismo importe"])
            number = re.sub(r"[\s\-/]", "", invoice.invoice_number.lower())
            if number and number in squashed:
                found.score += 100
                found.reasons.append("su número en el concepto")
            if _name_matches(invoice.client_name, movement.description):
                found.score += 40
                found.reasons.append("el nombre del cliente")
            if invoice.client_id and invoice.client_id.lower() in text:
                found.score += 40
                found.reasons.append("su NIF")
            out.append(found)
    else:
        for bill in bills.list_all(unpaid_only=True):
            if abs(bill["total"] + movement.amount) > 0.005:
                continue
            found = Candidate("bill", str(bill["id"]),
                              f"{bill['supplier_name']}"
                              + (f" ({bill['reference']})" if bill["reference"] else ""),
                              10, ["mismo importe"])
            reference = re.sub(r"[\s\-/]", "", _fold(bill["reference"] or ""))
            if reference and reference in squashed:
                found.score += 100
                found.reasons.append("su número en el concepto")
            if _name_matches(bill["supplier_name"], movement.description):
                found.score += 40
                found.reasons.append("el nombre del proveedor")
            out.append(found)
    return sorted(out, key=lambda c: -c.score)


def is_sure(found: list[Candidate]) -> bool:
    """Clear enough to apply without asking: the number in the text, or the only
    candidate of that amount and the right name in the text."""
    if not found:
        return False
    best = found[0]
    if best.score >= 100 and (len(found) == 1 or found[1].score < 100):
        return True
    return len(found) == 1 and best.score >= 50


def apply(movement_id: int, candidate_kind: str, ref: str) -> str:
    """Mark the invoice or bill as paid on the movement's date, and link them."""
    from src import bills

    movement = get_movement(movement_id)
    if movement is None:
        raise KeyError(f"Movement {movement_id} not found")
    if candidate_kind == "invoice":
        store.mark_paid(ref, movement.date, amount=movement.amount)
        link = ("invoice_number", ref, None)
    else:
        bills.mark_paid(int(ref), movement.date)
        link = ("bill_id", None, int(ref))
    with db.transaction() as conn:
        conn.execute("UPDATE bank_movements SET status = 'matched', invoice_number = ?, "
                     "bill_id = ? WHERE id = ?", (link[1], link[2], movement_id))
    return ref


def ignore(movement_id: int) -> None:
    with db.transaction() as conn:
        conn.execute("UPDATE bank_movements SET status = 'ignored' WHERE id = ?",
                     (movement_id,))


_NOISE = re.compile(
    r"\b(recibo|recibos|adeudo|domiciliacion|domiciliado|compra|tarj(eta)?\.?|"
    r"transferencia|trf|pago|movil|bizum|a favor de|de|en|cargo|com\.?|comision|"
    r"es\d{2}\w*)\b"
    # Card numbers, masked or not ("5402XXXX", "****1234"), and references.
    r"|\S*\d{3,}\S*|\*+\S*", re.IGNORECASE)


def guess_supplier(description: str) -> str:
    """"RECIBO ENDESA ENERGIA SAU 12345" -> "Endesa Energia Sau": a first guess only."""
    cleaned = _NOISE.sub(" ", description or "")
    cleaned = re.sub(r"[^\w\s&.,-]", " ", cleaned)
    words = [w for w in cleaned.split() if len(w) > 1][:4]
    return " ".join(w.capitalize() for w in words) or "Proveedor"


def record_as_expense(movement_id: int, supplier: Optional[str] = None,
                      category: str = "otros") -> int:
    """File a charge with no bill behind it as an expense, already paid. Returns its id.

    No VAT is assumed: a bank line is not an invoice. The expense counts as a cost;
    its VAT is deductible once the invoice itself is photographed.
    """
    from src import bills

    movement = get_movement(movement_id)
    if movement is None or movement.amount >= 0:
        raise ValueError("Solo un cargo puede anotarse como gasto.")
    bill_id = bills.create(
        (supplier or guess_supplier(movement.description)).strip(), abs(movement.amount),
        bill_date=movement.date, due_days=0, subtotal=abs(movement.amount),
        tax_amount=0.0, category=category,
        notes=f"Del extracto del banco: {movement.description[:120]}",
    )
    bills.mark_paid(bill_id, movement.date)
    with db.transaction() as conn:
        conn.execute("UPDATE bank_movements SET status = 'matched', bill_id = ? "
                     "WHERE id = ?", (bill_id, movement_id))
    return bill_id


@dataclass
class Report:
    added: int = 0
    duplicates: int = 0
    applied: list = field(default_factory=list)      # (movement, candidate)
    proposed: list = field(default_factory=list)     # (movement, [candidates])
    unmatched_in: list = field(default_factory=list)
    unmatched_out: list = field(default_factory=list)


def reconcile(movements: Optional[list[Movement]] = None, can_invoices: bool = True,
              can_bills: bool = True) -> Report:
    """Match every pending movement: apply the sure ones, list the rest."""
    report = Report()
    for movement in (movements if movements is not None else pending_movements()):
        if movement.id is None:
            continue
        allowed = can_invoices if movement.amount > 0 else can_bills
        found = candidates(movement) if allowed else []
        if found and is_sure(found):
            apply(movement.id, found[0].kind, found[0].ref)
            report.applied.append((movement, found[0]))
        elif found:
            report.proposed.append((movement, found))
        elif movement.amount > 0:
            report.unmatched_in.append(movement)
        else:
            report.unmatched_out.append(movement)
    return report


def import_and_reconcile(path: str, can_invoices: bool = True,
                         can_bills: bool = True) -> Report:
    movements = parse_file(path)
    added, duplicates = import_movements(movements, Path(path).name)
    report = reconcile(can_invoices=can_invoices, can_bills=can_bills)
    report.added, report.duplicates = added, duplicates
    return report
