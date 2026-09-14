"""Photograph a supplier invoice or a till receipt and have it filed as a bill.

Typing an expense into a phone is the step an autónomo skips, and a skipped expense is
deductible VAT thrown away. So the interaction is: take the picture, glance at what the
bot read back, press Guardar. Everything else -- supplier record, due date, category,
whether it is already paid -- is inferred.

The extraction runs on a vision model (Groq by default, free), and is deliberately
conservative: an amount it could not read comes back as null and is asked for, because a
bill silently recorded with the wrong total is worse than one that took a question to
enter. What the model returns is never trusted as arithmetic either -- see
`ReceiptData.reconcile()`.

Like src/conversation.py this module knows nothing about Telegram: it returns Reply
objects, so the whole flow is testable without a network or a camera.
"""

import base64
import json
import logging
import os
import re
import shutil
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Optional

from src import bills, contacts
from src.config_loader import get_config
from src.conversation import Reply, says_no, says_yes
from src.totals import format_money

logger = logging.getLogger(__name__)

# Groq's vision model. Reads a Spanish ticket, honours JSON mode, and costs nothing.
# The free tier does run out of capacity -- a 503 on one of these was seen in testing --
# so a second model stands in rather than losing the photo the user just took.
GROQ_VISION_MODEL = os.getenv("GROQ_VISION_MODEL", "qwen/qwen3.8-27b")
GROQ_VISION_FALLBACK = os.getenv("GROQ_VISION_FALLBACK", "qwen/qwen3.6-27b")
ANTHROPIC_VISION_MODEL = os.getenv("ANTHROPIC_VISION_MODEL", "claude-sonnet-4-6")

# Where the photographed originals are kept. A recorded expense without the document
# behind it is not deductible, so the image is part of the record, not a nicety.
RECEIPTS_DIR = Path("data/receipts")


class ReceiptError(Exception):
    """A problem the user can act on, phrased for them rather than for a log.

    Anything raised as this is shown in the chat verbatim, so it says what to do next
    ("mándame una foto", "vuelve a intentarlo") and never leaks a stack trace.
    """


CATEGORIES = (
    "material", "suministros", "transporte", "dietas", "servicios",
    "software", "alquiler", "seguros", "impuestos", "otros",
)

_MIME = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".webp": "image/webp", ".gif": "image/gif",
}

_SYSTEM_PROMPT = """You read photographs of supplier invoices and receipts (Spain) and return structured data.

Return ONE valid JSON object and nothing else:
{
  "supplier_name": "the business that ISSUED the document",
  "supplier_tax_id": "their CIF/NIF, or null",
  "reference": "their invoice or ticket number, or null",
  "date": "YYYY-MM-DD, or null",
  "due_date": "YYYY-MM-DD only if a due date is printed, else null",
  "subtotal": "number, the taxable base, or null",
  "tax_amount": "number, the VAT/IGIC charged, or null",
  "tax_rate": "number, the VAT percentage, or null",
  "total": "number, the amount actually payable, or null if unreadable",
  "category": "one of: material, suministros, transporte, dietas, servicios, software, alquiler, seguros, impuestos, otros",
  "payment_method": "efectivo | tarjeta | transferencia | domiciliado | null",
  "paid": "true if the document shows it was already paid, else false",
  "confidence": "number 0-1, how legible the document was",
  "notes": "anything odd worth flagging, else null"
}

Rules:
- Amounts are plain numbers with a dot as the decimal separator: "1.242,61" -> 1242.61.
- Spanish dates are DD/MM/YYYY. "11/09/2026" is 2026-09-11, never 2026-11-09.
- "total" is the final amount charged, VAT included -- the largest figure on the
  document, usually labelled TOTAL, TOTAL A PAGAR or IMPORTE.
- The supplier is whoever ISSUED the document, not the customer named on it. On a till
  receipt it is the shop at the top.
- A cash or card ticket is already paid. An invoice with a due date is not.
- NEVER invent a figure. Anything you cannot read with confidence is null, and lower
  "confidence" accordingly. A missing number is fine; a wrong one is not.
- Never translate names: they stay exactly as printed."""


# ── The extracted document ───────────────────────────────────────────────────

@dataclass
class ReceiptData:
    """What the model read off one photograph, after sanity checks."""

    supplier_name: str = ""
    supplier_tax_id: Optional[str] = None
    reference: Optional[str] = None
    date: Optional[date] = None
    due_date: Optional[date] = None
    subtotal: Optional[float] = None
    tax_amount: Optional[float] = None
    tax_rate: Optional[float] = None
    total: Optional[float] = None
    category: str = "otros"
    payment_method: Optional[str] = None
    paid: bool = False
    confidence: float = 0.0
    notes: Optional[str] = None
    # Warnings for the human, not errors: the bill still gets recorded.
    flags: list[str] = field(default_factory=list)
    image_path: Optional[str] = None

    @property
    def complete(self) -> bool:
        """Enough to record: someone to pay and an amount to pay them."""
        return bool(self.supplier_name.strip()) and self.total is not None

    @property
    def looks_like_a_document(self) -> bool:
        """Did the photo contain a receipt at all?

        A holiday snap comes back with every field null, and walking the user through
        "who is the supplier?" and "how much was it?" for a picture of their cat is
        worse than simply saying it is not a receipt. One readable field is enough to
        assume good faith and start asking.
        """
        return bool(
            self.supplier_name.strip() or self.total is not None
            or self.reference or self.date
        )

    def _tax_is_plausible(self) -> bool:
        """Could this VAT figure belong to this total at any legal Spanish rate?

        The ceiling is a little above 21% rather than exactly 21% so that a ticket
        mixing rates, or one rounded oddly, is not thrown out for being a fraction over.
        """
        if self.tax_amount is None or self.total is None:
            return False
        if not 0 <= self.tax_amount < self.total:
            return False
        base = self.total - self.tax_amount
        return base > 0 and (self.tax_amount / base) <= 0.255

    def reconcile(self) -> None:
        """Make base + VAT agree with the total, and say so when they did not.

        The three figures are read independently off the image, so one misread digit
        makes them contradict each other. They are trusted in the order they matter:

        1. The total -- the largest, boldest figure, and the amount that actually left
           the bank.
        2. The VAT -- the figure that gets reclaimed, and the only reason the expense is
           worth photographing.
        3. The base, which is the redundant one: total minus VAT.

        So when the parts do not add up, the VAT is kept and the base is recomputed from
        the total. This is what saves a supermarket ticket, where the 10% and 21% lines
        are listed separately: the VAT lines add up correctly even when a base was
        misread, and a blended rate is not one the company default could ever guess.
        A VAT figure that is itself impossible is discarded instead. Either way the
        discrepancy is surfaced rather than quietly smoothed over.
        """
        if self.total is None:
            return

        rate = self.tax_rate if self.tax_rate else get_config().tax_rate

        if self.subtotal is not None and self.tax_amount is not None:
            if abs(self.subtotal + self.tax_amount - self.total) > 0.02:
                self.flags.append(
                    f"La base ({self.subtotal:.2f}) más el IVA ({self.tax_amount:.2f}) "
                    f"no cuadra con el total ({self.total:.2f})."
                )
                if self._tax_is_plausible():
                    self.subtotal = None  # recomputed from the total below
                else:
                    self.subtotal = self.tax_amount = None

        if self.subtotal is None and self.tax_amount is not None:
            self.subtotal = round(self.total - self.tax_amount, 2)
        elif self.tax_amount is None and self.subtotal is not None:
            self.tax_amount = round(self.total - self.subtotal, 2)
        elif self.subtotal is None and self.tax_amount is None:
            self.subtotal = round(self.total / (1 + rate / 100), 2)
            self.tax_amount = round(self.total - self.subtotal, 2)

    def summary(self) -> str:
        """What the user checks before pressing Guardar."""
        config = get_config()
        lines = [
            "*He leído esto del documento*",
            "",
            f"Proveedor: *{self.supplier_name or '—'}*",
        ]
        if self.supplier_tax_id:
            lines.append(f"CIF/NIF: {self.supplier_tax_id}")
        if self.reference:
            lines.append(f"Nº: {self.reference}")
        lines.append(f"Fecha: {self.date.isoformat() if self.date else '—'}")
        lines.append("")
        if self.subtotal is not None:
            lines.append(f"Base: {format_money(self.subtotal, config)}")
        if self.tax_amount is not None:
            # Only a real Spanish rate is worth naming. A ticket mixing 10% and 21%
            # has a blended rate like 15.7%, which is arithmetic, not a VAT rate, and
            # printing it would just look like a mistake.
            rate = (f" ({self.tax_rate:g}%)"
                    if self.tax_rate in (0, 4, 10, 21) else "")
            lines.append(f"IVA{rate}: {format_money(self.tax_amount, config)}")
        total = format_money(self.total, config) if self.total is not None else "—"
        lines.append(f"*TOTAL: {total}*")
        lines.append("")
        lines.append(f"Categoría: {self.category}")
        lines.append(
            "Estado: *ya pagada*" if self.paid
            else "Estado: *pendiente de pago*"
                 + (f", vence el {self.due_date.isoformat()}" if self.due_date else "")
        )
        if self.notes:
            lines.append(f"Nota: {self.notes}")
        for flag in self.flags:
            lines.append(f"⚠️ {flag}")
        if self.confidence and self.confidence < 0.7:
            lines.append("⚠️ La foto se lee con dificultad. Comprueba los importes.")
        return "\n".join(lines)


# ── Reading the image ────────────────────────────────────────────────────────

def _which_provider() -> str:
    """Same precedence as src/parser.py, but only providers that can see."""
    forced = (os.getenv("LLM_PROVIDER") or "").strip().lower()
    if forced in ("groq", "anthropic"):
        return forced
    if os.getenv("ANTHROPIC_API_KEY"):
        return "anthropic"
    if os.getenv("GROQ_API_KEY"):
        return "groq"
    raise RuntimeError(
        "No LLM key found. Set GROQ_API_KEY (free) or ANTHROPIC_API_KEY in .env"
    )


def _encode(image_path: str) -> tuple[str, str]:
    """Return (base64 data, mime type) for an image the vision model can accept."""
    suffix = Path(image_path).suffix.lower()
    mime = _MIME.get(suffix)
    if mime is None:
        raise ReceiptError(
            f"No sé leer un archivo «{suffix or 'sin extensión'}». "
            "Mándame una foto (JPG o PNG) del documento."
        )
    data = Path(image_path).read_bytes()
    if not data:
        raise ReceiptError("La imagen está vacía.")
    return base64.b64encode(data).decode(), mime


_BE_BRIEF = (
    "\n\nResponde ÚNICAMENTE con el objeto JSON. No razones en voz alta ni expliques "
    "nada antes o después: no hay espacio para ello en la respuesta."
)


def _user_prompt(hint: Optional[str]) -> str:
    if hint and hint.strip():
        # The caption is context, never an instruction: it says what the expense was
        # for, and must not be able to talk the model out of reading the figures.
        return ("Extrae los datos del documento de la imagen. El usuario lo ha descrito "
                f"así, úsalo solo como contexto para la categoría y las notas: «{hint.strip()}»")
    return "Extrae los datos del documento de la imagen."


def _sleep(seconds: float) -> None:
    """Indirection so the retry tests do not actually wait."""
    import time

    time.sleep(seconds)


def _groq_once(client, model: str, b64: str, mime: str, hint: Optional[str],
               json_mode: bool = True) -> str:
    """One call. Without JSON mode the answer is prose around the object, not bare JSON.

    A reasoning model refuses JSON mode outright -- it wants to think out loud first --
    so it gets asked in plain text and needs the headroom to do it.
    """
    extra = {"response_format": {"type": "json_object"}} if json_mode else {}
    # The free tier caps output tokens per minute per model, and rejects the request
    # outright when max_tokens alone exceeds the cap -- so this stays under it rather
    # than asking for headroom the tier will never grant.
    response = client.chat.completions.create(
        model=model,
        messages=[{
            "role": "user",
            "content": [
                {"type": "text", "text": _SYSTEM_PROMPT + "\n\n" + _user_prompt(hint)
                                        + ("" if json_mode else _BE_BRIEF)},
                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
            ],
        }],
        temperature=0,
        max_tokens=1200 if json_mode else 900,
        **extra,
    )
    return response.choices[0].message.content or ""


def _complete_groq(b64: str, mime: str, hint: Optional[str]) -> str:
    """Read the image, retrying the busy free tier before giving up on it.

    Groq answers a saturated model with a 503 telling the caller to back off. The user
    has already taken the photo by this point, so the cost of waiting a second is far
    lower than the cost of making them take it again.
    """
    import groq

    client = groq.Groq(api_key=os.environ["GROQ_API_KEY"])
    last: Exception | None = None

    for model in (GROQ_VISION_MODEL, GROQ_VISION_FALLBACK):
        if not model:
            continue
        json_mode = True
        for attempt in range(4):
            try:
                return _groq_once(client, model, b64, mime, hint, json_mode=json_mode)
            except Exception as exc:
                status = getattr(exc, "status_code", None)
                if _is_json_mode_refusal(exc):
                    # A reasoning model cannot answer in JSON mode. Ask in plain text
                    # instead and pull the object out of the prose afterwards.
                    logger.info("%s refused JSON mode; retrying as plain text", model)
                    json_mode = False
                    continue
                # 4xx other than rate limiting means the request itself is wrong;
                # retrying an unreadable image or a bad key just wastes the user's time.
                if status is not None and 400 <= status < 500 and status != 429:
                    raise

                # A rate limit that will not clear for minutes is not worth sitting
                # through: say so now, with the wait, instead of retrying in silence.
                wait = _retry_after_seconds(exc)
                if wait and wait > 30:
                    logger.warning("%s rate-limited for %.0fs", model, wait)
                    last = exc
                    break
                last = exc
                logger.warning(
                    "Groq vision model %s failed (attempt %d): %s", model, attempt + 1, exc
                )
                # Groq asks callers to back off exponentially. The photo is already
                # taken, so waiting beats making the user shoot it again.
                _sleep(min(2 ** attempt * 2, 12))

    wait = _retry_after_seconds(last) if last else None
    if wait and wait > 30:
        minutes = max(1, round(wait / 60))
        raise ReceiptError(
            f"He agotado la cuota gratuita de lectura de imágenes por hoy. "
            f"Vuelve a mandarme la foto dentro de unos {minutes} minutos, o anótalo "
            f"a mano con /gasto."
        ) from last

    raise ReceiptError(
        "Los modelos de lectura de imágenes están saturados ahora mismo. "
        "Vuelve a mandarme la foto en un minuto."
    ) from last


def _complete_anthropic(b64: str, mime: str, hint: Optional[str]) -> str:
    import anthropic

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    response = client.messages.create(
        model=ANTHROPIC_VISION_MODEL,
        max_tokens=1200,
        system=_SYSTEM_PROMPT,
        temperature=0,
        messages=[
            {"role": "user", "content": [
                {"type": "image", "source": {
                    "type": "base64", "media_type": mime, "data": b64}},
                {"type": "text", "text": _user_prompt(hint)},
            ]},
            {"role": "assistant", "content": "{"},
        ],
    )
    return "{" + response.content[0].text


def _retry_after_seconds(exc: Exception) -> Optional[float]:
    """How long a rate limit says to wait, from its "try again in 8m27.1s" message."""
    match = re.search(r"try again in (?:(\d+)m)?([\d.]+)s", str(exc))
    if not match:
        return None
    return int(match.group(1) or 0) * 60 + float(match.group(2))


def _is_json_mode_refusal(exc: Exception) -> bool:
    """A 400 saying the model could not produce valid JSON, rather than a bad request."""
    if getattr(exc, "status_code", None) != 400:
        return False
    return "json_validate_failed" in str(exc) or "Failed to validate JSON" in str(exc)


def _extract_json(text: str, provider: str) -> dict:
    """Pull the object out of whatever the model wrapped it in.

    A reasoning model answers with a <think> block full of braces, then a fenced code
    block, then the JSON -- so the reasoning is removed first and the LAST object in
    what remains is taken, that being the model's conclusion rather than its workings.
    """
    text = (text or "").strip()
    try:
        return json.loads(text)
    except ValueError:
        pass

    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<think>.*", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"```(?:json)?|```", "", text)

    # Scan for balanced top-level objects and keep the last one that parses. The scan
    # jumps past each object it accepts, so a nested "meta": {...} is never mistaken
    # for the answer.
    found = None
    i = 0
    while i < len(text):
        if text[i] != "{":
            i += 1
            continue
        depth = 0
        for end in range(i, len(text)):
            if text[end] == "{":
                depth += 1
            elif text[end] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        found = json.loads(text[i:end + 1])
                    except ValueError:
                        pass
                    i = end
                    break
        i += 1

    if found is None:
        raise ValueError(f"No JSON in the {provider} response: {text[:200]}")
    return found


def _number(value) -> Optional[float]:
    """Coerce the model's figure into a float, tolerating "1.242,61 €". None if unreadable."""
    if value is None or value == "" or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    cleaned = re.sub(r"[^\d,.\-]", "", str(value))
    if "," in cleaned and "." in cleaned:
        cleaned = cleaned.replace(".", "").replace(",", ".")
    else:
        cleaned = cleaned.replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return None


def _date(value) -> Optional[date]:
    if not value:
        return None
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d/%m/%y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _flag(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("true", "sí", "si", "yes", "1")


def build_receipt(data: dict) -> ReceiptData:
    """Turn the model's JSON into a checked ReceiptData. Pure, so tests need no network."""
    category = str(data.get("category") or "otros").strip().lower()
    if category not in CATEGORIES:
        category = "otros"

    receipt = ReceiptData(
        supplier_name=(data.get("supplier_name") or "").strip(),
        supplier_tax_id=(data.get("supplier_tax_id") or None),
        reference=(data.get("reference") or None),
        date=_date(data.get("date")),
        due_date=_date(data.get("due_date")),
        subtotal=_number(data.get("subtotal")),
        tax_amount=_number(data.get("tax_amount")),
        tax_rate=_number(data.get("tax_rate")),
        total=_number(data.get("total")),
        category=category,
        payment_method=(data.get("payment_method") or None),
        paid=_flag(data.get("paid")),
        confidence=_number(data.get("confidence")) or 0.0,
        notes=(data.get("notes") or None),
    )

    # A total of zero is a misread, not a free lunch.
    if receipt.total is not None and receipt.total <= 0:
        receipt.total = None
    if receipt.date and receipt.date > date.today():
        receipt.flags.append(
            f"La fecha leída ({receipt.date.isoformat()}) está en el futuro. "
            "Compruébala."
        )
    receipt.reconcile()
    return receipt


def extract_receipt(image_path: str, hint: Optional[str] = None) -> ReceiptData:
    """Read one photographed document. `hint` is the user's caption, if any."""
    b64, mime = _encode(image_path)
    provider = _which_provider()
    raw = (_complete_groq(b64, mime, hint) if provider == "groq"
           else _complete_anthropic(b64, mime, hint))
    receipt = build_receipt(_extract_json(raw, provider))
    receipt.image_path = image_path
    return receipt


# ── Filing it ────────────────────────────────────────────────────────────────

def _safe(text: str) -> str:
    return re.sub(r"[^\w\-]+", "_", (text or "").strip())[:40].strip("_") or "gasto"


def _is_archived(image_path: str) -> bool:
    try:
        Path(image_path).resolve().relative_to(RECEIPTS_DIR.resolve())
        return True
    except (ValueError, OSError):
        return False


def archive_image(image_path: str, receipt: ReceiptData) -> Optional[str]:
    """Copy the photo into data/receipts/<year>/ under a name that means something.

    Idempotent: the bot archives as soon as the photo is read, because the download
    lives in a temp file that is deleted the moment the handler returns -- long before
    the user presses Guardar. Calling it again on the archived copy is a no-op.
    """
    if not image_path or not os.path.exists(image_path):
        return None
    if _is_archived(image_path):
        return image_path
    when = receipt.date or date.today()
    target_dir = RECEIPTS_DIR / str(when.year)
    target_dir.mkdir(parents=True, exist_ok=True)

    stem = f"{when.isoformat()}_{_safe(receipt.supplier_name)}"
    if receipt.reference:
        stem += f"_{_safe(receipt.reference)}"
    suffix = Path(image_path).suffix.lower() or ".jpg"

    target = target_dir / f"{stem}{suffix}"
    n = 2
    while target.exists():
        target = target_dir / f"{stem}_{n}{suffix}"
        n += 1

    shutil.copy2(image_path, target)
    return str(target)


def record(receipt: ReceiptData) -> int:
    """File the receipt as a supplier bill and return its id.

    The supplier's tax id is written back onto the contact the first time it is seen,
    so the record improves as more of their documents are photographed.
    """
    if not receipt.complete:
        raise ValueError("Falta el proveedor o el importe.")

    stored_image = archive_image(receipt.image_path, receipt)

    note_parts = [p for p in (receipt.notes, receipt.payment_method and
                              f"pago: {receipt.payment_method}") if p]

    bill_id = bills.create(
        receipt.supplier_name,
        round(receipt.total, 2),
        bill_date=receipt.date or date.today(),
        due_date=receipt.due_date,
        # An already-paid ticket has no credit period: it fell due the day it was issued.
        due_days=0 if (receipt.paid and not receipt.due_date) else None,
        subtotal=receipt.subtotal,
        tax_amount=receipt.tax_amount,
        reference=receipt.reference,
        category=receipt.category,
        notes=" · ".join(note_parts) or None,
        file_path=stored_image,
    )

    if receipt.paid:
        bills.mark_paid(bill_id, receipt.date or date.today())

    if receipt.supplier_tax_id:
        bill = bills.get(bill_id)
        supplier = contacts.get(bill["supplier_id"]) if bill["supplier_id"] else None
        if supplier and not (supplier.get("tax_id") or "").strip():
            contacts.update(supplier["id"], tax_id=receipt.supplier_tax_id.strip())

    return bill_id


# ── The confirmation dialogue ────────────────────────────────────────────────

AWAIT_CONFIRM = "__confirm_expense__"
AWAIT_TOTAL = "__expense_total__"
AWAIT_SUPPLIER = "__expense_supplier__"


@dataclass
class ExpenseSession:
    """One chat's photographed expense, waiting to be confirmed."""

    receipt: Optional[ReceiptData] = None
    awaiting: Optional[str] = None

    @property
    def active(self) -> bool:
        return self.receipt is not None

    def reset(self) -> None:
        self.receipt = None
        self.awaiting = None

    def start(self, receipt: ReceiptData) -> list[Reply]:
        self.reset()
        if not receipt.looks_like_a_document:
            return [Reply(
                "No veo ningún ticket ni factura en esa imagen. "
                "Mándame una foto del documento, con el total visible."
            )]
        self.receipt = receipt
        return self._ask_next()

    def _ask_next(self) -> list[Reply]:
        """Ask for whatever the photo did not give up, then present."""
        if not self.receipt.supplier_name.strip():
            self.awaiting = AWAIT_SUPPLIER
            return [Reply("No he podido leer el nombre del proveedor. ¿De quién es?")]
        if self.receipt.total is None:
            self.awaiting = AWAIT_TOTAL
            return [Reply(
                f"No he podido leer el importe de *{self.receipt.supplier_name}*. "
                "¿Cuánto es en total, con IVA?"
            )]
        return self._present()

    def _present(self) -> list[Reply]:
        self.awaiting = AWAIT_CONFIRM
        paid_label = "⏳ Marcar pendiente" if self.receipt.paid else "💳 Marcar pagada"
        return [Reply(
            self.receipt.summary() + "\n\n¿Lo anoto?",
            buttons=[
                ("✅ Guardar", "exp:save"),
                (paid_label, "exp:toggle_paid"),
                ("❌ Descartar", "exp:cancel"),
            ],
        )]

    def _set_total(self, amount: float) -> None:
        """Apply a total the user typed, keeping the VAT rate already read off the photo.

        Rebuilding the parts from the company's default rate would be wrong whenever the
        document is not at that rate -- a restaurant at 10%, or a supermarket ticket
        whose blended rate is 15.7%. Confirming a total the bot had read correctly would
        then quietly change the deductible VAT, and always upwards, which is the
        dangerous direction. So the rate implied by the current reading is captured
        first and reapplied to the new total.
        """
        receipt = self.receipt
        if receipt.subtotal and receipt.tax_amount is not None and receipt.subtotal > 0:
            receipt.tax_rate = round(receipt.tax_amount / receipt.subtotal * 100, 2)
        receipt.total = round(amount, 2)
        receipt.subtotal = receipt.tax_amount = None
        receipt.reconcile()

    def handle_text(self, text: str) -> list[Reply]:
        text = (text or "").strip()
        if not self.active:
            return [Reply("No hay ningún gasto en marcha. Mándame una foto del ticket.")]

        if says_no(text):
            return self.cancel()

        if self.awaiting == AWAIT_SUPPLIER:
            if not text:
                return [Reply("Dime el nombre del proveedor.")]
            self.receipt.supplier_name = text
            self.awaiting = None
            return self._ask_next()

        if self.awaiting == AWAIT_TOTAL:
            amount = _number(text)
            if amount is None or amount <= 0:
                return [Reply("No he entendido el importe. Dímelo en números, "
                              "por ejemplo «242,61».")]
            self._set_total(amount)
            self.awaiting = None
            return self._ask_next()

        if self.awaiting == AWAIT_CONFIRM:
            if says_yes(text):
                return self.confirm()
            # A bare number at the confirmation step is a correction of the total.
            amount = _number(text)
            if amount is not None and amount > 0 and re.fullmatch(r"[\d.,€\s]+", text):
                self._set_total(amount)
                return [Reply(f"Corregido a {amount:.2f}.")] + self._present()
            return [Reply("¿Lo anoto? Responde *sí* para guardarlo o *no* para descartarlo.")]

        return [Reply("No sé qué hacer con eso ahora mismo.")]

    def toggle_paid(self) -> list[Reply]:
        self.receipt.paid = not self.receipt.paid
        if self.receipt.paid:
            self.receipt.due_date = None
        return self._present()

    def confirm(self) -> list[Reply]:
        """Record the bill. Returns the message to show; the session is cleared."""
        config = get_config()
        try:
            bill_id = record(self.receipt)
        except Exception as exc:
            logger.exception("Could not record the expense")
            return [Reply(f"No he podido anotar el gasto: `{exc}`")]

        bill = bills.get(bill_id)
        deductible = self.receipt.tax_amount or 0.0
        lines = [
            f"✅ Anotado: *{bill['supplier_name']}* — "
            f"{format_money(bill['total'], config)}",
        ]
        if deductible:
            lines.append(f"IVA deducible: {format_money(deductible, config)}")
        if bill["paid"]:
            lines.append("Marcado como *ya pagado*.")
        else:
            lines.append(f"Pendiente de pago, vence el *{bill['due_date']}*.")
            lines.append(
                f"Total pendiente de pagar: {format_money(bills.total_owed(), config)}"
            )
        if bill["file_path"]:
            lines.append(f"\n📎 Guardado en `{bill['file_path']}`")

        self.reset()
        return [Reply("\n".join(lines))]

    def cancel(self) -> list[Reply]:
        """Discard the expense, and the archived photo with it.

        The image is filed before the user decides, so discarding has to clean up or the
        receipts folder slowly fills with pictures belonging to no bill.
        """
        path = self.receipt.image_path if self.receipt else None
        if path and _is_archived(path):
            try:
                os.unlink(path)
            except OSError:
                logger.warning("Could not delete the discarded receipt image %s", path)
        self.reset()
        return [Reply("Gasto descartado. No he anotado nada.")]


_sessions: dict[int, ExpenseSession] = {}


def session_for(chat_id: int) -> ExpenseSession:
    if chat_id not in _sessions:
        _sessions[chat_id] = ExpenseSession()
    return _sessions[chat_id]


def clear(chat_id: int) -> None:
    _sessions.pop(chat_id, None)
