"""Product catalog and stock levels.

Invoice lines used to be free text with a price attached, which meant nothing could be
counted: not how many units are left, not what a thing normally costs, not which items
sell. A catalog entry fixes a name, a price and (optionally) a stock level.

Stock is never written directly. Every change goes through move(), which records the
delta and the resulting balance in stock_moves, so a level that looks wrong can always
be traced back to the movements that produced it.
"""

from typing import Optional

from src import db

SALE = "sale"
PURCHASE = "purchase"
ADJUSTMENT = "adjustment"
COUNT = "count"

_FIELDS = (
    "sku", "name", "description", "unit", "unit_price", "cost_price",
    "tax_rate", "track_stock", "reorder_point", "supplier_id", "active",
)


# ── Catalog ──────────────────────────────────────────────────────────────────

def create(name: str, **fields) -> int:
    """Add a product or service. Pass track_stock=0 for services."""
    if not name or not name.strip():
        raise ValueError("A product needs a name")

    cols = ["name"]
    vals: list = [name.strip()]
    for key in _FIELDS:
        if key != "name" and key in fields:
            cols.append(key)
            vals.append(fields[key])

    opening = float(fields.get("stock_qty", 0) or 0)
    placeholders = ", ".join("?" for _ in cols)
    with db.transaction() as conn:
        cur = conn.execute(
            f"INSERT INTO products ({', '.join(cols)}) VALUES ({placeholders})", vals
        )
        product_id = cur.lastrowid
        if opening:
            _apply_move(conn, product_id, opening, COUNT, ref=None, note="Opening stock")
    return product_id


def get(product_id: int) -> Optional[dict]:
    row = db.connect().execute(
        "SELECT * FROM products WHERE id = ?", (product_id,)
    ).fetchone()
    return dict(row) if row else None


def update(product_id: int, **fields) -> None:
    """Change catalog fields. Stock is deliberately not settable here -- use move()."""
    changes = {k: v for k, v in fields.items() if k in _FIELDS}
    if not changes:
        return
    assignments = ", ".join(f"{k} = ?" for k in changes)
    with db.transaction() as conn:
        cur = conn.execute(
            f"UPDATE products SET {assignments} WHERE id = ?",
            (*changes.values(), product_id),
        )
        if cur.rowcount == 0:
            raise KeyError(f"Product {product_id} not found")


def delete(product_id: int) -> None:
    """Deactivate rather than remove, so past invoice lines keep their link."""
    update(product_id, active=0)


def list_all(include_inactive: bool = False) -> list[dict]:
    sql = "SELECT * FROM products"
    if not include_inactive:
        sql += " WHERE active = 1"
    sql += " ORDER BY name COLLATE NOCASE"
    return [dict(r) for r in db.connect().execute(sql).fetchall()]


def find_by_name(name: str) -> Optional[dict]:
    """Resolve a spoken product name, exact first then a unique partial match."""
    if not name or not name.strip():
        return None
    needle = name.strip()
    conn = db.connect()

    row = conn.execute(
        "SELECT * FROM products WHERE active = 1 AND "
        "(name = ? COLLATE NOCASE OR sku = ? COLLATE NOCASE)",
        (needle, needle),
    ).fetchone()
    if row:
        return dict(row)

    rows = conn.execute(
        "SELECT * FROM products WHERE active = 1 AND name LIKE ? COLLATE NOCASE",
        (f"%{needle}%",),
    ).fetchall()
    return dict(rows[0]) if len(rows) == 1 else None


# ── Stock ────────────────────────────────────────────────────────────────────

def _apply_move(conn, product_id: int, delta: float, reason: str,
                ref: Optional[str], note: Optional[str]) -> float:
    """Apply a movement inside an open transaction and return the new balance."""
    row = conn.execute(
        "SELECT stock_qty, track_stock FROM products WHERE id = ?", (product_id,)
    ).fetchone()
    if row is None:
        raise KeyError(f"Product {product_id} not found")
    if not row["track_stock"]:
        return row["stock_qty"]

    balance = round(row["stock_qty"] + delta, 4)
    conn.execute("UPDATE products SET stock_qty = ? WHERE id = ?", (balance, product_id))
    conn.execute(
        "INSERT INTO stock_moves (product_id, delta, balance, reason, ref, note) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (product_id, delta, balance, reason, ref, note),
    )
    return balance


def move(product_id: int, delta: float, reason: str = ADJUSTMENT,
         ref: Optional[str] = None, note: Optional[str] = None) -> float:
    """Change a stock level by `delta` (negative for outgoing) and return the new level.

    Stock is allowed to go negative: a shop that has sold something it had not registered
    yet is a real situation, and blocking the invoice would be worse than recording it.
    """
    with db.transaction() as conn:
        return _apply_move(conn, product_id, delta, reason, ref, note)


def set_level(product_id: int, quantity: float, note: str = "Physical count") -> float:
    """Set stock to a counted quantity, recording the difference as a movement."""
    with db.transaction() as conn:
        row = conn.execute(
            "SELECT stock_qty FROM products WHERE id = ?", (product_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"Product {product_id} not found")
        return _apply_move(
            conn, product_id, quantity - row["stock_qty"], COUNT, None, note
        )


def history(product_id: int, limit: int = 50) -> list[dict]:
    rows = db.connect().execute(
        "SELECT * FROM stock_moves WHERE product_id = ? "
        "ORDER BY created_at DESC, id DESC LIMIT ?",
        (product_id, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def low_stock() -> list[dict]:
    """Tracked products at or below their reorder point, shortest first."""
    rows = db.connect().execute(
        "SELECT * FROM products WHERE active = 1 AND track_stock = 1 "
        "AND reorder_point > 0 AND stock_qty <= reorder_point "
        "ORDER BY (stock_qty - reorder_point), name COLLATE NOCASE"
    ).fetchall()
    return [dict(r) for r in rows]


def stock_value() -> float:
    """Total value of stock on hand, at cost."""
    row = db.connect().execute(
        "SELECT COALESCE(SUM(stock_qty * cost_price), 0) FROM products "
        "WHERE active = 1 AND track_stock = 1"
    ).fetchone()
    return round(row[0], 2)


def apply_invoice(invoice, ref: Optional[str] = None, sign: int = -1) -> list[dict]:
    """Move stock for every invoice line that matches a catalog product.

    sign=-1 for a sale (stock out), +1 to put it back when an invoice is rectified.
    Lines that do not match a product are skipped: free-text services are the normal
    case, not an error. Returns the products that ended up at or below reorder point.
    """
    touched: list[int] = []
    for item in invoice.items:
        product = find_by_name(item.description)
        if product is None or not product["track_stock"]:
            continue
        move(
            product["id"],
            sign * abs(item.quantity),
            reason=SALE,
            ref=ref or invoice.invoice_number,
        )
        touched.append(product["id"])

    if not touched:
        return []
    low = {p["id"]: p for p in low_stock()}
    return [low[pid] for pid in dict.fromkeys(touched) if pid in low]
