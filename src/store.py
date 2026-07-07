"""Persistent stores for invoices.

- Pending queue: invoices awaiting review, one JSON file per draft in data/pending/.
- Issued record: finalized invoices, one JSON file per number in data/issued/ — kept so a
  contra / rectifying invoice can be issued later from the original data.
"""

import json
import os
import uuid
from datetime import date, datetime
from pathlib import Path
from typing import Optional

from src.models import InvoiceData, InvoiceItem

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
    )


# ── Pending queue ────────────────────────────────────────────────────────────

def add_pending(invoice: InvoiceData, draft_path: str) -> str:
    PENDING_DIR.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex[:12]
    payload = {
        "token": token,
        "created": datetime.now().isoformat(timespec="seconds"),
        "draft_path": draft_path,
        "invoice": invoice_to_dict(invoice),
    }
    with open(PENDING_DIR / f"{token}.json", "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return token


def get_pending(token: str) -> Optional[dict]:
    path = PENDING_DIR / f"{token}.json"
    if not path.exists():
        return None
    with open(path, "r", encoding="utf-8") as f:
        payload = json.load(f)
    payload["invoice"] = invoice_from_dict(payload["invoice"])
    return payload


def list_pending() -> list[dict]:
    if not PENDING_DIR.exists():
        return []
    out: list[dict] = []
    for path in sorted(PENDING_DIR.glob("*.json")):
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)
        payload["invoice"] = invoice_from_dict(payload["invoice"])
        out.append(payload)
    out.sort(key=lambda p: p.get("created", ""))
    return out


def count_pending() -> int:
    if not PENDING_DIR.exists():
        return 0
    return sum(1 for _ in PENDING_DIR.glob("*.json"))


def update_pending(token: str, invoice: InvoiceData, draft_path: Optional[str] = None) -> None:
    path = PENDING_DIR / f"{token}.json"
    if not path.exists():
        raise KeyError(f"Pending invoice {token} not found")
    with open(path, "r", encoding="utf-8") as f:
        payload = json.load(f)
    payload["invoice"] = invoice_to_dict(invoice)
    if draft_path is not None:
        payload["draft_path"] = draft_path
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def remove_pending(token: str) -> None:
    path = PENDING_DIR / f"{token}.json"
    # Best-effort cleanup of the draft PDF too.
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                draft = json.load(f).get("draft_path")
            if draft and os.path.exists(draft):
                os.unlink(draft)
        except Exception:
            pass
        path.unlink()


# ── Issued record ────────────────────────────────────────────────────────────

def _issued_path(number: str) -> Path:
    # Invoice numbers contain no path separators, but be safe.
    safe = number.replace("/", "_").replace("\\", "_")
    return ISSUED_DIR / f"{safe}.json"


def record_issued(invoice: InvoiceData) -> None:
    ISSUED_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "issued_at": datetime.now().isoformat(timespec="seconds"),
        "rectified_by": None,
        "invoice": invoice_to_dict(invoice),
    }
    with open(_issued_path(invoice.invoice_number), "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def get_issued(number: str) -> Optional[dict]:
    path = _issued_path(number)
    if not path.exists():
        return None
    with open(path, "r", encoding="utf-8") as f:
        payload = json.load(f)
    payload["invoice"] = invoice_from_dict(payload["invoice"])
    return payload


def list_issued() -> list[dict]:
    if not ISSUED_DIR.exists():
        return []
    out: list[dict] = []
    for path in sorted(ISSUED_DIR.glob("*.json")):
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)
        payload["invoice"] = invoice_from_dict(payload["invoice"])
        out.append(payload)
    out.sort(key=lambda p: p.get("issued_at", ""), reverse=True)
    return out


def mark_rectified(number: str, rectified_by: str) -> None:
    path = _issued_path(number)
    if not path.exists():
        raise KeyError(f"Issued invoice {number} not found")
    with open(path, "r", encoding="utf-8") as f:
        payload = json.load(f)
    payload["rectified_by"] = rectified_by
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
