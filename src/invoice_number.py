import json
from pathlib import Path
from datetime import date

COUNTER_FILE = Path("data/invoice_counter.json")


def get_next_invoice_number() -> str:
    COUNTER_FILE.parent.mkdir(parents=True, exist_ok=True)

    if COUNTER_FILE.exists():
        with open(COUNTER_FILE, "r") as f:
            data = json.load(f)
    else:
        data = {"year": date.today().year, "counter": 0}

    current_year = date.today().year
    if data.get("year") != current_year:
        data = {"year": current_year, "counter": 0}

    data["counter"] += 1

    with open(COUNTER_FILE, "w") as f:
        json.dump(data, f)

    return f"{current_year}-{data['counter']:04d}"
