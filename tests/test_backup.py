"""Backups: a consistent copy every day, kept off the machine, and restorable.

A backup that cannot be restored is not a backup, so the restore is tested too: the
copy is taken, the data changes, the copy is put back and the data is as it was.
"""

import zipfile
from datetime import datetime, timedelta

import pytest

from src import backup, db, finalize, store
from src.config_loader import get_config
from src.models import InvoiceData, InvoiceItem


@pytest.fixture(autouse=True)
def folders(tmp_path, monkeypatch):
    monkeypatch.setattr(backup, "BACKUP_DIR", tmp_path / "backups")
    monkeypatch.setattr(backup, "RECEIPTS_DIR", tmp_path / "receipts")
    return tmp_path


def an_invoice():
    return InvoiceData(client_name="Talleres Puig", client_email="t@puig.es",
                       client_id="B87654321", items=[InvoiceItem("Servicio", 1, 100.0)])


def test_a_backup_holds_the_database_and_the_settings(offline):
    finalize.issue(an_invoice())
    path = backup.make_backup()
    names = zipfile.ZipFile(path).namelist()
    assert "facturacio.db" in names and "config/company.yaml" in names
    assert backup.last_backup() is not None


def test_copies_made_in_the_same_second_do_not_overwrite_each_other(folders):
    paths = {backup.make_backup() for _ in range(3)}
    assert len(paths) == 3 and all(p.exists() for p in paths)


def test_only_the_newest_are_kept(folders):
    for _ in range(4):
        backup.make_backup()
    assert backup.prune(keep=2) == 2
    assert len(backup.list_backups()) == 2


def test_restoring_brings_the_data_back(offline, folders):
    finalize.issue(an_invoice())
    saved = backup.make_backup()

    finalize.issue(an_invoice())                    # a second invoice after the copy
    assert len(store.list_issued()) == 2

    safety = backup.restore(str(saved))
    db.connect()
    assert len(store.list_issued()) == 1            # back to the moment of the copy
    assert safety.exists()                          # and the undone state was kept


def test_the_off_site_copy_and_the_receipts_mirror(folders):
    receipt = folders / "receipts" / "2026" / "ticket.jpg"
    receipt.parent.mkdir(parents=True)
    receipt.write_bytes(b"jpeg")
    get_config().backup_copy_to = str(folders / "OneDrive")
    get_config().backup_email = ""

    done = backup.run_due()

    assert (folders / "OneDrive" / done["backup"].name).exists()
    assert (folders / "OneDrive" / "receipts" / "2026" / "ticket.jpg").exists()
    assert done["receipts_mirrored"] == 1
    assert backup.run_due()["receipts_mirrored"] == 0   # unchanged: not copied again


def test_the_weekly_email_goes_once_a_week(offline, folders):
    config = get_config()
    config.backup_copy_to, config.backup_email = "", "yo@empresa.es"

    assert backup.run_due().get("emailed") == "yo@empresa.es"
    assert "emailed" not in backup.run_due()

    backup._meta_set("backup:last_email",
                     (datetime.now() - timedelta(days=8)).isoformat(timespec="seconds"))
    assert backup.run_due().get("emailed") == "yo@empresa.es"
    assert offline[-1][0] == "yo@empresa.es" and offline[-1][2].endswith(".zip")


def test_auto_email_never_goes_to_the_example_company():
    config = get_config()
    config.backup_email = "auto"
    assert config.is_placeholder
    assert backup.email_address() is None


def test_the_panel_lists_and_serves_copies(folders, monkeypatch):
    import base64
    import json

    import itsdangerous
    from fastapi.testclient import TestClient

    from src import accounts

    monkeypatch.delenv("WEB_DEV_NO_AUTH", raising=False)
    monkeypatch.setenv("SESSION_SECRET", "test-secret")
    from src.web import app as web_app

    company = accounts.create_company("Talleres Mario S.L.")
    accounts.create_user("jefe@talleres.es", accounts.ADMIN, company_id=company)
    accounts.create_user("pepe@talleres.es", accounts.EMPLOYEE, company_id=company,
                         permissions=["invoices.view"])
    client = TestClient(web_app.app, follow_redirects=False)

    def sign_in(email):
        signer = itsdangerous.TimestampSigner("test-secret")
        data = base64.b64encode(json.dumps({"user": email}).encode())
        client.cookies.set("session", signer.sign(data).decode())

    sign_in("jefe@talleres.es")
    client.post("/backups/now")
    name = backup.list_backups()[0].name
    assert name in client.get("/backups").text
    assert client.get(f"/backups/{name}").headers["content-type"] == "application/zip"
    # A path smuggled into the name is refused one way or another, never served.
    sneaky = client.get("/backups/..%2F.env")
    assert sneaky.status_code == 404 or "no existe" in sneaky.text
    assert "SMTP" not in sneaky.text

    sign_in("pepe@talleres.es")
    assert "No tienes permiso" in client.get("/backups").text
