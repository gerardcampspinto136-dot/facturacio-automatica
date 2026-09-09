"""The one-time import of data written by the earlier JSON-file store.

This runs exactly once against the user's real invoices, so it is worth pinning down.
"""

import json
from datetime import date

import pytest

from src import store
from src.invoice_number import get_next_invoice_number


@pytest.fixture
def json_data(tmp_path, monkeypatch):
    """Point the migration at a throwaway set of legacy JSON files."""
    pending = tmp_path / "pending"
    issued = tmp_path / "issued"
    pending.mkdir()
    issued.mkdir()
    monkeypatch.setattr(store, "PENDING_DIR", pending)
    monkeypatch.setattr(store, "ISSUED_DIR", issued)
    monkeypatch.chdir(tmp_path)
    return {"pending": pending, "issued": issued, "root": tmp_path}


def write_pending(d, token, name):
    (d / f"{token}.json").write_text(json.dumps({
        "token": token,
        "created": "2026-07-01T10:00:00",
        "draft_path": f"data/invoices/{token}.pdf",
        "invoice": {
            "client_name": name,
            "client_email": "admin@ejemplo.es",
            "client_address": "Carrer Gran 1, Igualada",
            "client_id": "B12345678",
            "invoice_number": None,
            "date": "2026-07-01",
            "notes": "Pendiente de revisar",
            "rectifies": None,
            "items": [
                {"description": "Reparación", "quantity": 2,
                 "unit_price": 150.0, "total": 300.0}
            ],
        },
    }), encoding="utf-8")


def write_issued(d, number, name, rectified_by=None):
    (d / f"{number}.json").write_text(json.dumps({
        "issued_at": "2026-06-15T09:30:00",
        "rectified_by": rectified_by,
        "invoice": {
            "client_name": name,
            "client_email": "admin@ejemplo.es",
            "client_address": None,
            "client_id": None,
            "invoice_number": number,
            "date": "2026-06-15",
            "notes": None,
            "rectifies": None,
            "items": [
                {"description": "Mano de obra", "quantity": 3,
                 "unit_price": 45.0, "total": 135.0}
            ],
        },
    }), encoding="utf-8")


class TestMigration:
    def test_imports_pending_with_items_intact(self, json_data):
        write_pending(json_data["pending"], "abc123def456", "Talleres Mario S.L.")

        result = store.migrate_from_json()
        assert result["pending"] == 1

        payload = store.get_pending("abc123def456")
        assert payload["created"] == "2026-07-01T10:00:00"
        assert payload["draft_path"] == "data/invoices/abc123def456.pdf"
        assert payload["invoice"].client_name == "Talleres Mario S.L."
        assert payload["invoice"].notes == "Pendiente de revisar"
        assert payload["invoice"].date == date(2026, 7, 1)
        assert payload["invoice"].items[0].total == 300.0

    def test_imports_issued_including_rectification_links(self, json_data):
        write_issued(json_data["issued"], "2026-0001", "Cliente A", rectified_by="R-2026-0001")
        write_issued(json_data["issued"], "2026-0002", "Cliente B")

        result = store.migrate_from_json()
        assert result["issued"] == 2

        assert store.get_issued("2026-0001")["rectified_by"] == "R-2026-0001"
        assert store.get_issued("2026-0002")["rectified_by"] is None
        assert store.get_issued("2026-0002")["invoice"].items[0].unit_price == 45.0

    def test_imports_the_per_series_counter(self, json_data):
        (json_data["root"] / "data").mkdir()
        (json_data["root"] / "data" / "invoice_counter.json").write_text(json.dumps({
            "": {"year": date.today().year, "counter": 42},
            "R": {"year": date.today().year, "counter": 3},
        }), encoding="utf-8")

        store.migrate_from_json()

        year = date.today().year
        assert get_next_invoice_number() == f"{year}-0043"
        assert get_next_invoice_number("R") == f"R-{year}-0004"

    def test_imports_the_legacy_flat_counter(self, json_data):
        (json_data["root"] / "data").mkdir()
        (json_data["root"] / "data" / "invoice_counter.json").write_text(
            json.dumps({"year": date.today().year, "counter": 17}), encoding="utf-8"
        )

        store.migrate_from_json()
        assert get_next_invoice_number() == f"{date.today().year}-0018"

    def test_runs_only_once(self, json_data):
        write_pending(json_data["pending"], "abc123def456", "Talleres Mario")

        assert store.migrate_from_json()["pending"] == 1
        second = store.migrate_from_json()
        assert second["skipped"] is True
        assert store.count_pending() == 1

    def test_deleted_records_are_not_resurrected(self, json_data):
        write_pending(json_data["pending"], "abc123def456", "Talleres Mario")
        store.migrate_from_json()

        store.remove_pending("abc123def456")
        store.migrate_from_json()
        assert store.count_pending() == 0

    def test_leaves_the_json_files_in_place_as_a_backup(self, json_data):
        write_pending(json_data["pending"], "abc123def456", "Talleres Mario")
        store.migrate_from_json()
        assert (json_data["pending"] / "abc123def456.json").exists()

    def test_survives_a_corrupt_file(self, json_data):
        (json_data["pending"] / "broken.json").write_text("{not json", encoding="utf-8")
        write_pending(json_data["pending"], "abc123def456", "Talleres Mario")

        assert store.migrate_from_json()["pending"] == 1

    def test_nothing_to_import_is_fine(self, json_data):
        result = store.migrate_from_json()
        assert result == {"skipped": False, "pending": 0, "issued": 0, "counters": 0}
