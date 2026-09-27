"""Verifactu: fingerprints exactly as the AEAT computes them, chained, and a QR per spec.

The three worked examples are copied from the AEAT's own technical document ("Detalle
de las especificaciones técnicas para la generación de la huella o hash de los
registros", v0.1.2, section 6). If these ever fail, records produced by this software
would be rejected by the AEAT -- nothing else here matters as much.
"""

import sqlite3
from datetime import date
from urllib.parse import parse_qs, urlparse

import pytest

from src import db, finalize, rectify, store, verifactu
from src.models import InvoiceData, InvoiceItem

AEAT_CASE_1 = "3C464DAF61ACB827C65FDA19F352A4E3BDC2C640E9E9FC4CC058073F38F12F60"
AEAT_CASE_2 = "F7B94CFD8924EDFF273501B01EE5153E4CE8F259766F88CF6ACB8935802A2B97"
AEAT_CASE_3 = "177547C0D57AC74748561D054A9CEC14B4C4EA23D1BEFD6F2E69E3A388F90C68"


def an_invoice(**kw):
    return InvoiceData(
        client_name=kw.pop("client_name", "Talleres Puig"),
        client_email=kw.pop("client_email", "taller@puig.es"),
        client_id=kw.pop("client_id", "B87654321"),
        items=kw.pop("items", [InvoiceItem("Reparación", 1, 100.0)]),
        **kw,
    )


# ── The AEAT's own examples ──────────────────────────────────────────────────

class TestOfficialExamples:
    def test_case_1_first_registration_record(self):
        assert verifactu.hash_alta(
            "89890001K", "12345678/G33", "01-01-2024", "F1", "12.35", "123.45", "",
            "2024-01-01T19:20:30+01:00") == AEAT_CASE_1

    def test_case_2_chained_to_the_first(self):
        assert verifactu.hash_alta(
            "89890001K", "12345679/G34", "01-01-2024", "F1", "12.35", "123.45",
            AEAT_CASE_1, "2024-01-01T19:20:35+01:00") == AEAT_CASE_2

    def test_case_3_cancellation_record(self):
        assert verifactu.hash_anulacion(
            "89890001K", "12345679/G34", "01-01-2024", AEAT_CASE_2,
            "2024-01-01T19:20:40+01:00") == AEAT_CASE_3

    def test_spaces_around_a_value_do_not_count(self):
        assert verifactu.hash_alta(
            "  89890001K ", " 12345678/G33 ", "01-01-2024", "F1", "12.35", "123.45", "",
            "2024-01-01T19:20:30+01:00") == AEAT_CASE_1


# ── Registering what this software issues ────────────────────────────────────

class TestRegister:
    def test_every_issued_invoice_gets_a_record(self, offline):
        result = finalize.issue(an_invoice())
        record = verifactu.record_for(result.number)
        assert record["invoice_type"] == "F1"
        assert (record["tax_total"], record["amount_total"]) == ("21.00", "121.00")
        assert record["issue_date"] == date.today().strftime("%d-%m-%Y")
        assert record["previous_hash"] == ""

    def test_records_are_chained(self, offline):
        first = verifactu.record_for(finalize.issue(an_invoice()).number)
        second = verifactu.record_for(finalize.issue(an_invoice()).number)
        assert second["previous_hash"] == first["hash"]

    def test_the_stored_fingerprint_is_the_real_one(self, offline):
        r = verifactu.record_for(finalize.issue(an_invoice()).number)
        assert r["hash"] == verifactu.hash_alta(
            r["issuer_nif"], r["invoice_number"], r["issue_date"], r["invoice_type"],
            r["tax_total"], r["amount_total"], r["previous_hash"], r["generated_at"])

    def test_the_generation_time_carries_its_utc_offset(self, offline):
        r = verifactu.record_for(finalize.issue(an_invoice()).number)
        assert r["generated_at"][-6] in "+-" and r["generated_at"][-3] == ":"

    def test_no_tax_id_is_a_simplified_invoice(self, offline):
        r = verifactu.record_for(finalize.issue(an_invoice(client_id="SIN NIF")).number)
        assert r["invoice_type"] == "F2"

    def test_a_rectifying_invoice_is_registered_as_r1_with_negative_amounts(self, offline):
        original = finalize.issue(an_invoice())
        credit = rectify.rectify(original.number)
        r = verifactu.record_for(credit.number)
        assert r["invoice_type"] == "R1"
        assert (r["tax_total"], r["amount_total"]) == ("-21.00", "-121.00")

    def test_rectifying_a_simplified_invoice_is_r5(self, offline):
        original = finalize.issue(an_invoice(client_id="SIN NIF"))
        credit = rectify.rectify(original.number)
        assert verifactu.record_for(credit.number)["invoice_type"] == "R5"

    def test_the_record_and_the_invoice_live_or_die_together(self, offline, monkeypatch):
        def broken(conn, number):
            raise RuntimeError("fallo al registrar")
        monkeypatch.setattr(verifactu, "register_issued", broken)
        with pytest.raises(RuntimeError):
            finalize.issue(an_invoice())
        assert store.list_issued() == []

    def test_a_record_cannot_be_edited(self, offline):
        number = finalize.issue(an_invoice()).number
        with pytest.raises(sqlite3.DatabaseError):
            with db.transaction() as conn:
                conn.execute("UPDATE verifactu_records SET amount_total = '1.00' "
                             "WHERE invoice_number = ?", (number,))

    def test_a_record_cannot_be_deleted(self, offline):
        number = finalize.issue(an_invoice()).number
        with pytest.raises(sqlite3.DatabaseError):
            with db.transaction() as conn:
                conn.execute("DELETE FROM verifactu_records WHERE invoice_number = ?",
                             (number,))

    def test_it_can_still_be_marked_as_sent(self, offline):
        number = finalize.issue(an_invoice()).number
        with db.transaction() as conn:
            conn.execute("UPDATE verifactu_records SET sent_status = 'sent' "
                         "WHERE invoice_number = ?", (number,))


# ── Checking the chain ───────────────────────────────────────────────────────

class TestVerification:
    def test_an_untouched_chain_is_intact(self, offline):
        for _ in range(3):
            finalize.issue(an_invoice())
        rectify.rectify(store.list_issued()[-1]["invoice"].invoice_number)
        assert verifactu.verify_chain() == (True, [])

    def test_a_record_altered_in_the_file_is_caught(self, offline):
        finalize.issue(an_invoice())
        number = finalize.issue(an_invoice()).number
        conn = db.connect()
        conn.execute("DROP TRIGGER trg_verifactu_frozen")  # as a tamperer would
        conn.execute("UPDATE verifactu_records SET amount_total = '1.00' "
                     "WHERE invoice_number = ?", (number,))
        intact, problems = verifactu.verify_chain()
        assert not intact
        assert any(number in p for p in problems)

    def test_a_record_removed_from_the_middle_breaks_the_chain(self, offline):
        numbers = [finalize.issue(an_invoice()).number for _ in range(3)]
        conn = db.connect()
        conn.execute("DROP TRIGGER trg_verifactu_kept")
        conn.execute("DELETE FROM verifactu_records WHERE invoice_number = ?",
                     (numbers[1],))
        intact, problems = verifactu.verify_chain()
        assert not intact
        assert any(numbers[2] in p and "enlaza" in p for p in problems)

    def test_an_invoice_rewritten_behind_its_record_is_caught(self, offline):
        number = finalize.issue(an_invoice()).number
        conn = db.connect()
        conn.execute("DROP TRIGGER trg_issued_items_no_update")
        conn.execute("UPDATE invoice_items SET total = 50, unit_price = 50 WHERE "
                     "invoice_id = (SELECT id FROM invoices WHERE number = ?)", (number,))
        intact, problems = verifactu.verify_chain()
        assert not intact and any("importes" in p for p in problems)


# ── The panel page ───────────────────────────────────────────────────────────

class TestPanel:
    @pytest.fixture
    def client(self, monkeypatch):
        from fastapi.testclient import TestClient

        from src import accounts

        monkeypatch.delenv("WEB_DEV_NO_AUTH", raising=False)
        monkeypatch.setenv("SESSION_SECRET", "test-secret")
        from src.web import app as web_app

        company = accounts.create_company("Talleres Mario S.L.")
        accounts.create_user("jefe@talleres.es", accounts.ADMIN, company_id=company)
        accounts.create_user("almacen@talleres.es", accounts.EMPLOYEE,
                             company_id=company, permissions=["stock.view"])
        return TestClient(web_app.app, follow_redirects=False)

    def sign_in(self, client, email):
        import base64
        import json

        import itsdangerous

        signer = itsdangerous.TimestampSigner("test-secret")
        data = base64.b64encode(json.dumps({"user": email}).encode())
        client.cookies.set("session", signer.sign(data).decode())

    def test_the_owner_sees_the_register_and_that_it_is_intact(self, client, offline):
        number = finalize.issue(an_invoice()).number
        self.sign_in(client, "jefe@talleres.es")
        page = client.get("/verifactu").text
        assert "Registro íntegro" in page and number in page

    def test_someone_without_invoice_access_does_not(self, client):
        self.sign_in(client, "almacen@talleres.es")
        assert "No tienes permiso" in client.get("/verifactu").text


# ── The QR ───────────────────────────────────────────────────────────────────

class TestQr:
    def test_the_url_follows_the_spec_and_encodes_the_series(self):
        url = verifactu.qr_url("89890001K", "12345678&G33", "01-01-2024", "241.40",
                               verifactu=True, env="test")
        assert url == ("https://prewww2.aeat.es/wlpl/TIKE-CONT/ValidarQR?nif=89890001K"
                       "&numserie=12345678%26G33&fecha=01-01-2024&importe=241.40")

    def test_until_records_reach_the_aeat_it_is_a_no_verifactu_qr(self, offline):
        result = finalize.issue(an_invoice())
        url = verifactu.qr_url_for(result.invoice)
        assert "/ValidarQRNoVerifactu?" in url
        query = parse_qs(urlparse(url).query)
        assert query["numserie"] == [result.number]
        assert query["importe"] == ["121.00"]
        assert query["fecha"] == [date.today().strftime("%d-%m-%Y")]

    def test_an_example_company_points_at_the_test_environment(self, offline):
        result = finalize.issue(an_invoice())
        assert verifactu.qr_url_for(result.invoice).startswith("https://prewww2.aeat.es/")

    def test_the_pdf_carries_the_qr_at_the_top_but_not_the_verifactu_legend(
            self, offline):
        pymupdf = pytest.importorskip("pymupdf")
        result = finalize.issue(an_invoice())
        page = pymupdf.open(result.pdf_path)[0]
        text = page.get_text()
        assert "QR tributario:" in text
        # Asserting the invoice reached the AEAT would be false until it does.
        assert "VERI*FACTU" not in text
        qr_top = page.search_for("QR tributario:")[0].y0
        title_top = page.search_for("FACTURA")[0].y0
        assert qr_top < title_top

    def test_a_draft_has_no_qr(self, tmp_path):
        pymupdf = pytest.importorskip("pymupdf")
        from src.invoice_generator import generate_invoice_pdf

        out = tmp_path / "draft.pdf"
        generate_invoice_pdf(an_invoice(), str(out))
        assert "QR tributario" not in pymupdf.open(str(out))[0].get_text()

    def test_the_qr_decodes_to_the_url(self, offline):
        """Read the printed code back, as a phone would."""
        cv2 = pytest.importorskip("cv2")
        pymupdf = pytest.importorskip("pymupdf")
        import numpy as np

        result = finalize.issue(an_invoice())
        pix = pymupdf.open(result.pdf_path)[0].get_pixmap(dpi=200)
        image = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.h, pix.w, pix.n)
        decoded, _, _ = cv2.QRCodeDetector().detectAndDecode(image)
        assert decoded == verifactu.qr_url_for(result.invoice)
