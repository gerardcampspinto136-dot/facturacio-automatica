"""Receipt capture: what the vision model returns, and what gets filed from it.

The model call itself is never made here. `build_receipt` takes the parsed JSON, so
every rule that matters -- date order, arithmetic that does not add up, an invented
total, an already-paid ticket -- is tested against a dict, and the dialogue is driven
end to end without a network or a camera.
"""

from datetime import date, timedelta

import pytest

from src import bills, contacts, receipts


def raw(**overrides) -> dict:
    """A clean extraction of the Ferretería Puig invoice, before any override."""
    data = {
        "supplier_name": "Ferretería Puig, S.L.",
        "supplier_tax_id": "B62345678",
        "reference": "F-2026/0412",
        "date": "2026-09-11",
        "due_date": "2026-10-11",
        "subtotal": 200.50,
        "tax_amount": 42.11,
        "tax_rate": 21,
        "total": 242.61,
        "category": "material",
        "payment_method": "transferencia",
        "paid": False,
        "confidence": 0.95,
        "notes": None,
    }
    data.update(overrides)
    return data


# ── Reading the figures ──────────────────────────────────────────────────────

def test_a_clean_receipt_is_read_as_is():
    r = receipts.build_receipt(raw())
    assert r.supplier_name == "Ferretería Puig, S.L."
    assert r.reference == "F-2026/0412"
    assert r.date == date(2026, 9, 11)
    assert r.due_date == date(2026, 10, 11)
    assert (r.subtotal, r.tax_amount, r.total) == (200.50, 42.11, 242.61)
    assert r.category == "material"
    assert r.complete
    assert r.flags == []


def test_spanish_number_formats_survive():
    r = receipts.build_receipt(
        raw(total="1.242,61 €", subtotal="1.026,95", tax_amount="215,66")
    )
    assert r.total == 1242.61
    assert r.subtotal == 1026.95


def test_a_day_first_date_is_not_read_as_month_first():
    assert receipts.build_receipt(raw(date="11/09/2026")).date == date(2026, 9, 11)


def test_an_unreadable_date_does_not_sink_the_receipt():
    r = receipts.build_receipt(raw(date="ilegible"))
    assert r.date is None
    assert r.complete


def test_a_future_date_is_flagged_but_accepted():
    ahead = (date.today() + timedelta(days=30)).isoformat()
    r = receipts.build_receipt(raw(date=ahead))
    assert r.complete
    assert any("futuro" in f for f in r.flags)


def test_an_unknown_category_falls_back_to_otros():
    assert receipts.build_receipt(raw(category="ferretería")).category == "otros"


# ── Arithmetic the model got wrong ───────────────────────────────────────────

def test_parts_that_do_not_add_up_are_recomputed_from_the_total_and_flagged():
    # A misread base: 100.50 + 42.11 is nowhere near 242.61.
    r = receipts.build_receipt(raw(subtotal=100.50))
    assert r.total == 242.61
    assert r.subtotal == pytest.approx(200.50, abs=0.01)
    assert r.tax_amount == pytest.approx(42.11, abs=0.01)
    assert any("no cuadra" in f for f in r.flags)


def test_a_missing_base_is_derived_from_the_total():
    r = receipts.build_receipt(raw(subtotal=None, tax_amount=None))
    assert r.subtotal == pytest.approx(200.50, abs=0.01)
    assert r.tax_amount == pytest.approx(42.11, abs=0.01)
    assert round(r.subtotal + r.tax_amount, 2) == r.total


def test_a_supermarket_ticket_keeps_the_vat_it_read_and_rebuilds_the_base():
    # Two VAT rates on one ticket: 9.49 + 0.94 at 10%, 10.33 + 2.17 at 21%. The bases
    # were misread, the VAT lines were not -- and the VAT is the deductible figure, so
    # it survives and the base is derived from the total.
    r = receipts.build_receipt(
        raw(subtotal=20.82, tax_amount=3.11, tax_rate=None, total=22.93,
            due_date=None, paid=True)
    )
    assert r.tax_amount == 3.11
    assert r.subtotal == pytest.approx(19.82, abs=0.01)
    assert round(r.subtotal + r.tax_amount, 2) == 22.93
    assert any("no cuadra" in f for f in r.flags)


def test_an_impossible_vat_figure_is_discarded_rather_than_kept():
    # VAT larger than the total cannot be right at any rate, so both parts go and the
    # company rate rebuilds them.
    r = receipts.build_receipt(raw(subtotal=200.50, tax_amount=300.0, total=242.61))
    assert r.tax_amount == pytest.approx(42.11, abs=0.01)
    assert r.subtotal == pytest.approx(200.50, abs=0.01)
    assert any("no cuadra" in f for f in r.flags)


def test_a_vat_figure_implying_an_absurd_rate_is_discarded():
    # 90 of VAT on a 242.61 total implies 59%; no Spanish rate is close.
    r = receipts.build_receipt(raw(subtotal=100.0, tax_amount=90.0, total=242.61))
    assert r.tax_amount == pytest.approx(42.11, abs=0.01)


def test_a_missing_base_uses_the_rate_printed_on_the_document():
    r = receipts.build_receipt(
        raw(subtotal=None, tax_amount=None, tax_rate=10, total=110.0)
    )
    assert r.subtotal == pytest.approx(100.0, abs=0.01)
    assert r.tax_amount == pytest.approx(10.0, abs=0.01)


# ── What must never be invented ──────────────────────────────────────────────

def test_an_unreadable_total_is_left_empty_rather_than_guessed():
    r = receipts.build_receipt(raw(total=None))
    assert r.total is None
    assert not r.complete


def test_a_zero_total_is_treated_as_a_misread():
    assert receipts.build_receipt(raw(total=0)).total is None


def test_a_missing_supplier_makes_the_receipt_incomplete():
    assert not receipts.build_receipt(raw(supplier_name="")).complete


def test_a_photo_that_is_not_a_receipt_is_rejected_rather_than_interrogated():
    blank = receipts.build_receipt({
        "supplier_name": None, "total": None, "date": None, "reference": None,
        "confidence": 0.0, "notes": "The image shows a cat.",
    })
    assert not blank.looks_like_a_document

    s = session()
    out = texts(s.start(blank))
    assert "no veo ningún ticket" in out.lower()
    assert not s.active


def test_a_half_read_receipt_is_still_worth_asking_about():
    partial = receipts.build_receipt(raw(supplier_name="", total=None))
    assert partial.looks_like_a_document  # the date and number came through
    s = session()
    s.start(partial)
    assert s.active


# ── Filing it ────────────────────────────────────────────────────────────────

def test_recording_creates_the_bill_and_the_supplier():
    bill_id = receipts.record(receipts.build_receipt(raw()))

    bill = bills.get(bill_id)
    assert bill["supplier_name"] == "Ferretería Puig, S.L."
    assert bill["total"] == 242.61
    assert bill["reference"] == "F-2026/0412"
    assert bill["due_date"] == "2026-10-11"
    assert bill["category"] == "material"
    assert not bill["paid"]

    supplier = contacts.get(bill["supplier_id"])
    assert supplier["kind"] == contacts.SUPPLIER
    assert supplier["tax_id"] == "B62345678"


def test_a_card_ticket_is_filed_as_already_paid():
    r = receipts.build_receipt(
        raw(paid=True, payment_method="tarjeta", due_date=None)
    )
    bill = bills.get(receipts.record(r))
    assert bill["paid"]
    # Already paid means nothing is owed, so it must not show up in the reminders.
    assert bills.total_owed() == 0
    assert bills.due_soon(within_days=365) == []


def test_an_unpaid_bill_does_show_up_as_owed():
    receipts.record(receipts.build_receipt(raw()))
    assert bills.total_owed() == 242.61


def test_the_second_receipt_from_a_supplier_reuses_the_contact():
    receipts.record(receipts.build_receipt(raw()))
    receipts.record(receipts.build_receipt(raw(reference="F-2026/0500")))
    assert len(contacts.list_all(contacts.SUPPLIER)) == 1


def test_a_known_supplier_keeps_the_tax_id_already_on_file():
    contacts.create(contacts.SUPPLIER, "Ferretería Puig, S.L.", tax_id="B00000000")
    receipts.record(receipts.build_receipt(raw()))
    assert contacts.list_all(contacts.SUPPLIER)[0]["tax_id"] == "B00000000"


def test_recording_an_incomplete_receipt_is_refused():
    with pytest.raises(ValueError):
        receipts.record(receipts.build_receipt(raw(total=None)))


def test_the_photo_is_archived_next_to_the_bill(tmp_path, monkeypatch):
    monkeypatch.setattr(receipts, "RECEIPTS_DIR", tmp_path / "receipts")
    photo = tmp_path / "IMG_0042.jpg"
    photo.write_bytes(b"not really a jpeg")

    r = receipts.build_receipt(raw())
    r.image_path = str(photo)
    bill = bills.get(receipts.record(r))

    stored = bill["file_path"]
    assert stored and "2026" in stored
    assert "Ferreteria_Puig" in stored or "Ferreter" in stored
    with open(stored, "rb") as f:
        assert f.read() == b"not really a jpeg"


def test_archiving_the_same_photo_twice_does_not_duplicate_it(tmp_path, monkeypatch):
    monkeypatch.setattr(receipts, "RECEIPTS_DIR", tmp_path / "receipts")
    photo = tmp_path / "IMG_0042.jpg"
    photo.write_bytes(b"x")

    r = receipts.build_receipt(raw())
    once = receipts.archive_image(str(photo), r)
    twice = receipts.archive_image(once, r)
    assert once == twice
    assert len(list((tmp_path / "receipts" / "2026").iterdir())) == 1


# ── The confirmation dialogue ────────────────────────────────────────────────

def session() -> receipts.ExpenseSession:
    return receipts.ExpenseSession()


def texts(replies) -> str:
    return "\n".join(r.text for r in replies)


def test_a_complete_receipt_goes_straight_to_the_summary():
    s = session()
    out = texts(s.start(receipts.build_receipt(raw())))
    assert "Ferretería Puig" in out
    assert "242,61" in out or "242.61" in out
    assert s.awaiting == receipts.AWAIT_CONFIRM


def test_nothing_is_recorded_until_it_is_confirmed():
    s = session()
    s.start(receipts.build_receipt(raw()))
    assert bills.list_all() == []
    s.confirm()
    assert len(bills.list_all()) == 1


def test_confirming_by_voice_or_text_works_like_the_button():
    s = session()
    s.start(receipts.build_receipt(raw()))
    out = texts(s.handle_text("sí, anótalo"))
    assert "Anotado" in out
    assert len(bills.list_all()) == 1
    assert not s.active


def test_saying_no_discards_it():
    s = session()
    s.start(receipts.build_receipt(raw()))
    assert "descartado" in texts(s.handle_text("no")).lower()
    assert bills.list_all() == []
    assert not s.active


def test_an_unreadable_total_is_asked_for_and_then_used():
    s = session()
    out = texts(s.start(receipts.build_receipt(raw(total=None))))
    assert "importe" in out.lower()
    assert s.awaiting == receipts.AWAIT_TOTAL

    s.handle_text("242,61")
    assert s.awaiting == receipts.AWAIT_CONFIRM
    assert s.receipt.total == 242.61
    # The base has to be rebuilt from the corrected total, not left as the model read it.
    assert round(s.receipt.subtotal + s.receipt.tax_amount, 2) == 242.61

    s.confirm()
    assert bills.list_all()[0]["total"] == 242.61


def test_a_nonsense_answer_to_the_amount_is_rejected():
    s = session()
    s.start(receipts.build_receipt(raw(total=None)))
    assert "no he entendido" in texts(s.handle_text("pues bastante")).lower()
    assert s.awaiting == receipts.AWAIT_TOTAL


def test_an_unreadable_supplier_is_asked_for_first():
    s = session()
    out = texts(s.start(receipts.build_receipt(raw(supplier_name=""))))
    assert "proveedor" in out.lower()

    s.handle_text("Ferretería Puig")
    assert s.awaiting == receipts.AWAIT_CONFIRM
    s.confirm()
    assert bills.list_all()[0]["supplier_name"] == "Ferretería Puig"


def test_confirming_the_total_does_not_change_the_vat_that_was_read():
    # Retyping the same total must not turn a correctly read 3.11 of VAT into the
    # 3.98 that the company's default 21% would imply -- that is deductible VAT
    # invented out of nothing.
    s = session()
    s.start(receipts.build_receipt(
        raw(subtotal=20.82, tax_amount=3.11, tax_rate=None, total=22.93,
            due_date=None, paid=True)
    ))
    assert s.receipt.tax_amount == 3.11

    s.handle_text("22,93")
    assert s.receipt.total == 22.93
    assert s.receipt.tax_amount == pytest.approx(3.11, abs=0.01)
    assert s.receipt.subtotal == pytest.approx(19.82, abs=0.01)


def test_correcting_the_total_keeps_the_documents_own_rate():
    # A 10% restaurant ticket whose total was misread stays at 10%, not the 21% default.
    s = session()
    s.start(receipts.build_receipt(
        raw(subtotal=27.82, tax_amount=2.78, tax_rate=10, total=30.60,
            due_date=None, paid=True)
    ))
    s.handle_text("60,00")
    assert s.receipt.total == 60.00
    assert s.receipt.subtotal == pytest.approx(54.55, abs=0.02)
    assert s.receipt.tax_amount == pytest.approx(5.45, abs=0.02)


def test_a_blended_rate_is_not_shown_as_if_it_were_a_real_vat_rate():
    r = receipts.build_receipt(
        raw(subtotal=20.82, tax_amount=3.11, tax_rate=None, total=22.93)
    )
    r.tax_rate = 15.69
    assert "15,69%" not in r.summary() and "15.69%" not in r.summary()


def test_a_real_rate_is_shown():
    r = receipts.build_receipt(raw(tax_rate=10, subtotal=27.82,
                                   tax_amount=2.78, total=30.60))
    assert "(10%)" in r.summary()


def test_a_bare_number_at_the_summary_corrects_the_total():
    s = session()
    s.start(receipts.build_receipt(raw()))
    out = texts(s.handle_text("242,00"))
    assert "Corregido" in out
    assert s.receipt.total == 242.00
    assert s.awaiting == receipts.AWAIT_CONFIRM


def test_a_sentence_at_the_summary_is_not_read_as_a_correction():
    s = session()
    s.start(receipts.build_receipt(raw()))
    s.handle_text("y esto qué es")
    assert s.receipt.total == 242.61


def test_marking_it_paid_clears_the_due_date():
    s = session()
    s.start(receipts.build_receipt(raw()))
    s.toggle_paid()
    assert s.receipt.paid
    assert s.receipt.due_date is None

    bill = bills.get(receipts.record(s.receipt))
    assert bill["paid"]


def test_discarding_deletes_the_archived_photo(tmp_path, monkeypatch):
    monkeypatch.setattr(receipts, "RECEIPTS_DIR", tmp_path / "receipts")
    photo = tmp_path / "IMG_0042.jpg"
    photo.write_bytes(b"x")

    r = receipts.build_receipt(raw())
    r.image_path = receipts.archive_image(str(photo), r)
    s = session()
    s.start(r)
    s.cancel()

    assert not (tmp_path / "receipts" / "2026").exists() or not list(
        (tmp_path / "receipts" / "2026").iterdir()
    )


def test_a_failure_to_record_is_reported_not_raised(monkeypatch):
    s = session()
    s.start(receipts.build_receipt(raw()))

    def boom(*_args, **_kwargs):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(receipts, "record", boom)
    assert "no he podido" in texts(s.confirm()).lower()


# ── Rejecting what it cannot read ────────────────────────────────────────────

def test_a_pdf_is_refused_with_an_explanation(tmp_path):
    doc = tmp_path / "factura.pdf"
    doc.write_bytes(b"%PDF-1.4")
    with pytest.raises(receipts.ReceiptError, match="foto"):
        receipts.extract_receipt(str(doc))


def test_an_empty_image_is_refused(tmp_path):
    photo = tmp_path / "empty.jpg"
    photo.write_bytes(b"")
    with pytest.raises(receipts.ReceiptError):
        receipts.extract_receipt(str(photo))


# ── Surviving a busy free tier ───────────────────────────────────────────────

class _Busy(Exception):
    status_code = 503


class _BadRequest(Exception):
    status_code = 400


@pytest.fixture
def groq_env(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    monkeypatch.setattr(receipts, "_sleep", lambda _s: None)
    monkeypatch.setattr("groq.Groq", lambda **_kw: object())


def test_a_busy_model_is_retried_and_then_succeeds(groq_env, monkeypatch):
    calls = []

    def flaky(_client, model, *_args, **_kw):
        calls.append(model)
        if len(calls) < 3:
            raise _Busy("over capacity")
        return '{"supplier_name": "Puig", "total": 10}'

    monkeypatch.setattr(receipts, "_groq_once", flaky)
    assert "Puig" in receipts._complete_groq("x", "image/png", None)
    assert len(calls) == 3


def test_a_model_that_stays_busy_falls_back_to_the_other_one(groq_env, monkeypatch):
    seen = []

    def busy_then_ok(_client, model, *_args, **_kw):
        seen.append(model)
        if model == receipts.GROQ_VISION_MODEL:
            raise _Busy("over capacity")
        return '{"supplier_name": "Puig", "total": 10}'

    monkeypatch.setattr(receipts, "_groq_once", busy_then_ok)
    receipts._complete_groq("x", "image/png", None)
    assert receipts.GROQ_VISION_FALLBACK in seen


def test_both_models_busy_gives_the_user_something_to_do(groq_env, monkeypatch):
    monkeypatch.setattr(
        receipts, "_groq_once",
        lambda *_a, **_kw: (_ for _ in ()).throw(_Busy("over capacity")),
    )
    with pytest.raises(receipts.ReceiptError, match="saturados"):
        receipts._complete_groq("x", "image/png", None)


class _DailyLimit(Exception):
    status_code = 429

    def __str__(self):
        return ("Rate limit reached ... on tokens per day (TPD): Limit 200000. "
                "Please try again in 8m27.168s.")


def test_a_daily_quota_is_not_sat_through(groq_env, monkeypatch):
    calls = []

    def limited(_client, model, *_args, **_kw):
        calls.append(model)
        raise _DailyLimit()

    monkeypatch.setattr(receipts, "_groq_once", limited)
    with pytest.raises(receipts.ReceiptError, match="cuota gratuita"):
        receipts._complete_groq("x", "image/png", None)
    # One try per model, not four: the limit will not clear for minutes.
    assert len(calls) == 2


def test_the_wait_is_read_off_the_rate_limit_message():
    assert receipts._retry_after_seconds(_DailyLimit()) == pytest.approx(507.168)
    assert receipts._retry_after_seconds(Exception("try again in 12.5s")) == 12.5
    assert receipts._retry_after_seconds(Exception("nope")) is None


def test_a_short_rate_limit_is_still_retried(groq_env, monkeypatch):
    calls = []

    def brief(_client, model, *_args, **_kw):
        calls.append(model)
        if len(calls) < 2:
            raise _Busy("try again in 3.2s")
        return '{"supplier_name": "Puig", "total": 10}'

    monkeypatch.setattr(receipts, "_groq_once", brief)
    assert "Puig" in receipts._complete_groq("x", "image/png", None)


def test_a_bad_request_is_not_retried(groq_env, monkeypatch):
    calls = []

    def bad(_client, model, *_args, **_kw):
        calls.append(model)
        raise _BadRequest("malformed image")

    monkeypatch.setattr(receipts, "_groq_once", bad)
    with pytest.raises(_BadRequest):
        receipts._complete_groq("x", "image/png", None)
    assert len(calls) == 1


class _JsonRefused(Exception):
    status_code = 400

    def __str__(self):
        return "Error code: 400 - {'code': 'json_validate_failed'}"


def test_a_reasoning_model_is_asked_again_without_json_mode(groq_env, monkeypatch):
    modes = []

    def picky(_client, _model, *_args, json_mode=True):
        modes.append(json_mode)
        if json_mode:
            raise _JsonRefused()
        return '<think>Let me look...</think>\n{"supplier_name": "Can Pere", "total": 30.6}'

    monkeypatch.setattr(receipts, "_groq_once", picky)
    out = receipts._complete_groq("x", "image/png", None)
    assert modes == [True, False]
    assert receipts._extract_json(out, "groq")["supplier_name"] == "Can Pere"


# ── Digging the object out of a reasoning model's answer ─────────────────────

def test_plain_json_is_read_as_is():
    assert receipts._extract_json('{"total": 10}', "groq")["total"] == 10


def test_a_think_block_full_of_braces_is_ignored():
    text = (
        '<think>Maybe {"total": 99} is right? No, look again: {"total": 1}</think>\n'
        '```json\n{"supplier_name": "Can Pere", "total": 30.6}\n```'
    )
    data = receipts._extract_json(text, "groq")
    assert data == {"supplier_name": "Can Pere", "total": 30.6}


def test_an_unterminated_think_block_does_not_swallow_everything():
    text = '<think>thinking about {"total": 99}'
    with pytest.raises(ValueError):
        receipts._extract_json(text, "groq")


def test_the_conclusion_wins_over_the_working():
    text = 'First I thought {"total": 99}. Final answer:\n{"total": 30.6}'
    assert receipts._extract_json(text, "groq")["total"] == 30.6


def test_a_nested_object_is_kept_whole():
    text = 'Here:\n{"total": 10, "meta": {"rate": 21}}'
    assert receipts._extract_json(text, "groq")["meta"] == {"rate": 21}


def test_no_json_at_all_is_an_error():
    with pytest.raises(ValueError, match="No JSON"):
        receipts._extract_json("no puedo leer la imagen", "groq")
