"""Supplier bills -- the money going out.

The invoicing side tracks what clients owe you. This is the mirror image: what you owe
suppliers, and by when. A missed due date costs late fees and goodwill, and it is the
thing a busy autónomo forgets first, so the point of this module is not bookkeeping but
the reminder that comes out of it (see src/notify.py).

Amounts are stored as entered rather than recomputed: a supplier bill is a document you
received, and it has to reconcile against their figures even when their rounding differs
from yours.
"""

from datetime import date, timedelta
from typing import Optional

from src import contacts, db

_FIELDS = (
    "supplier_id", "supplier_name", "reference", "date", "due_date",
    "subtotal", "tax_amount", "total", "category", "notes", "file_path",
)


def _row(row) -> Optional[dict]:
    if row is None:
        return None
    out = dict(row)
    out["paid"] = out["paid_at"] is not None
    return out


def create(supplier_name: str, total: float, *, bill_date: Optional[date] = None,
           due_date: Optional[date] = None, due_days: Optional[int] = None,
           subtotal: Optional[float] = None, tax_amount: Optional[float] = None,
           reference: Optional[str] = None, category: Optional[str] = None,
           notes: Optional[str] = None, file_path: Optional[str] = None,
           link_supplier: bool = True) -> int:
    """Record a bill received from a supplier. Returns its id.

    The due date is taken as given, else computed from `due_days`, else from the
    supplier's stored payment terms, else 30 days -- so entering a bill from a known
    supplier needs nothing but a name and an amount.
    """
    if not supplier_name or not supplier_name.strip():
        raise ValueError("A bill needs a supplier name")

    supplier_name = supplier_name.strip()
    bill_date = bill_date or date.today()

    supplier = contacts.find_by_name(supplier_name, contacts.SUPPLIER)
    supplier_id = None
    if supplier:
        supplier_id = supplier["id"]
        supplier_name = supplier["name"]
    elif link_supplier:
        supplier_id = contacts.create(contacts.SUPPLIER, supplier_name)

    if due_date is None:
        if due_days is None:
            due_days = supplier["payment_terms_days"] if supplier else 30
        due_date = bill_date + timedelta(days=due_days)

    if subtotal is None and tax_amount is not None:
        subtotal = round(total - tax_amount, 2)
    elif tax_amount is None and subtotal is not None:
        tax_amount = round(total - subtotal, 2)

    with db.transaction() as conn:
        cur = conn.execute(
            "INSERT INTO bills (supplier_id, supplier_name, reference, date, due_date, "
            "subtotal, tax_amount, total, category, notes, file_path) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                supplier_id, supplier_name, reference,
                bill_date.isoformat(), due_date.isoformat(),
                subtotal if subtotal is not None else total,
                tax_amount if tax_amount is not None else 0.0,
                total, category, notes, file_path,
            ),
        )
        return cur.lastrowid


def get(bill_id: int) -> Optional[dict]:
    return _row(db.connect().execute(
        "SELECT * FROM bills WHERE id = ?", (bill_id,)
    ).fetchone())


def update(bill_id: int, **fields) -> None:
    changes = {k: v for k, v in fields.items() if k in _FIELDS}
    for key in ("date", "due_date"):
        if isinstance(changes.get(key), date):
            changes[key] = changes[key].isoformat()
    if not changes:
        return
    assignments = ", ".join(f"{k} = ?" for k in changes)
    with db.transaction() as conn:
        cur = conn.execute(
            f"UPDATE bills SET {assignments} WHERE id = ?",
            (*changes.values(), bill_id),
        )
        if cur.rowcount == 0:
            raise KeyError(f"Bill {bill_id} not found")


def delete(bill_id: int) -> None:
    """Bills are removable: unlike an issued invoice, a mistyped one is not a legal record."""
    with db.transaction() as conn:
        conn.execute("DELETE FROM bills WHERE id = ?", (bill_id,))


def mark_paid(bill_id: int, when: Optional[date] = None) -> None:
    with db.transaction() as conn:
        cur = conn.execute(
            "UPDATE bills SET paid_at = ? WHERE id = ?",
            ((when or date.today()).isoformat(), bill_id),
        )
        if cur.rowcount == 0:
            raise KeyError(f"Bill {bill_id} not found")


def mark_unpaid(bill_id: int) -> None:
    with db.transaction() as conn:
        conn.execute("UPDATE bills SET paid_at = NULL WHERE id = ?", (bill_id,))


def list_all(supplier_id: Optional[int] = None, unpaid_only: bool = False,
             since: Optional[date] = None) -> list[dict]:
    sql = "SELECT * FROM bills WHERE 1 = 1"
    params: list = []
    if supplier_id is not None:
        sql += " AND supplier_id = ?"
        params.append(supplier_id)
    if unpaid_only:
        sql += " AND paid_at IS NULL"
    if since is not None:
        sql += " AND date >= ?"
        params.append(since.isoformat())
    sql += " ORDER BY due_date IS NULL, due_date, id"
    return [_row(r) for r in db.connect().execute(sql, params).fetchall()]


def due_soon(within_days: int = 7, as_of: Optional[date] = None) -> list[dict]:
    """Unpaid bills already overdue or falling due within `within_days`.

    Overdue ones are included deliberately: a reminder that only looks forward stops
    mentioning a bill on the very day it starts costing money.
    """
    as_of = as_of or date.today()
    horizon = as_of + timedelta(days=within_days)
    rows = db.connect().execute(
        "SELECT * FROM bills WHERE paid_at IS NULL AND due_date IS NOT NULL "
        "AND due_date <= ? ORDER BY due_date, id",
        (horizon.isoformat(),),
    ).fetchall()

    out = []
    for row in rows:
        bill = _row(row)
        due = date.fromisoformat(bill["due_date"])
        bill["days_until_due"] = (due - as_of).days
        bill["overdue"] = due < as_of
        out.append(bill)
    return out


def total_owed(as_of: Optional[date] = None, overdue_only: bool = False) -> float:
    sql = "SELECT COALESCE(SUM(total), 0) FROM bills WHERE paid_at IS NULL"
    params: list = []
    if overdue_only:
        sql += " AND due_date IS NOT NULL AND due_date < ?"
        params.append((as_of or date.today()).isoformat())
    return round(db.connect().execute(sql, params).fetchone()[0], 2)


def by_supplier(unpaid_only: bool = True) -> list[dict]:
    """What is owed, grouped by supplier, biggest first."""
    sql = (
        "SELECT supplier_name, COUNT(*) AS bills, COALESCE(SUM(total), 0) AS amount "
        "FROM bills WHERE 1 = 1"
    )
    if unpaid_only:
        sql += " AND paid_at IS NULL"
    sql += " GROUP BY supplier_name ORDER BY amount DESC"
    return [dict(r) for r in db.connect().execute(sql).fetchall()]
