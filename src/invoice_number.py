"""Gap-free, per-series invoice numbering.

The counter lives in the `counters` table rather than a JSON file so that assigning a
number and writing the invoice that uses it happen in one transaction. Under the old
file-based counter, a crash between the two left a number consumed with no invoice
behind it, which is exactly the gap the tax rules forbid.

series=""  -> "2026-0001"    (normal invoices)
series="R" -> "R-2026-0001"  (rectifying / contra invoices)
"""

from datetime import date

from src import db


def get_next_invoice_number(series: str = "") -> str:
    """Consume and return the next number in `series` for the current year."""
    current_year = date.today().year
    with db.transaction() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO counters (series, year, counter) VALUES (?, ?, 0)",
            (series, current_year),
        )
        conn.execute(
            "UPDATE counters SET counter = counter + 1 WHERE series = ? AND year = ?",
            (series, current_year),
        )
        counter = conn.execute(
            "SELECT counter FROM counters WHERE series = ? AND year = ?",
            (series, current_year),
        ).fetchone()[0]

    prefix = f"{series}-" if series else ""
    return f"{prefix}{current_year}-{counter:04d}"


def peek_invoice_number(series: str = "") -> str:
    """The number the next invoice would take, without consuming it."""
    current_year = date.today().year
    conn = db.connect()
    row = conn.execute(
        "SELECT counter FROM counters WHERE series = ? AND year = ?",
        (series, current_year),
    ).fetchone()
    prefix = f"{series}-" if series else ""
    return f"{prefix}{current_year}-{(row[0] if row else 0) + 1:04d}"
