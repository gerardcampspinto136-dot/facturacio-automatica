"""Clients and suppliers.

Before this, client details were re-dictated on every invoice, so the same client could
end up spelled three ways with two different tax ids. A contact record is entered once
and matched by name afterwards, which also gives the voice parser something to correct
itself against: "factura para Talleres Mario" resolves to the stored email and CIF.
"""

from typing import Optional

from src import db

CLIENT = "client"
SUPPLIER = "supplier"

_FIELDS = (
    "name", "tax_id", "email", "phone", "address",
    "payment_terms_days", "iban", "notes", "active",
)


def create(kind: str, name: str, **fields) -> int:
    """Add a contact and return its id. `kind` is CLIENT or SUPPLIER."""
    if kind not in (CLIENT, SUPPLIER):
        raise ValueError(f"Unknown contact kind: {kind}")
    if not name or not name.strip():
        raise ValueError("A contact needs a name")

    cols = ["kind", "name"]
    vals: list = [kind, name.strip()]
    for key in _FIELDS:
        if key != "name" and key in fields:
            cols.append(key)
            vals.append(fields[key])

    placeholders = ", ".join("?" for _ in cols)
    with db.transaction() as conn:
        cur = conn.execute(
            f"INSERT INTO contacts ({', '.join(cols)}) VALUES ({placeholders})", vals
        )
        return cur.lastrowid


def get(contact_id: int) -> Optional[dict]:
    row = db.connect().execute(
        "SELECT * FROM contacts WHERE id = ?", (contact_id,)
    ).fetchone()
    return dict(row) if row else None


def update(contact_id: int, **fields) -> None:
    changes = {k: v for k, v in fields.items() if k in _FIELDS}
    if not changes:
        return
    assignments = ", ".join(f"{k} = ?" for k in changes)
    with db.transaction() as conn:
        cur = conn.execute(
            f"UPDATE contacts SET {assignments} WHERE id = ?",
            (*changes.values(), contact_id),
        )
        if cur.rowcount == 0:
            raise KeyError(f"Contact {contact_id} not found")


def delete(contact_id: int) -> None:
    """Deactivate rather than remove, so invoices keep pointing at a real contact."""
    update(contact_id, active=0)


def list_all(kind: Optional[str] = None, include_inactive: bool = False) -> list[dict]:
    sql = "SELECT * FROM contacts WHERE 1 = 1"
    params: list = []
    if kind:
        sql += " AND kind = ?"
        params.append(kind)
    if not include_inactive:
        sql += " AND active = 1"
    sql += " ORDER BY name COLLATE NOCASE"
    return [dict(r) for r in db.connect().execute(sql, params).fetchall()]


def find_by_name(name: str, kind: str = CLIENT) -> Optional[dict]:
    """Match a spoken or typed name against the stored contacts.

    Tries exact (case-insensitive) first, then a unique substring match, so "Talleres
    Mario" finds "Talleres Mario S.L." but an ambiguous "Talleres" returns nothing rather
    than guessing wrong on an invoice.
    """
    if not name or not name.strip():
        return None
    needle = name.strip()
    conn = db.connect()

    row = conn.execute(
        "SELECT * FROM contacts WHERE kind = ? AND active = 1 "
        "AND name = ? COLLATE NOCASE",
        (kind, needle),
    ).fetchone()
    if row:
        return dict(row)

    rows = conn.execute(
        "SELECT * FROM contacts WHERE kind = ? AND active = 1 "
        "AND name LIKE ? COLLATE NOCASE",
        (kind, f"%{needle}%"),
    ).fetchall()
    return dict(rows[0]) if len(rows) == 1 else None


def find_or_create(kind: str, name: str, **fields) -> int:
    """Return the id of the matching contact, creating it if there is no match."""
    found = find_by_name(name, kind)
    if found:
        return found["id"]
    return create(kind, name, **fields)
