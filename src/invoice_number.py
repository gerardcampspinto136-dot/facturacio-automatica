"""Gap-free, per-series invoice numbering.

The counter lives in the `counters` table rather than a JSON file so that assigning a
number and writing the invoice that uses it happen in one transaction. Under the old
file-based counter, a crash between the two left a number consumed with no invoice
behind it, which is exactly the gap the tax rules forbid.

series=""  -> "2026-0001"    (normal invoices, when the company uses no series)
series="A" -> "A-2026-0001"  (a company that numbers its invoices in series A)
series="R" -> "R-2026-0001"  (rectifying / contra invoices)
series="P" -> "P-2026-0001"  (quotes -- not invoices, but numbered the same way)
"""

import re
from datetime import date
from typing import Optional

from src import db

# Taken by the software itself, so a company cannot choose them for its invoices.
RESERVED_SERIES = {"R", "P"}


def clean_series(value: Optional[str]) -> str:
    """Normalise a series typed into the panel. Raises ValueError when unusable."""
    series = (value or "").strip().upper()
    if not series:
        return ""
    if not re.fullmatch(r"[A-Z]{1,5}", series):
        raise ValueError("La serie solo puede llevar letras (hasta 5), por ejemplo «A» o «TM».")
    if series in RESERVED_SERIES:
        raise ValueError(f"La serie «{series}» la usa el programa (R = rectificativas, "
                         "P = presupuestos). Elige otra.")
    return series


def format_number(series: str, year: int, counter: int) -> str:
    prefix = f"{series}-" if series else ""
    return f"{prefix}{year}-{counter:04d}"


def next_number(conn, series: str = "", year: Optional[int] = None) -> str:
    """Consume the next number inside a transaction the caller already holds.

    This is the one to use when issuing: the invoice is written in the same
    transaction, so if anything fails the number is rolled back with it.
    """
    year = year or date.today().year
    conn.execute(
        "INSERT OR IGNORE INTO counters (series, year, counter) VALUES (?, ?, 0)",
        (series, year),
    )
    conn.execute(
        "UPDATE counters SET counter = counter + 1 WHERE series = ? AND year = ?",
        (series, year),
    )
    counter = conn.execute(
        "SELECT counter FROM counters WHERE series = ? AND year = ?", (series, year)
    ).fetchone()[0]
    return format_number(series, year, counter)


def get_next_invoice_number(series: str = "") -> str:
    """Consume and return the next number in `series` for the current year."""
    with db.transaction() as conn:
        return next_number(conn, series)


def peek_invoice_number(series: str = "") -> str:
    """The number the next invoice would take, without consuming it."""
    current_year = date.today().year
    conn = db.connect()
    row = conn.execute(
        "SELECT counter FROM counters WHERE series = ? AND year = ?",
        (series, current_year),
    ).fetchone()
    return format_number(series, current_year, (row[0] if row else 0) + 1)
