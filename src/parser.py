import anthropic
import json
import os
import re
from datetime import date

from src.models import InvoiceData, InvoiceItem

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
  "notes": "string or null"
}

Rules:
- If hours + rate are mentioned: set quantity=hours, unit_price=rate, total=hours*rate.
- If only a total amount for an item is mentioned: quantity=1, unit_price=total, total=total.
- All monetary values must be plain numbers without currency symbols.
- The transcription may be in Spanish, Catalan, or English."""


def parse_invoice_from_transcript(transcript: str) -> InvoiceData:
    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": transcript}],
    )

    text = response.content[0].text.strip()

    # Extract JSON block if wrapped in markdown fences
    json_match = re.search(r"\{.*\}", text, re.DOTALL)
    if not json_match:
        raise ValueError(f"Could not find JSON in Claude response: {text[:200]}")

    data = json.loads(json_match.group())

    items: list[InvoiceItem] = []
    for item_data in data.get("items", []):
        hours = item_data.get("hours")
        rate = item_data.get("rate")
        quantity = float(item_data.get("quantity", 1))
        unit_price = float(item_data.get("unit_price", 0))
        total = float(item_data.get("total", 0))

        if hours and rate:
            quantity = float(hours)
            unit_price = float(rate)
            total = round(quantity * unit_price, 2)

        items.append(
            InvoiceItem(
                description=item_data.get("description", "Servicio"),
                quantity=quantity,
                unit_price=unit_price,
                total=total,
            )
        )

    return InvoiceData(
        client_name=data.get("client_name", ""),
        client_email=data.get("client_email", ""),
        client_address=data.get("client_address"),
        client_id=data.get("client_id"),
        items=items,
        notes=data.get("notes"),
        date=date.today(),
    )
