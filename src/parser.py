"""Turn a voice transcription into structured invoice data.

Works with either provider, chosen the same way transcription.py chooses its speech
engine: whatever key is present, preferring the paid one when it is configured because
it is measurably better at this particular job -- pulling a name, an email spelled out
loud, a tax id and a set of amounts out of loose dictation in Spanish or Catalan.

Set LLM_PROVIDER=groq or LLM_PROVIDER=anthropic in .env to force one.

Both paths ask for a JSON object explicitly (JSON mode on Groq, a prefilled opening brace
on Anthropic) rather than hoping for clean output and regexing it out afterwards.
"""

import json
import os
import re
from datetime import date

from src.models import InvoiceData, InvoiceItem

# Groq's largest open model. 131k context, free tier, reliable with JSON mode.
GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6")

_SYSTEM_PROMPT = """You are an invoice data extraction assistant. Given a voice transcription, extract all invoice-relevant information and return it as a single valid JSON object — nothing else, no explanation.

JSON schema:
{
  "client_name": "string",
  "client_email": "string",
  "client_address": "string or null",
  "client_id": "NIF/CIF/DNI or null",
  "items": [
    {
      "description": "string",
      "hours": "number or null",
      "rate": "number or null (hourly rate)",
      "quantity": "number (default 1)",
      "unit_price": "number",
      "total": "number"
    }
  ],
  "notes": "string or null",
  "prices_include_tax": "true | false | null"
}

Rules:
- If hours + rate are mentioned: set quantity=hours, unit_price=rate, total=hours*rate.
- If only a total amount for an item is mentioned: quantity=1, unit_price=total, total=total.
- All monetary values must be plain numbers without currency symbols.
- The transcription may be in Spanish, Catalan, or English.
- Emails and tax ids are often dictated letter by letter or digit by digit
  ("jota u a ene arroba ejemplo punto com", "be uno dos tres"). Reassemble them into
  their normal written form: "arroba"/"arrova" is @, "punto"/"punt" is a dot.
- NEVER translate. Words inside an email address, a domain or a company name stay in the
  language they were spoken: "ejemplo punto com" is "ejemplo.com", NOT "example.com".
- Read compound numbers in full and in the language spoken. Spanish and Catalan build
  them additively: "ciento ochenta" / "cent vuitanta" is 180, not 80; "mil doscientos
  cincuenta" is 1250; "veinticuatro con cincuenta" / "vint-i-quatre amb cinquanta" is
  24.50. Check that every amount you output uses every part of what was said.
- Use null for anything genuinely not mentioned. Never invent an email or a tax id.
- prices_include_tax records how the amounts were quoted, and is NOT a note:
    true  if the speaker said the price already contains VAT — "IVA incluido",
          "con IVA", "IVA inclòs", "amb IVA", "VAT included", "todo incluido".
    false if the speaker said VAT goes on top — "más IVA", "mas IVA", "sin IVA",
          "más el IVA", "més IVA", "IVA aparte", "plus VAT".
    null  if they did not say either way.
  Never put the VAT instruction in "notes" and never change the amounts yourself:
  report the figures exactly as spoken and let prices_include_tax say what they mean.
- "notes" is only for a genuine remark about the job. If there is none, use null."""


def _which_provider() -> str:
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


def _complete_groq(transcript: str) -> str:
    from groq import Groq

    client = Groq(api_key=os.environ["GROQ_API_KEY"])
    response = client.chat.completions.create(
        model=GROQ_MODEL,
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": transcript},
        ],
        response_format={"type": "json_object"},
        temperature=0,
        max_tokens=1500,
    )
    return response.choices[0].message.content or ""


def _complete_anthropic(transcript: str) -> str:
    import anthropic

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    response = client.messages.create(
        model=ANTHROPIC_MODEL,
        max_tokens=1500,
        system=_SYSTEM_PROMPT,
        temperature=0,
        messages=[
            {"role": "user", "content": transcript},
            # Prefilling the opening brace stops any preamble before the JSON.
            {"role": "assistant", "content": "{"},
        ],
    )
    return "{" + response.content[0].text


def _extract_json(text: str, provider: str) -> dict:
    text = (text or "").strip()
    try:
        return json.loads(text)
    except ValueError:
        pass
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError(f"No JSON in the {provider} response: {text[:200]}")
    return json.loads(match.group())


def _number(value, default: float = 0.0) -> float:
    """Coerce whatever the model returned into a float, tolerating "1.250,50" and "45 €"."""
    if value is None or value == "":
        return default
    if isinstance(value, (int, float)):
        return float(value)
    cleaned = re.sub(r"[^\d,.\-]", "", str(value))
    if "," in cleaned and "." in cleaned:
        # Spanish thousands separator: 1.250,50
        cleaned = cleaned.replace(".", "").replace(",", ".")
    else:
        cleaned = cleaned.replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return default


def parse_invoice_from_transcript(transcript: str) -> InvoiceData:
    provider = _which_provider()
    raw = _complete_groq(transcript) if provider == "groq" else _complete_anthropic(transcript)
    data = _extract_json(raw, provider)

    items: list[InvoiceItem] = []
    for item_data in data.get("items") or []:
        hours = _number(item_data.get("hours"), 0)
        rate = _number(item_data.get("rate"), 0)
        quantity = _number(item_data.get("quantity"), 1) or 1
        unit_price = _number(item_data.get("unit_price"), 0)
        total = _number(item_data.get("total"), 0)

        if hours and rate:
            quantity, unit_price = hours, rate
            total = round(quantity * unit_price, 2)
        elif unit_price and not total:
            total = round(quantity * unit_price, 2)
        elif total and not unit_price:
            unit_price = round(total / quantity, 2) if quantity else total

        items.append(
            InvoiceItem(
                description=item_data.get("description") or "Servicio",
                quantity=quantity,
                unit_price=unit_price,
                total=total,
            )
        )

    raw_flag = data.get("prices_include_tax")
    if isinstance(raw_flag, str):
        lowered = raw_flag.strip().lower()
        raw_flag = True if lowered in ("true", "si", "sí", "yes") else (
            False if lowered in ("false", "no") else None
        )

    return InvoiceData(
        prices_include_tax=raw_flag if isinstance(raw_flag, bool) else None,
        client_name=data.get("client_name") or "",
        client_email=data.get("client_email") or "",
        client_address=data.get("client_address"),
        client_id=data.get("client_id"),
        items=items,
        notes=data.get("notes"),
        date=date.today(),
    )
