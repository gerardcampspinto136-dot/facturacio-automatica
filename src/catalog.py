"""Product catalog and stock levels.

Invoice lines used to be free text with a price attached, which meant nothing could be
counted: not how many units are left, not what a thing normally costs, not which items
sell. A catalog entry fixes a name, a price and (optionally) a stock level.

Stock is never written directly. Every change goes through move(), which records the
delta and the resulting balance in stock_moves, so a level that looks wrong can always
be traced back to the movements that produced it.
"""

import re
import unicodedata
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


# Words that carry no identity in a Spanish or Catalan product name, so that
# "3 cajas de tornillos" still finds "Tornillos".
_NOISE = {
    "de", "del", "la", "el", "los", "las", "un", "una", "unos", "unas", "y", "e",
    "con", "sin", "para", "por", "a", "al", "en", "d", "l", "i", "amb", "per",
    "ud", "uds", "unidad", "unidades", "unitat", "unitats", "pack", "caja", "cajas",
    "capsa", "capses", "hora", "horas", "hores",
}


def _normalize(text: str) -> str:
    """Lowercase and strip accents, so "instalación" and "instalacion" are one word."""
    decomposed = unicodedata.normalize("NFD", (text or "").lower())
    return "".join(c for c in decomposed if unicodedata.category(c) != "Mn")


_UNITS = {"mm", "cm", "m", "kg", "g", "l", "ml", "w", "v", "ud", "mts"}


def _singular(word: str) -> str:
    """Fold a Spanish or Catalan plural onto its singular, roughly but consistently.

    Dictation is plural where a catalog name is singular -- "3 brocas widia" against a
    product called "Broca widia 10mm" -- and that one letter was enough to stop stock
    being deducted. Applied to both sides, so it only has to be self-consistent, not
    linguistically correct.
    """
    if len(word) > 4 and word.endswith("es"):
        return word[:-2]
    if len(word) > 3 and word.endswith("s"):
        return word[:-1]
    return word


def _tokens(text: str) -> list[str]:
    """Significant words: no accents, no punctuation, no filler, no bare numbers.

    A measurement said as two words is glued back together ("10 mm" -> "10mm") so it
    matches a catalog name that writes it as one, and stays specific enough to tell a
    10mm bit from a 8mm one.
    """
    words = re.findall(r"[a-z0-9]+", _normalize(text))

    glued: list[str] = []
    for word in words:
        if glued and glued[-1].isdigit() and word in _UNITS:
            glued[-1] += word
        else:
            glued.append(word)

    return [_singular(w) for w in glued if w not in _NOISE and not w.isdigit()]


def find_in_text(text: str) -> Optional[dict]:
    """Find the catalog product that a free-text invoice line is talking about.

    find_by_name() only matches when the spoken words are a substring of the catalog
    name, which is backwards for dictation: a line reads "20 tornillos inox M8", far
    longer than the product called "Tornillos M8". Stock was therefore never deducted
    for anything said naturally.

    So this works the other way round -- a product matches when every significant word
    of its name appears somewhere in the line. The most specific match wins, because
    "Tornillos M8" should beat a product simply called "Tornillos"; a tie between two
    equally specific products is refused rather than guessed at, as elsewhere.
    """
    if not text or not text.strip():
        return None

    line = set(_tokens(text))
    if not line:
        return None

    best: list[dict] = []
    best_score = 0
    for product in list_all():
        name_tokens = _tokens(product["name"])
        if product["sku"]:
            name_tokens = name_tokens or _tokens(product["sku"])
        if not name_tokens or not set(name_tokens) <= line:
            continue
        score = len(set(name_tokens))
        if score > best_score:
            best, best_score = [product], score
        elif score == best_score:
            best.append(product)

    if len(best) == 1:
        return best[0]
    if len(best) > 1:
        return None  # genuinely ambiguous: let a human decide
    return find_by_name(text)


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
    case, not an error.

    Returns one record per movement made -- what moved, by how much, and what is left --
    rather than only the products that fell low. The caller is the one place that can
    tell the user "I took 3 off, 7 left", and silently deducting stock is exactly the
    kind of thing that should be said out loud.
    """
    movements: list[dict] = []
    for item in invoice.items:
        product = find_in_text(item.description)
        if product is None or not product["track_stock"]:
            continue
        quantity = abs(item.quantity)
        balance = move(
            product["id"],
            sign * quantity,
            reason=SALE,
            ref=ref or invoice.invoice_number,
        )
        reorder = product["reorder_point"]
        movements.append({
            "product_id": product["id"],
            "name": product["name"],
            "unit": product["unit"],
            "quantity": quantity,
            "delta": sign * quantity,
            "balance": balance,
            "stock_qty": balance,
            "reorder_point": reorder,
            "low": bool(reorder > 0 and balance <= reorder),
        })
    return movements
