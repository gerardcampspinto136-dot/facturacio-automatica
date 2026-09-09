"""What an invoice must contain before it can be issued.

A Spanish invoice is a legal document: issuing one without the client's tax id is not a
cosmetic problem, and an invoice with no email cannot be delivered. Rather than letting
those through and discovering it later, the bot walks the missing fields one at a time
and asks for each.

Which fields are compulsory is configurable (`required_fields` in company.yaml), except
the two that make the document meaningless: a client name and at least one line.
"""

import re
from typing import Optional

from src.models import InvoiceData

# Deliberately permissive. The job is to catch "no me acuerdo" and obvious dictation
# debris, not to adjudicate the RFC.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")

# A Spanish NIF/CIF/NIE: 12345678A, B12345678, X1234567L. Used to tell the user their
# id looks unusual -- never to refuse it, because plenty of legitimate clients are not
# Spanish and carry a VAT number, a passport, or a foreign tax id instead.
_SPANISH_TAX_ID_RE = re.compile(r"^[A-Za-z]?\d{7,8}[A-Za-z]?$")

# What counts as an identifier at all: letters and digits, plausibly long enough to be
# one. This rejects "no me acuerdo" and "pepito" without adjudicating world tax law.
_ANY_TAX_ID_RE = re.compile(r"^(?=.*\d)[A-Za-z0-9]{4,20}$")

# Ways of saying the client has no tax id, for the cases where that is genuinely true
# (a private individual abroad, say). The invoice records why it is missing.
_NO_TAX_ID = {
    "no tiene", "no tienen", "sin nif", "sin cif", "sin dni", "no tengo", "ninguno",
    "no aplica", "n/a", "na", "omitir", "saltar", "sin identificador", "no en te",
}

FIELDS = {
    "client_name": {
        "label": "el nombre del cliente",
        "question": "¿A nombre de quién va la factura?",
    },
    "items": {
        "label": "el concepto y el importe",
        "question": "¿Qué le facturas y por cuánto? Por ejemplo: "
                    "«tres horas de montaje a 45 euros» o «reparación, 180 euros».",
    },
    "client_email": {
        "label": "el email del cliente",
        "question": "¿Cuál es el email del cliente? "
                    "Lo necesito para poder enviarle la factura.",
    },
    "client_id": {
        "label": "el NIF/CIF del cliente",
        "question": "¿Cuál es el NIF o CIF del cliente? "
                    "Es obligatorio en una factura española.",
    },
    "client_address": {
        "label": "la dirección del cliente",
        "question": "¿Cuál es la dirección fiscal del cliente?",
    },
}


def valid_email(value: Optional[str]) -> bool:
    return bool(value and _EMAIL_RE.match(value.strip()))


def _clean_tax_id(value: Optional[str]) -> str:
    return (value or "").strip().upper().replace("-", "").replace(" ", "").replace(".", "")


def valid_tax_id(value: Optional[str]) -> bool:
    """Is this usable as a tax id at all? Deliberately permissive about the format."""
    cleaned = _clean_tax_id(value)
    if not cleaned:
        return False
    if cleaned == "SIN NIF".replace(" ", ""):
        return True
    return bool(_ANY_TAX_ID_RE.match(cleaned))


def looks_spanish_tax_id(value: Optional[str]) -> bool:
    """Does it match the Spanish NIF/CIF/NIE shape? Used only to warn."""
    return bool(_SPANISH_TAX_ID_RE.match(_clean_tax_id(value)))


def missing_fields(invoice: InvoiceData, config=None) -> list[str]:
    """Field names still needed, in the order they should be asked for."""
    if config is None:
        from src.config_loader import get_config

        config = get_config()

    missing: list[str] = []

    if not (invoice.client_name or "").strip():
        missing.append("client_name")

    if not invoice.items or all(i.total == 0 for i in invoice.items):
        missing.append("items")

    if config.require_email and not valid_email(invoice.client_email):
        missing.append("client_email")

    if config.require_tax_id and not valid_tax_id(invoice.client_id):
        missing.append("client_id")

    if config.require_address and not (invoice.client_address or "").strip():
        missing.append("client_address")

    return missing


def is_complete(invoice: InvoiceData, config=None) -> bool:
    return not missing_fields(invoice, config)


def next_question(invoice: InvoiceData, config=None) -> Optional[tuple[str, str]]:
    """The next (field, question) to put to the user, or None when nothing is missing."""
    missing = missing_fields(invoice, config)
    if not missing:
        return None
    field = missing[0]
    return field, FIELDS[field]["question"]


def summary_of_missing(invoice: InvoiceData, config=None) -> str:
    """Human list of what is still needed, for a one-line status."""
    missing = missing_fields(invoice, config)
    if not missing:
        return ""
    labels = [FIELDS[f]["label"] for f in missing]
    if len(labels) == 1:
        return f"Falta {labels[0]}."
    return "Faltan " + ", ".join(labels[:-1]) + f" y {labels[-1]}."


def apply_answer(invoice: InvoiceData, field: str, answer: str) -> tuple[bool, Optional[str]]:
    """Put a user's reply into `field`. Returns (accepted, complaint_if_rejected).

    Validation happens here rather than at the end so a mistyped email is caught while
    the user is still looking at the question that produced it.
    """
    answer = (answer or "").strip()
    if not answer:
        return False, "No he entendido nada. ¿Puedes repetirlo?"

    if field == "client_name":
        invoice.client_name = answer
        return True, None

    if field == "client_email":
        candidate = _clean_email(answer)
        if not valid_email(candidate):
            return False, (
                f"«{answer}» no parece un email válido. "
                "Escríbelo entero, por ejemplo: nombre@empresa.com"
            )
        invoice.client_email = candidate
        return True, None

    if field == "client_id":
        if answer.strip().lower() in _NO_TAX_ID:
            invoice.client_id = "SIN NIF"
            return True, None
        candidate = _clean_tax_id(answer)
        if not valid_tax_id(candidate):
            return False, (
                f"«{answer}» no me sirve como identificador fiscal. "
                "Dime el NIF/CIF (por ejemplo 12345678A o B12345678), "
                "el número de IVA si es de fuera, o «no tiene» si realmente no tiene."
            )
        invoice.client_id = candidate
        return True, None

    if field == "client_address":
        invoice.client_address = answer
        return True, None

    return False, None


def _clean_email(text: str) -> str:
    """Pull an address out of a spoken or typed answer.

    People reply "es juan arroba ejemplo punto es" or "Mi correo: juan@ejemplo.es", and
    the transcription often keeps the dictated form.
    """
    text = text.strip()
    match = re.search(r"[^\s<>()\[\]]+@[^\s<>()\[\],;]+", text)
    if match:
        return match.group().strip(".,;:")

    spoken = text.lower()
    spoken = re.sub(r"\s*\b(arroba|arrova|at)\b\s*", "@", spoken)
    spoken = re.sub(r"\s*\b(punto|punt|dot)\b\s*", ".", spoken)
    spoken = re.sub(r"\s*\b(guion|guión|guio)\b\s*", "-", spoken)
    spoken = re.sub(r"\s*\b(guion bajo|barra baja)\b\s*", "_", spoken)
    spoken = spoken.replace(" ", "")
    match = re.search(r"[^\s<>()\[\]]+@[^\s<>()\[\],;]+", spoken)
    return match.group().strip(".,;:") if match else text
