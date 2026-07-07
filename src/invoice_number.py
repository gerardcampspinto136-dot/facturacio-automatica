import json
from pathlib import Path
from datetime import date

COUNTER_FILE = Path("data/invoice_counter.json")


def get_next_invoice_number(series: str = "") -> str:
    """Return the next gap-free invoice number for the given series.

    series="" → "2026-0001" (normal invoices)
    series="R" → "R-2026-0001" (rectifying / contra invoices)

    Each series keeps its own year-based counter in data/invoice_counter.json.
    """
    COUNTER_FILE.parent.mkdir(parents=True, exist_ok=True)

    data: dict = {}
    if COUNTER_FILE.exists():
        with open(COUNTER_FILE, "r") as f:
            data = json.load(f)

    # Migrate the legacy flat format {"year": ..., "counter": ...} to per-series.
    if "year" in data or "counter" in data:
        data = {"": {"year": data.get("year"), "counter": data.get("counter", 0)}}

    current_year = date.today().year
    entry = data.get(series)
    if not entry or entry.get("year") != current_year:
        entry = {"year": current_year, "counter": 0}

    entry["counter"] += 1
    data[series] = entry

    with open(COUNTER_FILE, "w") as f:
        json.dump(data, f)

    prefix = f"{series}-" if series else ""
    return f"{prefix}{current_year}-{entry['counter']:04d}"
