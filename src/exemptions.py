"""Why an invoice carries no VAT -- which the invoice itself has to say.

Spanish invoicing rules (RD 1619/2012, art. 6.1 j and m) require an invoice without
VAT to state the reason: the exemption, or "inversión del sujeto pasivo" when it is the
client who accounts for the VAT. Getting it wrong is not cosmetic -- "exenta" on a
service to a French company tells Hacienda something false -- so the reason is chosen,
never guessed, and then remembered: for the client (a French company is always one) or
for the whole business (an academy's classes are always exempt).

Each reason also carries what the other systems call it: the AEAT's code in Verifactu,
the EN 16931 VAT category, and Facturae's special taxable event.
"""

import re
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class Reason:
    key: str
    label: str          # on the button
    text: str           # printed on the invoice
    verifactu: str      # E1-E6 exempt, N2 not subject (place of supply), S2 reverse charge
    en16931: str        # VAT category: E exempt, K intra-EU, G export, AE reverse charge, O
    per_client: bool    # a fact about the client, remembered on their record


LAW = "de la Ley 37/1992 del IVA"

REASONS = {r.key: r for r in (
    Reason("exempt", "Actividad exenta (formación, sanidad…)",
           f"Operación exenta de IVA (artículo 20 {LAW}).", "E1", "E", False),
    Reason("eu_services", "Servicio a empresa de la UE",
           "Inversión del sujeto pasivo: servicio no sujeto al IVA español (artículo "
           f"69.Uno.1º {LAW}; artículo 196 de la Directiva 2006/112/CE).", "N2", "AE", True),
    Reason("eu_goods", "Mercancía a empresa de la UE",
           f"Entrega intracomunitaria exenta de IVA (artículo 25 {LAW}).", "E5", "K", True),
    Reason("non_eu_services", "Servicio a cliente de fuera de la UE",
           "Operación no sujeta al IVA español por reglas de localización (artículo 69 "
           f"{LAW}).", "N2", "O", True),
    Reason("export", "Exportación de mercancía (fuera de la UE)",
           f"Exportación exenta de IVA (artículo 21 {LAW}).", "E2", "G", True),
    Reason("reverse_charge", "Inversión del sujeto pasivo (obras…)",
           f"Inversión del sujeto pasivo (artículo 84.Uno.2º {LAW}).", "S2", "AE", True),
)}

# How a typed or spoken answer names each reason, checked in this order: "fuera de la
# UE" must be read before "UE", and goods before services.
_GOODS = r"(mercanc|producto|bienes|material|venta|vend)"
_EU = (r"(\bue\b|europ|comunitari|franc|alem|italia|portug|b[eé]lg|holand|pa[ií]ses "
       r"bajos|irland|austria|polac|polonia|suec|dinamar|finland|grecia|grieg|checa|"
       r"rumano|rumania|hungr|luxemb|eslov|croac|bulgar|eston|leton|litu|malta|chipr)")
_SAID = (
    ("reverse_charge", r"inversi[oó]n|sujeto pasivo|\bisp\b|obra|construcci"),
    ("export", rf"export|{_GOODS}.*fuera|fuera.*{_GOODS}"),
    ("non_eu_services", r"fuera|extranjer|internacional|eeuu|\busa\b|estados unidos|"
                        r"reino unido|ingl[eé]s|suiza|andorra"),
    ("eu_goods", rf"{_EU}.*{_GOODS}|{_GOODS}.*{_EU}"),
    ("eu_services", _EU),
    ("exempt", r"exent|formaci|enseñanza|educaci|sanitari|m[eé]dic|alquiler|seguro"),
)


def get(key: Optional[str]) -> Optional[Reason]:
    return REASONS.get(key or "")


def from_answer(text: str) -> Optional[str]:
    """The reason a typed answer names: a number from the list, or its words."""
    answer = (text or "").strip().lower()
    if answer.rstrip(".").isdigit():
        index = int(answer.rstrip(".")) - 1
        keys = list(REASONS)
        return keys[index] if 0 <= index < len(keys) else None
    for key, pattern in _SAID:
        if re.search(pattern, answer):
            return key
    return None


def question(tax_id: Optional[str] = None) -> tuple[str, list[tuple[str, str]]]:
    """What to ask and the buttons, the likeliest reasons first for this client."""
    order = list(REASONS)
    prefix = (tax_id or "").strip().upper()[:2]
    if prefix.isalpha() and prefix != "ES" and not _spanish(tax_id):
        # An id with a country prefix: the EU reasons are almost certainly the answer.
        order.sort(key=lambda k: 0 if k.startswith("eu_") else 1)
    lines = ["Esta factura va *sin IVA*, y la ley obliga a poner en ella el motivo. "
             "¿Cuál es?", ""]
    lines += [f"{i}. {REASONS[k].label}" for i, k in enumerate(REASONS, 1)]
    return "\n".join(lines), [(REASONS[k].label, f"vatwhy:{k}") for k in order]


def _spanish(tax_id: Optional[str]) -> bool:
    return bool(re.fullmatch(r"[A-Z]?\d{7,8}[A-Z0-9]?", (tax_id or "").strip().upper()))


def foreign_eu(tax_id: Optional[str]) -> bool:
    """Is this a VAT number from another EU country (FR..., DE..., IT...)?"""
    from src.einvoice import EU_PREFIXES

    cleaned = re.sub(r"[\s.\-]", "", tax_id or "").upper()
    return cleaned[:2] in EU_PREFIXES and len(cleaned) > 4


def settle(invoice, config=None) -> None:
    """Before issuing: an invoice without VAT says why; one with VAT carries no reason.

    The reason comes from the invoice, else the client's record, else the company's
    setting; with none of them, nothing is invented (the conversation asks before it
    gets here). The legal text goes into the notes, which are frozen with the invoice.
    """
    from src.totals import vat_rate

    # Whatever reason an earlier version of this draft said goes; the right one is
    # put back below (a reason changed, or VAT added, must not leave a stale one).
    notes = invoice.notes or ""
    for known in REASONS.values():
        notes = notes.replace(known.text, "")
    notes = "\n".join(line.strip() for line in notes.splitlines() if line.strip())

    if vat_rate(invoice, config):
        invoice.vat_reason = None
    else:
        if not get(invoice.vat_reason):
            invoice.vat_reason = default_for(invoice, config)
        reason = get(invoice.vat_reason)
        if reason:
            notes = f"{notes}\n{reason.text}" if notes else reason.text
    invoice.notes = notes or None


def default_for(invoice, config=None) -> Optional[str]:
    """The reason already settled for this client, or the business's own."""
    from src import contacts
    from src.config_loader import get_config

    if invoice.contact_id:
        contact = contacts.get(invoice.contact_id) or {}
        if get(contact.get("vat_reason")):
            return contact["vat_reason"]
    config = config or get_config()
    key = getattr(config, "vat_reason", None)
    return key if get(key) else None


def options_html(selected: Optional[str], blank: str = "—") -> str:
    """<option>s for a web form's select."""
    import html

    out = [f"<option value=''>{html.escape(blank)}</option>"]
    for key, reason in REASONS.items():
        mark = " selected" if key == selected else ""
        out.append(f"<option value='{key}'{mark}>{html.escape(reason.label)}</option>")
    return "".join(out)
