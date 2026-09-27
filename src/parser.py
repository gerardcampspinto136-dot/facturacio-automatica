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
# The free tier answers "over capacity" often enough that a dictation needs somewhere
# else to go: the smaller sibling is usually free when the big one is not.
GROQ_FALLBACK_MODEL = os.getenv("GROQ_FALLBACK_MODEL", "openai/gpt-oss-20b")
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6")


class ParseError(Exception):
    """A failure the user can act on, phrased for the chat rather than for a log."""

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
  "prices_include_tax": "true | false | null",
  "irpf_rate": "number or null",
  "tax_rate": "number or null"
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
          "IVA inclòs", "VAT included", "todo incluido", or a bare "con IVA" /
          "amb IVA" with no rate after it ("300 euros con IVA").
    false if the speaker said VAT goes on top — "más IVA", "mas IVA", "sin IVA",
          "más el IVA", "més IVA", "IVA aparte", "plus VAT".
    null  if they did not say either way.
  Never put the VAT instruction in "notes" and never change the amounts yourself:
  report the figures exactly as spoken and let prices_include_tax say what they mean.
- irpf_rate is the IRPF withholding ("retención") percentage, only if the speaker
  mentions one: "con retención del 15%", "retención del siete por ciento",
  "amb retenció del 15" -> 15 or 7; "sin retención", "sense retenció" -> 0;
  not mentioned -> null.
- tax_rate is the VAT percentage, only if the speaker states a rate: "IVA del 10%",
  "al cuatro por ciento de IVA", "IVA reducido del 10" -> 10 or 4; "exento de IVA",
  "exempt d'IVA" -> 0; not mentioned -> null. "más IVA", "sin IVA" and "IVA incluido"
  say how the price was quoted -- that is prices_include_tax -- NOT the rate.
- Naming a rate does not say the price includes it: "con IVA del 10%" and "amb IVA
  del deu per cent" set tax_rate and leave prices_include_tax null, unless the speaker
  ALSO says it is included ("IVA del 10% incluido" -> tax_rate 10, true).
- "notes" is only for a genuine remark about the job. If there is none, use null.
  Never put the VAT, the withholding or how the price was quoted in "notes"."""


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


def _sleep(seconds: float) -> None:
    """Indirection so the retry tests do not actually wait."""
    import time

    time.sleep(seconds)


def _retry_after_seconds(exc: Exception):
    """How long a rate limit says to wait, from its "try again in 8m27.1s" message."""
    match = re.search(r"try again in (?:(\d+)m)?([\d.]+)s", str(exc))
    if not match:
        return None
    return int(match.group(1) or 0) * 60 + float(match.group(2))


def with_retries(call, models, what: str = "leer el mensaje", attempts: int = 3):
    """Run call(model) against each model in turn, riding out the free tier's hiccups.

    Groq's free tier answers "503 over capacity" several times a day and rate-limits
    with a 429. Neither means the request was wrong, so it is retried with a short
    backoff, then tried on the next model. A 401/403 (bad key) is permanent and raised
    at once; a 404 (the model was withdrawn -- it happens) moves straight on. When
    everything fails the user gets a sentence, not a stack trace.
    """
    last: Exception | None = None
    for model in [m for m in models if m]:
        for attempt in range(attempts):
            try:
                return call(model)
            except Exception as exc:
                last = exc
                status = getattr(exc, "status_code", None)
                if status in (401, 403):
                    raise
                if status == 404:
                    break
                wait = _retry_after_seconds(exc)
                if wait and wait > 30:
                    break  # a long rate limit: not worth sitting through here
                if status is not None and 400 <= status < 500 and status not in (400, 429):
                    break
                if attempt + 1 < attempts:
                    _sleep(min(2 ** attempt * 2, 8))

    wait = _retry_after_seconds(last) if last else None
    if wait and wait > 30:
        raise ParseError(
            f"He agotado la cuota gratuita de la IA para {what}. Vuelve a intentarlo "
            f"dentro de unos {max(1, round(wait / 60))} minutos."
        ) from last
    raise ParseError(
        f"El servicio de IA está saturado y no he podido {what}. "
        "Vuelve a mandármelo en un minuto."
    ) from last


def _complete_groq(transcript: str) -> str:
    from groq import Groq

    client = Groq(api_key=os.environ["GROQ_API_KEY"])

    def call(model: str) -> str:
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": transcript},
            ],
            response_format={"type": "json_object"},
            temperature=0,
            max_tokens=1500,
        )
        return response.choices[0].message.content or ""

    return with_retries(call, (GROQ_MODEL, GROQ_FALLBACK_MODEL), "leer la factura")


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
        irpf_rate=_rate(data.get("irpf_rate"), maximum=50),
        tax_rate=_rate(data.get("tax_rate"), maximum=30),
    )


def _rate(value, maximum: float):
    """A percentage the model reported, or None when absent or implausible.

    A model that mishears "IVA al 10" as 100 must not produce an invoice at 100% VAT;
    an out-of-range figure is dropped, which falls back to the company default and is
    visible in the summary before anything is sent.
    """
    if value is None or value == "" or isinstance(value, bool):
        return None
    rate = _number(value, default=-1)
    return rate if 0 <= rate <= maximum else None
