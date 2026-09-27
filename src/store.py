"""Persistent store for invoices, backed by SQLite (data/facturacio.db).

Two states live in one table: 'pending' invoices are drafts awaiting review and carry a
token but no number; 'issued' invoices have consumed a number and been sent. Keeping them
together means approving a draft is an UPDATE rather than a move between stores, so an
invoice can never be briefly in both places or in neither.

The public functions are unchanged from the earlier JSON-file version, so the bot, the
web app and the scheduler did not need to change. Data written by that version is
imported automatically the first time this module runs -- see migrate_from_json().
"""

import json
import os
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

from src import db
from src.models import InvoiceData, InvoiceItem

# Legacy JSON locations, read once at migration time and then left alone.
PENDING_DIR = Path("data/pending")
ISSUED_DIR = Path("data/issued")


# ── Serialization ────────────────────────────────────────────────────────────

def _item_to_dict(item: InvoiceItem) -> dict:
    return {
        "description": item.description,
        "quantity": item.quantity,
        "unit_price": item.unit_price,
        "total": item.total,
    }


def invoice_to_dict(invoice: InvoiceData) -> dict:
    return {
        "client_name": invoice.client_name,
        "client_email": invoice.client_email,
        "client_address": invoice.client_address,
        "client_id": invoice.client_id,
        "invoice_number": invoice.invoice_number,
        "date": invoice.date.isoformat() if invoice.date else None,
        "notes": invoice.notes,
        "rectifies": invoice.rectifies,
        "prices_include_tax": invoice.prices_include_tax,
        "items": [_item_to_dict(i) for i in invoice.items],
    }


def invoice_from_dict(data: dict) -> InvoiceData:
    items = [
        InvoiceItem(
            description=i.get("description", ""),
            quantity=float(i.get("quantity", 1)),
            unit_price=float(i.get("unit_price", 0)),
            total=float(i.get("total", 0)),
        )
        for i in data.get("items", [])
    ]
    raw_date = data.get("date")
    inv_date = date.fromisoformat(raw_date) if raw_date else date.today()
    return InvoiceData(
        client_name=data.get("client_name", ""),
        client_email=data.get("client_email", ""),
        items=items,
        client_address=data.get("client_address"),
        client_id=data.get("client_id"),
        invoice_number=data.get("invoice_number"),
        date=inv_date,
        notes=data.get("notes"),
        rectifies=data.get("rectifies"),
        prices_include_tax=data.get("prices_include_tax"),
    )


# ── Row <-> model ────────────────────────────────────────────────────────────

def _load_items(conn, invoice_id: int) -> list[InvoiceItem]:
    rows = conn.execute(
        "SELECT description, quantity, unit_price, total FROM invoice_items "
        "WHERE invoice_id = ? ORDER BY position, id",
        (invoice_id,),
    ).fetchall()
    return [
        InvoiceItem(
            description=r["description"],
            quantity=r["quantity"],
            unit_price=r["unit_price"],
            total=r["total"],
        )
        for r in rows
    ]


def _row_to_invoice(conn, row) -> InvoiceData:
    raw_date = row["date"]
    return InvoiceData(
        client_name=row["client_name"] or "",
        client_email=row["client_email"] or "",
        items=_load_items(conn, row["id"]),
        client_address=row["client_address"],
        client_id=row["client_id"],
        invoice_number=row["number"],
        date=date.fromisoformat(raw_date) if raw_date else date.today(),
        notes=row["notes"],
        rectifies=row["rectifies"],
        prices_include_tax=(
            None if row["prices_include_tax"] is None else bool(row["prices_include_tax"])
        ),
        # Line prices are stored net, so they must not be converted a second time.
        prices_normalized=True,
        contact_id=row["contact_id"],
        tax_rate=row["tax_rate"],
        irpf_rate=row["irpf_rate"],
    )


def _pending_payload(conn, row) -> dict:
    return {
        "token": row["token"],
        "created": row["created_at"],
        "draft_path": row["draft_path"],
        # Who prepared it, so they can be told when it is approved or thrown out.
        "created_by": row["created_by"],
        "created_by_name": row["created_by_name"],
        "created_chat_id": row["created_chat_id"],
        "invoice": _row_to_invoice(conn, row),
    }


def _issued_payload(conn, row) -> dict:
    return {
        "id": row["id"],
        "issued_at": row["issued_at"],
        "rectified_by": row["rectified_by"],
        "paid_at": row["paid_at"],
        "due_date": row["due_date"],
        "email_sent_at": row["email_sent_at"],
        "email_error": row["email_error"],
        "invoice": _row_to_invoice(conn, row),
    }


def _write_items(conn, invoice_id: int, invoice: InvoiceData) -> None:
    conn.execute("DELETE FROM invoice_items WHERE invoice_id = ?", (invoice_id,))
    for pos, item in enumerate(invoice.items):
        conn.execute(
            "INSERT INTO invoice_items "
            "(invoice_id, position, description, quantity, unit_price, total) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (invoice_id, pos, item.description, item.quantity, item.unit_price, item.total),
        )


# ── Pending queue ────────────────────────────────────────────────────────────

def new_token() -> str:
    return uuid.uuid4().hex[:12]


def add_pending(invoice: InvoiceData, draft_path: str, *, token: Optional[str] = None,
                created_by: Optional[int] = None, created_by_name: Optional[str] = None,
                created_chat_id: Optional[int] = None) -> str:
    """Queue a finished draft for someone allowed to approve it. Returns its token.

    It consumes no invoice number: that happens on approval, so a draft that is thrown
    away leaves no gap in the series.
    """
    token = token or new_token()
    with db.transaction() as conn:
        cur = conn.execute(
            "INSERT INTO invoices "
            "(status, token, client_name, client_email, client_address, client_id, "
            " date, notes, rectifies, prices_include_tax, contact_id, tax_rate, "
            " irpf_rate, draft_path, created_by, created_by_name, created_chat_id, "
            " created_at) "
            "VALUES ('pending', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                token,
                invoice.client_name,
                invoice.client_email,
                invoice.client_address,
                invoice.client_id,
                invoice.date.isoformat() if invoice.date else date.today().isoformat(),
                invoice.notes,
                invoice.rectifies,
                None if invoice.prices_include_tax is None else int(invoice.prices_include_tax),
                invoice.contact_id,
                invoice.tax_rate,
                invoice.irpf_rate,
                draft_path,
                created_by,
                created_by_name,
                created_chat_id,
                datetime.now().isoformat(timespec="seconds"),
            ),
        )
        _write_items(conn, cur.lastrowid, invoice)
    return token


def get_pending(token: str) -> Optional[dict]:
    conn = db.connect()
    row = conn.execute(
        "SELECT * FROM invoices WHERE token = ? AND status = 'pending'", (token,)
    ).fetchone()
    return _pending_payload(conn, row) if row else None


def list_pending() -> list[dict]:
    conn = db.connect()
    rows = conn.execute(
        "SELECT * FROM invoices WHERE status = 'pending' ORDER BY created_at, id"
    ).fetchall()
    return [_pending_payload(conn, r) for r in rows]


def count_pending() -> int:
    conn = db.connect()
    return conn.execute(
        "SELECT COUNT(*) FROM invoices WHERE status = 'pending'"
    ).fetchone()[0]


def update_pending(token: str, invoice: InvoiceData, draft_path: Optional[str] = None) -> None:
    with db.transaction() as conn:
        row = conn.execute(
            "SELECT id FROM invoices WHERE token = ? AND status = 'pending'", (token,)
        ).fetchone()
        if row is None:
            raise KeyError(f"Pending invoice {token} not found")
        conn.execute(
            "UPDATE invoices SET client_name = ?, client_email = ?, client_address = ?, "
            "client_id = ?, date = ?, notes = ?, prices_include_tax = ?, tax_rate = ?, "
            "irpf_rate = ?"
            + (", draft_path = ?" if draft_path is not None else "")
            + " WHERE id = ?",
            (
                invoice.client_name,
                invoice.client_email,
                invoice.client_address,
                invoice.client_id,
                invoice.date.isoformat() if invoice.date else date.today().isoformat(),
                invoice.notes,
                None if invoice.prices_include_tax is None else int(invoice.prices_include_tax),
                invoice.tax_rate,
                invoice.irpf_rate,
                *([draft_path] if draft_path is not None else []),
                row["id"],
            ),
        )
        _write_items(conn, row["id"], invoice)


def remove_pending(token: str) -> None:
    """Drop a pending draft, deleting its draft PDF as a best effort."""
    with db.transaction() as conn:
        row = conn.execute(
            "SELECT id, draft_path FROM invoices WHERE token = ? AND status = 'pending'",
            (token,),
        ).fetchone()
        if row is None:
            return
        draft = row["draft_path"]
        conn.execute("DELETE FROM invoices WHERE id = ?", (row["id"],))
    if draft and os.path.exists(draft):
        try:
            os.unlink(draft)
        except OSError:
            pass


def approve_pending(token: str, number: str, due_days: int = 30) -> None:
    """Promote a reviewed draft to issued, under the number just assigned to it.

    The draft row -- its token, creation time and author -- survives approval instead
    of being deleted and rewritten.
    """
    with db.transaction() as conn:
        row = conn.execute(
            "SELECT * FROM invoices WHERE token = ? AND status = 'pending'", (token,)
        ).fetchone()
        if row is None:
            raise KeyError(f"Pending invoice {token} not found")
        write_issued(conn, _row_to_invoice(conn, row), number, token=token,
                     due_days=due_days)


# ── Issued record ────────────────────────────────────────────────────────────

def _flag(value: Optional[bool]):
    return None if value is None else int(value)


def write_issued(conn, invoice: InvoiceData, number: str, *, token: Optional[str] = None,
                 due_days: int = 30) -> int:
    """Record `invoice` as issued under `number`, inside the caller's transaction.

    The caller holds the transaction in which `number` was consumed, so the number and
    the invoice using it are committed together or not at all -- a failure anywhere
    rolls the counter back as well, and the series never gets a gap.

    An issued row is frozen by triggers (see db.py), so everything is written while the
    row is still 'pending' and the status flips last. With `token`, the waiting draft is
    the row that becomes the invoice; a token that is no longer pending (someone else
    approved it a moment ago) raises KeyError and rolls everything back.
    """
    inv_date = invoice.date or date.today()
    fields = (
        invoice.client_name,
        invoice.client_email,
        invoice.client_address,
        invoice.client_id,
        inv_date.isoformat(),
        invoice.notes,
        invoice.rectifies,
        _flag(invoice.prices_include_tax),
        invoice.contact_id,
        invoice.tax_rate,
        invoice.irpf_rate,
    )

    if token:
        row = conn.execute(
            "SELECT id FROM invoices WHERE token = ? AND status = 'pending'", (token,)
        ).fetchone()
        if row is None:
            raise KeyError(f"Pending invoice {token} not found")
        invoice_id = row["id"]
        conn.execute(
            "UPDATE invoices SET client_name = ?, client_email = ?, client_address = ?, "
            "client_id = ?, date = ?, notes = ?, rectifies = ?, prices_include_tax = ?, "
            "contact_id = COALESCE(?, contact_id), tax_rate = ?, irpf_rate = ? "
            "WHERE id = ?",
            (*fields, invoice_id),
        )
    else:
        cur = conn.execute(
            "INSERT INTO invoices "
            "(status, client_name, client_email, client_address, client_id, date, notes, "
            " rectifies, prices_include_tax, contact_id, tax_rate, irpf_rate) "
            "VALUES ('pending', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            fields,
        )
        invoice_id = cur.lastrowid

    _write_items(conn, invoice_id, invoice)
    conn.execute(
        "UPDATE invoices SET status = 'issued', number = ?, issued_at = ?, due_date = ? "
        "WHERE id = ?",
        (
            number,
            datetime.now().isoformat(timespec="seconds"),
            (inv_date + timedelta(days=due_days)).isoformat(),
            invoice_id,
        ),
    )
    return invoice_id


def record_issued(invoice: InvoiceData, due_days: int = 30) -> None:
    """Record an invoice that already carries its number, in a transaction of its own.

    Issuing normally goes through finalize.issue(), which consumes the number in the
    same transaction; this is for callers that already hold one.
    """
    if not invoice.invoice_number:
        raise ValueError("Una factura emitida necesita número")
    with db.transaction() as conn:
        if conn.execute("SELECT 1 FROM invoices WHERE number = ?",
                        (invoice.invoice_number,)).fetchone():
            raise ValueError(f"Ya existe la factura {invoice.invoice_number}")
        write_issued(conn, invoice, invoice.invoice_number, due_days=due_days)


def record_delivery(number: str, error: Optional[str] = None) -> None:
    """Note whether the email carrying an issued invoice actually left.

    The bot used to say "Enviada" whether or not Gmail had accepted it; this is what
    lets it -- and the panel -- tell the truth, and offer to try again.
    """
    with db.transaction() as conn:
        if error:
            conn.execute("UPDATE invoices SET email_error = ? WHERE number = ?",
                         (error[:500], number))
        else:
            conn.execute(
                "UPDATE invoices SET email_sent_at = ?, email_error = NULL "
                "WHERE number = ?",
                (datetime.now().isoformat(timespec="seconds"), number),
            )


def get_issued(number: str) -> Optional[dict]:
    conn = db.connect()
    row = conn.execute(
        "SELECT * FROM invoices WHERE number = ? AND status = 'issued'", (number,)
    ).fetchone()
    return _issued_payload(conn, row) if row else None


def list_issued() -> list[dict]:
    conn = db.connect()
    rows = conn.execute(
        "SELECT * FROM invoices WHERE status = 'issued' "
        "ORDER BY issued_at DESC, id DESC"
    ).fetchall()
    return [_issued_payload(conn, r) for r in rows]


def mark_rectified(number: str, rectified_by: str) -> None:
    with db.transaction() as conn:
        cur = conn.execute(
            "UPDATE invoices SET rectified_by = ? WHERE number = ? AND status = 'issued'",
            (rectified_by, number),
        )
        if cur.rowcount == 0:
            raise KeyError(f"Issued invoice {number} not found")


# ── Payments ─────────────────────────────────────────────────────────────────

def mark_paid(number: str, when: Optional[date] = None, amount: Optional[float] = None) -> None:
    """Record that an issued invoice has been paid."""
    with db.transaction() as conn:
        cur = conn.execute(
            "UPDATE invoices SET paid_at = ?, paid_amount = ? "
            "WHERE number = ? AND status = 'issued'",
            ((when or date.today()).isoformat(), amount, number),
        )
        if cur.rowcount == 0:
            raise KeyError(f"Issued invoice {number} not found")


def mark_unpaid(number: str) -> None:
    with db.transaction() as conn:
        conn.execute(
            "UPDATE invoices SET paid_at = NULL, paid_amount = NULL WHERE number = ?",
            (number,),
        )


def list_unpaid(as_of: Optional[date] = None, overdue_only: bool = False) -> list[dict]:
    """Issued, unpaid, not cancelled -- oldest due date first.

    Rectifying invoices are excluded: a credit note is not a receivable.
    """
    as_of = as_of or date.today()
    conn = db.connect()
    sql = (
        "SELECT * FROM invoices WHERE status = 'issued' AND paid_at IS NULL "
        "AND rectified_by IS NULL AND rectifies IS NULL"
    )
    params: list = []
    if overdue_only:
        sql += " AND due_date IS NOT NULL AND due_date < ?"
        params.append(as_of.isoformat())
    sql += " ORDER BY due_date IS NULL, due_date, id"

    out = []
    for row in conn.execute(sql, params).fetchall():
        payload = _issued_payload(conn, row)
        due = row["due_date"]
        payload["days_overdue"] = (
            (as_of - date.fromisoformat(due)).days if due else 0
        )
        out.append(payload)
    return out


# ── One-time import of the JSON-file data ────────────────────────────────────

def migrate_from_json() -> dict:
    """Import data written by the previous JSON-file store. Safe to call repeatedly.

    The JSON files are left on disk untouched as a backup; a marker in `meta` stops the
    import from running twice and re-creating records the user has since deleted.
    """
    conn = db.connect()
    done = conn.execute(
        "SELECT value FROM meta WHERE key = 'json_import'"
    ).fetchone()
    if done:
        return {"skipped": True, "pending": 0, "issued": 0, "counters": 0}

    n_pending = n_issued = n_counters = 0

    for path in sorted(PENDING_DIR.glob("*.json")) if PENDING_DIR.exists() else []:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        invoice = invoice_from_dict(payload.get("invoice", {}))
        with db.transaction() as c:
            cur = c.execute(
                "INSERT OR IGNORE INTO invoices "
                "(status, token, client_name, client_email, client_address, client_id, "
                " date, notes, rectifies, draft_path, created_at) "
                "VALUES ('pending', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    payload.get("token") or uuid.uuid4().hex[:12],
                    invoice.client_name,
                    invoice.client_email,
                    invoice.client_address,
                    invoice.client_id,
                    invoice.date.isoformat(),
                    invoice.notes,
                    invoice.rectifies,
                    payload.get("draft_path"),
                    payload.get("created") or datetime.now().isoformat(timespec="seconds"),
                ),
            )
            if cur.lastrowid and cur.rowcount:
                _write_items(c, cur.lastrowid, invoice)
                n_pending += 1

    for path in sorted(ISSUED_DIR.glob("*.json")) if ISSUED_DIR.exists() else []:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        invoice = invoice_from_dict(payload.get("invoice", {}))
        if not invoice.invoice_number:
            continue
        with db.transaction() as c:
            # Written as pending and flipped last: an issued row is frozen, items and all.
            cur = c.execute(
                "INSERT OR IGNORE INTO invoices "
                "(status, number, client_name, client_email, client_address, client_id, "
                " date, notes, rectifies, rectified_by, issued_at) "
                "VALUES ('pending', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    invoice.invoice_number,
                    invoice.client_name,
                    invoice.client_email,
                    invoice.client_address,
                    invoice.client_id,
                    invoice.date.isoformat(),
                    invoice.notes,
                    invoice.rectifies,
                    payload.get("rectified_by"),
                    payload.get("issued_at"),
                ),
            )
            if cur.lastrowid and cur.rowcount:
                _write_items(c, cur.lastrowid, invoice)
                c.execute("UPDATE invoices SET status = 'issued' WHERE id = ?",
                          (cur.lastrowid,))
                n_issued += 1

    # Invoice counters, in either the legacy flat shape or the per-series one.
    counter_file = Path("data/invoice_counter.json")
    if counter_file.exists():
        try:
            data = json.loads(counter_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        if "year" in data or "counter" in data:
            data = {"": {"year": data.get("year"), "counter": data.get("counter", 0)}}
        with db.transaction() as c:
            for series, entry in data.items():
                if not isinstance(entry, dict) or not entry.get("year"):
                    continue
                c.execute(
                    "INSERT OR IGNORE INTO counters (series, year, counter) VALUES (?, ?, ?)",
                    (series, int(entry["year"]), int(entry.get("counter", 0))),
                )
                n_counters += 1

    with db.transaction() as c:
        c.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('json_import', ?)",
            (datetime.now().isoformat(timespec="seconds"),),
        )

    return {
        "skipped": False,
        "pending": n_pending,
        "issued": n_issued,
        "counters": n_counters,
    }
