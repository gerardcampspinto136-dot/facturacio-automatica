"""The dialogue that turns a half-finished dictation into an invoice worth sending.

The bot used to take one utterance, build whatever it could, and hand over a draft --
so "factura a Talleres Puig, 300 euros" produced an invoice with no tax id and no email,
and nothing said so. This module makes it a conversation: it fills in what it knows from
the stored client, asks for what is still missing one question at a time, and refuses to
issue anything until the invoice is actually complete.

It deliberately knows nothing about Telegram. Every method returns Reply objects, so the
whole dialogue can be driven from a test without a network.

State lives in memory, keyed by chat. A draft in progress is ephemeral by nature; once
approved it goes to the database like any other invoice.
"""

import logging
from dataclasses import dataclass, field
from typing import Optional

from src import checklist, contacts
from src.config_loader import get_config
from src.models import InvoiceData
from src.totals import compute_totals, format_money, normalize_prices, summary_lines

logger = logging.getLogger(__name__)

# What the session is waiting for, beyond the ordinary checklist fields.
AWAIT_CONTACT = "__contact__"
AWAIT_CONFIRM = "__confirm__"
AWAIT_SAVE_CONTACT = "__save_contact__"


@dataclass
class Reply:
    """One outgoing message, optionally with buttons."""
    text: str
    buttons: list[tuple[str, str]] = field(default_factory=list)
    markdown: bool = True


# Agreement and refusal. Spanish and Catalan glue pronouns onto imperatives
# ("envíala", "descártala", "cancel·la-la"), so matching whole words alone would need an
# endless list. Short unambiguous words are matched exactly; verbs are matched by stem.
_YES_EXACT = {
    "si", "sí", "sip", "vale", "ok", "okay", "dale", "yes", "send", "perfecto",
    "correcto", "correcta", "correcte", "adelante", "endavant", "hazlo", "hazla",
}
_YES_STEMS = ("envi", "enví", "manda", "mánda", "aprob", "aprueb", "confirm", "d'acord")

_NO_EXACT = {"no", "nope", "para", "stop", "alto"}
_NO_STEMS = ("cancel", "anul", "anúl", "descart", "descárt", "olvid", "olvíd", "atur",
             "déjalo", "dejalo", "déjala", "dejala")


def _tokens(text: str) -> set[str]:
    import re

    return set(re.findall(r"[\wáéíóúüñçàèòï'·]+", (text or "").lower()))


def _matches(words: set[str], exact: set[str], stems: tuple) -> bool:
    if words & exact:
        return True
    return any(w.startswith(stem) for w in words for stem in stems)


def says_yes(text: str) -> bool:
    """True only for an unambiguous yes: a refusal anywhere in the sentence wins.

    "no lo envíes" contains an approval stem, and reading it as consent would send an
    invoice the user just declined.
    """
    words = _tokens(text)
    if _matches(words, _NO_EXACT, _NO_STEMS):
        return False
    return _matches(words, _YES_EXACT, _YES_STEMS)


def says_no(text: str) -> bool:
    return _matches(_tokens(text), _NO_EXACT, _NO_STEMS)


@dataclass
class Session:
    """One chat's in-progress invoice."""

    invoice: Optional[InvoiceData] = None
    awaiting: Optional[str] = None
    candidates: list[dict] = field(default_factory=list)
    # Set once the draft has been written to the pending queue.
    token: Optional[str] = None

    # ── Lifecycle ────────────────────────────────────────────────────────────

    def reset(self) -> None:
        self.invoice = None
        self.awaiting = None
        self.candidates = []
        self.token = None

    @property
    def active(self) -> bool:
        return self.invoice is not None

    # ── Entry point ──────────────────────────────────────────────────────────

    def start(self, invoice: InvoiceData) -> list[Reply]:
        """Begin a new invoice from a freshly parsed dictation."""
        self.reset()
        self.invoice = invoice
        return self._resolve_client()

    def handle_text(self, text: str) -> list[Reply]:
        """Route a free-text message according to what the session is waiting for."""
        text = (text or "").strip()
        if not self.active:
            return [Reply("No hay ninguna factura en marcha. "
                          "Envíame un audio o escribe los datos para empezar una.")]

        if says_no(text) and self.awaiting in (AWAIT_CONFIRM, AWAIT_SAVE_CONTACT):
            return self.cancel()

        if self.awaiting == AWAIT_CONTACT:
            return self._pick_contact_by_text(text)

        if self.awaiting == AWAIT_CONFIRM:
            if says_yes(text):
                return []  # the caller performs the approval
            return [Reply("¿Confirmas que la envío? Responde *sí* para enviarla "
                          "o *no* para descartarla.")]

        if self.awaiting in checklist.FIELDS:
            return self._answer_field(text)

        return [Reply("No sé qué hacer con eso ahora mismo.")]

    def cancel(self) -> list[Reply]:
        self.reset()
        return [Reply("Factura descartada. No se ha enviado nada.")]

    # ── Client resolution ────────────────────────────────────────────────────

    def _resolve_client(self) -> list[Reply]:
        """Match the spoken name against the stored clients before asking anything."""
        name = (self.invoice.client_name or "").strip()
        if not name:
            return self._ask_next()

        matches = contacts.find_candidates(name, contacts.CLIENT)

        if len(matches) > 1:
            self.awaiting = AWAIT_CONTACT
            self.candidates = matches
            lines = [f"Hay {len(matches)} clientes que coinciden con «{name}». ¿Cuál es?"]
            for i, c in enumerate(matches, 1):
                lines.append(f"  {i}. {contacts.describe(c)}")
            lines.append("\nResponde con el número.")
            return [Reply(
                "\n".join(lines),
                buttons=[(c["name"][:28], f"contact:{c['id']}") for c in matches],
            )]

        if len(matches) == 1:
            self._apply_contact(matches[0])

        return self._ask_next()

    def _pick_contact_by_text(self, text: str) -> list[Reply]:
        choice = None
        stripped = text.strip().rstrip(".")
        if stripped.isdigit():
            index = int(stripped)
            if 1 <= index <= len(self.candidates):
                choice = self.candidates[index - 1]
        if choice is None:
            for c in self.candidates:
                if c["name"].lower() == stripped.lower():
                    choice = c
                    break
        if choice is None:
            return [Reply("No he entendido cuál. Responde con el número de la lista.")]
        return self.pick_contact(choice["id"])

    def pick_contact(self, contact_id: int) -> list[Reply]:
        """Resolve the ambiguity, from a button press or a typed number."""
        chosen = next((c for c in self.candidates if c["id"] == contact_id), None)
        if chosen is None:
            chosen = contacts.get(contact_id)
        if chosen is None:
            return [Reply("Ese cliente ya no existe. Dime el nombre otra vez.")]

        self._apply_contact(chosen)
        self.candidates = []
        self.awaiting = None
        return [Reply(f"Vale, {chosen['name']}.")] + self._ask_next()

    def _apply_contact(self, contact: dict) -> None:
        """Fill in from the stored record, without overwriting anything just dictated."""
        inv = self.invoice
        inv.contact_id = contact["id"]
        inv.client_name = contact["name"]
        if not checklist.valid_email(inv.client_email) and contact.get("email"):
            inv.client_email = contact["email"]
        if not checklist.valid_tax_id(inv.client_id) and contact.get("tax_id"):
            inv.client_id = contact["tax_id"]
        if not (inv.client_address or "").strip() and contact.get("address"):
            inv.client_address = contact["address"]

    # ── The checklist ────────────────────────────────────────────────────────

    def _ask_next(self) -> list[Reply]:
        """Ask for the next missing field, or present the finished invoice."""
        config = get_config()
        question = checklist.next_question(self.invoice, config)

        if question is None:
            return self._present()

        field_name, prompt = question
        self.awaiting = field_name
        return [Reply(prompt)]

    def _answer_field(self, text: str) -> list[Reply]:
        field_name = self.awaiting

        if field_name == "items":
            # Re-parse, because an amount and a description arrive together.
            from src.parser import parse_invoice_from_transcript

            try:
                parsed = parse_invoice_from_transcript(text)
            except Exception:
                logger.exception("Could not parse the item answer")
                return [Reply("No he podido entender el importe. "
                              "Prueba así: «reparación, 180 euros».")]
            if not parsed.items:
                return [Reply("Sigo sin ver un importe. "
                              "Dime el concepto y cuánto, por ejemplo «montaje, 250 euros».")]
            self.invoice.items = parsed.items
            self.invoice.prices_normalized = False
            if parsed.prices_include_tax is not None:
                self.invoice.prices_include_tax = parsed.prices_include_tax
            self.awaiting = None
            return self._ask_next()

        accepted, complaint = checklist.apply_answer(self.invoice, field_name, text)
        if not accepted:
            return [Reply(complaint or "No he entendido eso.")]

        # A name given late still deserves a lookup against the stored clients.
        if field_name == "client_name":
            self.awaiting = None
            return self._resolve_client()

        self.awaiting = None
        return self._ask_next()

    # ── Presenting the finished draft ────────────────────────────────────────

    def _present(self) -> list[Reply]:
        config = get_config()
        normalize_prices(self.invoice, config)
        self.awaiting = AWAIT_CONFIRM

        inv = self.invoice
        lines = [
            "*Revisa la factura antes de enviarla*",
            "",
            f"Cliente: *{inv.client_name}*",
            f"NIF/CIF: {inv.client_id or '—'}",
            f"Email: {inv.client_email or '—'}",
        ]
        if inv.client_address:
            lines.append(f"Dirección: {inv.client_address}")
        lines.append("")
        for item in inv.items:
            lines.append(
                f"• {item.description} — {item.quantity:g} × "
                f"{format_money(item.unit_price, config)} = {format_money(item.total, config)}"
            )
        # Accepted, but worth flagging: an unusual id is often a mis-transcription.
        if inv.client_id == "SIN NIF":
            lines.append("⚠️ Sin identificador fiscal — revisa si la factura lo necesita.")
        elif inv.client_id and not checklist.looks_spanish_tax_id(inv.client_id):
            lines.append(f"⚠️ «{inv.client_id}» no tiene forma de NIF/CIF español. "
                         "Lo uso igual, pero compruébalo.")
        lines.append("")
        lines.append(summary_lines(inv, config))
        if inv.notes:
            lines.append(f"\nObservaciones: {inv.notes}")
        lines.append(f"\n¿La envío a {inv.client_email}?")

        return [Reply(
            "\n".join(lines),
            buttons=[
                ("✅ Enviar", "approve"),
                ("🔄 Cambiar IVA", "toggle_tax"),
                ("❌ Descartar", "cancel"),
            ],
        )]

    def toggle_tax(self) -> list[Reply]:
        """Flip between VAT-inclusive and VAT-on-top, recomputing from the original figures."""
        inv = self.invoice
        config = get_config()
        was_inclusive = bool(inv.prices_include_tax)

        if was_inclusive:
            # Undo the normalisation: put the gross figures back, then treat as exclusive.
            for item in inv.items:
                item.total = round(item.total * (1 + config.tax_rate / 100), 2)
                item.unit_price = (
                    round(item.total / item.quantity, 2) if item.quantity else item.total
                )
            inv.prices_include_tax = False
            inv.prices_normalized = True
        else:
            inv.prices_include_tax = True
            inv.prices_normalized = False
            normalize_prices(inv, config)

        state = "IVA incluido" if inv.prices_include_tax else "IVA aparte"
        return [Reply(f"Cambiado a *{state}*.")] + self._present()

    # ── Saving the client for next time ──────────────────────────────────────

    def contact_is_new(self) -> bool:
        return self.invoice is not None and self.invoice.contact_id is None

    def save_contact(self) -> Optional[int]:
        """Store this client so the next invoice does not have to ask again."""
        inv = self.invoice
        if inv is None or inv.contact_id is not None:
            return None
        try:
            contact_id = contacts.create(
                contacts.CLIENT,
                inv.client_name,
                email=inv.client_email or None,
                tax_id=inv.client_id or None,
                address=inv.client_address or None,
            )
            inv.contact_id = contact_id
            return contact_id
        except Exception:
            logger.exception("Could not save the client")
            return None


# ── Per-chat registry ────────────────────────────────────────────────────────

_sessions: dict[int, Session] = {}


def session_for(chat_id: int) -> Session:
    if chat_id not in _sessions:
        _sessions[chat_id] = Session()
    return _sessions[chat_id]


def clear(chat_id: int) -> None:
    _sessions.pop(chat_id, None)
