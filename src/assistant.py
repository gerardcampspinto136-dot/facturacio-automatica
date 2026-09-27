"""What a message to the bot is: an invoice, a quote, an expense, or a question.

The bot used to treat every message as an invoice to dictate, so "¿cuánto he facturado
este mes?" produced an invoice for a client called "cuánto". Now a message is sorted
first:

  invoice / quote   the dictation flow, as before
  expense           "he pagado 45 € de gasolina en Repsol" -- filed like a photographed
                    ticket, after the same confirmation
  question          "¿quién me debe dinero?" -- answered from the books by
                    src/answers.py (the model chooses the topic; it never states a
                    figure)

Most messages are dictated invoices and say so ("factura para..."), so those skip the
model call entirely: the routing costs nothing on the common path.
"""

import json
import logging
import re
from datetime import date
from typing import Optional

logger = logging.getLogger(__name__)

_INVOICE_WORDS = ("factura para", "factura a ", "factura per", "factúrale", "facturale",
                  "presupuesto", "pressupost", "invoice for", "quote for")
_QUESTION_START = ("¿", "cuánt", "cuant", "quién", "quien", "qué ", "que ", "cuál", "cual",
                   "dime", "quant", "qui ", "quin", "com va", "how much", "who ", "what ")


def looks_like_question(text: str) -> bool:
    low = text.strip().lower()
    return low.endswith("?") or low.startswith(_QUESTION_START)


def quick_intent(text: str) -> Optional[str]:
    """Decide without the model when the message leaves no doubt. None = ask the model."""
    low = (text or "").strip().lower()
    if not low:
        return "other"
    if looks_like_question(low):
        return None
    if low.startswith(("factura", "presupuesto", "pressupost")) or any(
            w in low for w in _INVOICE_WORDS):
        return "invoice"
    return None


_ROUTER_PROMPT = """You sort the messages that the owner or staff of a small Spanish business send to their invoicing assistant. Today is {today}.

Return ONE JSON object and nothing else:
{{
  "intent": "invoice | quote | expense | question | other",
  "topic": "revenue | expenses | profit | receivables | payables | vat | irpf | client | supplier | stock | invoices | quotes | help | null",
  "period": "today | this_week | this_month | last_month | this_quarter | last_quarter | this_year | last_year | all | custom | null",
  "from": "YYYY-MM-DD or null",
  "to": "YYYY-MM-DD or null",
  "name": "the client, supplier or product asked about, or null",
  "category": "for expenses: what was bought (gasolina, comida, material...), or null",
  "expense": {{
    "supplier_name": "who was paid, or null",
    "total": "number, the amount paid, or null",
    "tax_rate": "number, only if a VAT rate is said, else null",
    "tax_amount": "number, only if the VAT amount is said, else null",
    "date": "YYYY-MM-DD, only if a date is said, else null",
    "category": "material | suministros | transporte | dietas | servicios | software | alquiler | seguros | impuestos | otros",
    "paid": "true unless they say it is still to be paid",
    "notes": "what it was for, or null"
  }}
}}

Rules:
- invoice: they want to bill a client ("factura para Talleres Puig...", "cóbrale 300 euros a Juan").
- quote: a quote or estimate ("presupuesto para...", "pressupost per a...").
- expense: something THEY paid or bought ("he pagado 45 euros de gasolina en Repsol", "gasto de 30 euros en la ferretería", "he comprat material per 120 euros"). Fill "expense".
- question: they ask about their own figures. Fill topic/period/name/category.
  revenue = facturado, ventas, ingresos; expenses = gastado, gastos, compras; profit = gano, beneficio, ganancias;
  receivables = me deben, pendiente de cobro, quién no ha pagado; payables = debo, tengo que pagar, proveedores pendientes;
  vat = IVA, modelo 303; irpf = IRPF, modelo 130, retenciones; client = everything about one client;
  supplier = everything about one supplier; stock = cuánto queda, existencias; invoices = list invoices;
  quotes = presupuestos pendientes; help = what can you do.
- period: say exactly what was asked. With no period: this_year for revenue, expenses and profit; this_quarter for vat and irpf.
  "este mes" = this_month, "el mes pasado" = last_month, "este trimestre" = this_quarter, "el trimestre pasado" = last_quarter,
  "este año" = this_year, "el año pasado" = last_year, "hoy" = today, "esta semana" = this_week.
  A named month ("en agosto") is custom with its first and last day, in the current year unless another is said.
- other: anything else (greetings, thanks, nonsense).
- Spanish, Catalan or English. Never invent amounts or names that were not said."""


def route(text: str, today: Optional[date] = None) -> dict:
    """Ask the model what the message is. Returns the parsed JSON (see the prompt)."""
    import os

    from src.parser import (ANTHROPIC_MODEL, GROQ_FALLBACK_MODEL, GROQ_MODEL,
                            _which_provider, with_retries)

    prompt = _ROUTER_PROMPT.format(today=(today or date.today()).isoformat())
    if _which_provider() == "anthropic":
        import anthropic

        client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        response = client.messages.create(
            model=ANTHROPIC_MODEL, max_tokens=700, temperature=0, system=prompt,
            messages=[{"role": "user", "content": text},
                      {"role": "assistant", "content": "{"}])
        raw = "{" + response.content[0].text
    else:
        from groq import Groq

        client = Groq(api_key=os.environ["GROQ_API_KEY"])

        def call(model: str) -> str:
            response = client.chat.completions.create(
                model=model, temperature=0, max_tokens=700,
                response_format={"type": "json_object"},
                messages=[{"role": "system", "content": prompt},
                          {"role": "user", "content": text}])
            return response.choices[0].message.content or ""

        raw = with_retries(call, (GROQ_MODEL, GROQ_FALLBACK_MODEL), "entender el mensaje")
    return _parse(raw)


def _parse(raw: str) -> dict:
    try:
        data = json.loads(raw)
    except ValueError:
        match = re.search(r"\{.*\}", raw or "", re.DOTALL)
        data = json.loads(match.group()) if match else {}
    intent = str(data.get("intent") or "other").strip().lower()
    data["intent"] = intent if intent in (
        "invoice", "quote", "expense", "question", "other") else "other"
    return data


def expense_receipt(data: dict):
    """A spoken expense as a ReceiptData, for the usual confirmation.

    Without a document there is no deductible VAT to speak of -- deducting it needs the
    invoice -- so none is assumed: the base is the whole amount unless the VAT was said,
    and the summary asks for the photo.
    """
    from src import receipts

    expense = dict(data.get("expense") or {})
    stated_vat = expense.get("tax_amount") not in (None, "") or \
        expense.get("tax_rate") not in (None, "")
    receipt = receipts.build_receipt({
        "supplier_name": expense.get("supplier_name") or "",
        "total": expense.get("total"),
        "tax_rate": expense.get("tax_rate"),
        "tax_amount": expense.get("tax_amount"),
        "date": expense.get("date"),
        "category": expense.get("category") or "otros",
        "paid": expense.get("paid", True) is not False,
        "notes": expense.get("notes"),
        "confidence": 1.0,
    })
    if not stated_vat and receipt.total is not None:
        receipt.subtotal, receipt.tax_amount = receipt.total, 0.0
        receipt.flags.append("Sin factura no hay IVA deducible: cuando la tengas, "
                             "mándame la foto y la anoto con su IVA.")
    return receipt
