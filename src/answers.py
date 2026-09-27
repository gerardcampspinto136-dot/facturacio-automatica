"""Answers to plain questions about the business, computed from the books.

"¿Cuánto he facturado este mes?", "¿Quién me debe dinero?", "¿Cuánto he gastado en
gasolina este año?" -- the assistant (src/assistant.py) works out *what* is being asked;
this module works out the *answer*, from the database, and phrases it. The model never
produces a figure: a number that reaches the chat was added up here. That is the point
of splitting the two -- a language model guessing an amount of money would be worse
than no answer at all.

Every topic states the permission it needs, the same keys as everywhere else: an
employee who may not see the receivables does not get them by asking nicely.
"""

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Optional

from src import db, store
from src.config_loader import get_config
from src.models import round_money

MONTHS = ("enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto",
          "septiembre", "octubre", "noviembre", "diciembre")

# topic -> the permission needed to be told
PERMISSION = {
    "revenue": "invoices.view", "invoices": "invoices.view", "quotes": "invoices.view",
    "expenses": "bills.view", "payables": "bills.view", "supplier": "bills.view",
    "profit": "taxes.view", "vat": "taxes.view", "irpf": "taxes.view",
    "receivables": "receivables.view", "client": "invoices.view",
    "stock": "stock.view", "help": None,
}


@dataclass
class Period:
    start: date
    end: date
    label: str               # "este mes", "el 3T 2026", "en 2025"
    previous: Optional["Period"] = field(default=None, repr=False)


def _d(value) -> Decimal:
    return Decimal(str(value or 0))


def _money(value: float) -> str:
    from src.totals import format_money

    return format_money(value, get_config())


def period(name: Optional[str], start: Optional[str] = None, end: Optional[str] = None,
           today: Optional[date] = None) -> Period:
    """Turn "this_month", "last_quarter", explicit dates... into a date range."""
    from src import taxes

    today = today or date.today()
    name = (name or "this_year").strip().lower()

    def month_range(year, month):
        first = date(year, month, 1)
        nxt = date(year + (month == 12), month % 12 + 1, 1)
        return first, nxt - timedelta(days=1)

    if name == "custom" and start:
        try:
            s = date.fromisoformat(start)
            e = date.fromisoformat(end) if end else today
            return Period(s, e, f"del {s.strftime('%d/%m/%Y')} al {e.strftime('%d/%m/%Y')}")
        except ValueError:
            name = "this_year"
    if name == "today":
        return Period(today, today, "hoy", Period(today - timedelta(days=1),
                                                  today - timedelta(days=1), "ayer"))
    if name == "this_week":
        s = today - timedelta(days=today.weekday())
        return Period(s, today, "esta semana",
                      Period(s - timedelta(days=7), s - timedelta(days=1), "la semana pasada"))
    if name in ("this_month", "last_month"):
        year, month = today.year, today.month
        if name == "last_month":
            year, month = (year - 1, 12) if month == 1 else (year, month - 1)
        s, e = month_range(year, month)
        py, pm = (year - 1, 12) if month == 1 else (year, month - 1)
        ps, pe = month_range(py, pm)
        label = "este mes" if name == "this_month" else f"en {MONTHS[month - 1]}"
        return Period(s, min(e, today), label, Period(ps, pe, f"en {MONTHS[pm - 1]}"))
    if name in ("this_quarter", "last_quarter"):
        y, q = taxes.quarter_of(today)
        if name == "last_quarter":
            y, q = taxes.last_closed_quarter(today)
        s, e = taxes.quarter_bounds(y, q)
        py, pq = (y - 1, 4) if q == 1 else (y, q - 1)
        ps, pe = taxes.quarter_bounds(py, pq)
        label = "este trimestre" if name == "this_quarter" else f"en el {q}T {y}"
        return Period(s, min(e, today), label, Period(ps, pe, f"en el {pq}T {py}"))
    if name == "last_year":
        y = today.year - 1
        return Period(date(y, 1, 1), date(y, 12, 31), f"en {y}",
                      Period(date(y - 1, 1, 1), date(y - 1, 12, 31), f"en {y - 1}"))
    if name == "all":
        return Period(date(2000, 1, 1), today, "en total")
    y = today.year  # this_year, and anything unrecognised
    return Period(date(y, 1, 1), today, "este año",
                  Period(date(y - 1, 1, 1), date(y - 1, 12, 31), f"en {y - 1}"))


# ── The figures ──────────────────────────────────────────────────────────────

def _invoices(p: Period, contact_name: Optional[str] = None) -> list[dict]:
    from src import taxes

    records = taxes.issued_between(p.start, p.end)
    if contact_name:
        wanted = contact_name.strip().lower()
        records = [r for r in records if wanted in r["invoice"].client_name.lower()]
    return records


def _turnover(records) -> tuple[float, float, int]:
    """(base, total with VAT, invoices that still stand) for a set.

    A cancelled invoice and its rectifying one net to zero in the amounts, and neither
    is counted: "0 € in 1 invoice" would be arithmetic, not an answer.
    """
    from src.totals import breakdown

    base = total = Decimal(0)
    count = 0
    for record in records:
        t = breakdown(record["invoice"])
        base += _d(t.base)
        total += _d(t.gross)
        if not record["invoice"].rectifies and not record.get("rectified_by"):
            count += 1
    return round_money(base), round_money(total), count


# What people ask about -> the category the expense is filed under. A fuel ticket is
# filed as "transporte", so "¿cuánto he gastado en gasolina?" must look there, not
# for a category called "gasolina" that never exists.
_CATEGORY_OF = {
    "gasolina": "transporte", "gasoil": "transporte", "combustible": "transporte",
    "diesel": "transporte", "taxi": "transporte", "taxis": "transporte",
    "tren": "transporte", "parking": "transporte", "peaje": "transporte",
    "peajes": "transporte", "benzina": "transporte",
    "comida": "dietas", "comidas": "dietas", "restaurante": "dietas",
    "restaurantes": "dietas", "menú": "dietas", "menús": "dietas", "àpats": "dietas",
    "luz": "suministros", "electricidad": "suministros", "llum": "suministros",
    "agua": "suministros", "gas": "suministros", "teléfono": "suministros",
    "telefono": "suministros", "móvil": "suministros", "internet": "suministros",
    "programas": "software", "suscripciones": "software", "aplicaciones": "software",
    "seguro": "seguros", "herramientas": "material", "materiales": "material",
}


def category_for(word: Optional[str]) -> Optional[str]:
    if not word:
        return None
    word = word.strip().lower()
    return _CATEGORY_OF.get(word, word)


def _bills(p: Period, category: Optional[str] = None,
           supplier: Optional[str] = None) -> list[dict]:
    from src import taxes

    rows = taxes.bills_between(p.start, p.end)
    if category:
        word = category.strip().lower()
        filed_as = category_for(word)
        rows = [b for b in rows if (b["category"] or "").lower() == filed_as
                or word in (b["notes"] or "").lower()
                or word in (b["supplier_name"] or "").lower()]
    if supplier:
        rows = [b for b in rows if supplier.lower() in (b["supplier_name"] or "").lower()]
    return rows


def _before(amount: float, label: str) -> str:
    """The previous period's figure, stated rather than turned into a percentage:
    "this month" is only the days so far, so "60% less than August" on the 10th would
    be true and misleading."""
    return f" {label[0].upper()}{label[1:]} fueron {_money(amount)}." if amount else ""


# ── One function per topic ───────────────────────────────────────────────────

def revenue(q: dict) -> str:
    p = period(q.get("period"), q.get("from"), q.get("to"))
    base, total, count = _turnover(_invoices(p, q.get("name")))
    who = f" a {q['name']}" if q.get("name") else ""
    if not count and not base:
        return f"No has facturado nada{who} {p.label}."
    text = (f"Has facturado{who} {_money(base)} sin IVA {p.label} "
            f"({_money(total)} con IVA), en {count} factura(s).")
    if p.previous and not q.get("name"):
        before, _, _ = _turnover(_invoices(p.previous))
        text += _before(before, p.previous.label)
    return text


def expenses(q: dict) -> str:
    p = period(q.get("period"), q.get("from"), q.get("to"))
    rows = _bills(p, q.get("category"), q.get("name"))
    what = f" en {q['category']}" if q.get("category") else (
        f" con {q['name']}" if q.get("name") else "")
    filed_as = category_for(q.get("category"))
    if q.get("category") and filed_as != q["category"].strip().lower():
        # Say what was actually counted: the whole category, not only that word.
        what += f" (gastos de {filed_as})"
    if not rows:
        return f"No tienes gastos anotados{what} {p.label}."
    total = round_money(sum(_d(b["total"]) for b in rows))
    vat = round_money(sum(_d(b["tax_amount"]) for b in rows))
    text = (f"Has gastado{what} {_money(total)} {p.label}, en {len(rows)} gasto(s)"
            + (f"; {_money(vat)} de IVA deducible." if vat else "."))
    if not q.get("category") and not q.get("name"):
        by_cat: dict = {}
        for b in rows:
            by_cat[b["category"] or "otros"] = by_cat.get(b["category"] or "otros", 0) + b["total"]
        top = sorted(by_cat.items(), key=lambda kv: -kv[1])[:3]
        if len(by_cat) > 1:
            text += " Sobre todo en " + ", ".join(f"{c} ({_money(v)})" for c, v in top) + "."
    return text


def profit(q: dict) -> str:
    p = period(q.get("period"), q.get("from"), q.get("to"))
    income, _, _ = _turnover(_invoices(p))
    spent = round_money(sum(_d(b["subtotal"]) for b in _bills(p)))
    result = round_money(_d(income) - _d(spent))
    word = "Ganas" if result >= 0 else "Pierdes"
    return (f"{word} {_money(abs(result))} {p.label}: {_money(income)} facturados menos "
            f"{_money(spent)} de gastos, todo sin IVA. (Antes de impuestos, y contando "
            "solo lo anotado aquí.)")


def receivables(q: dict) -> str:
    from src.totals import compute_totals

    unpaid = store.list_unpaid()
    if q.get("name"):
        wanted = q["name"].lower()
        unpaid = [u for u in unpaid if wanted in u["invoice"].client_name.lower()]
    if not unpaid:
        return (f"{q['name']} no te debe nada. 👌" if q.get("name")
                else "Nadie te debe nada. 🎉")
    total = round_money(sum(_d(compute_totals(u["invoice"])[2]) for u in unpaid))
    late = [u for u in unpaid if u["days_overdue"] > 0]
    lines = [f"Te deben {_money(total)} en {len(unpaid)} factura(s)"
             + (f"; {len(late)} ya vencida(s):" if late else ", ninguna vencida todavía.")]
    for u in sorted(late, key=lambda r: -r["days_overdue"])[:6]:
        inv = u["invoice"]
        lines.append(f"• {inv.client_name}: {_money(compute_totals(inv)[2])} "
                     f"({inv.invoice_number}, {u['days_overdue']} días de retraso)")
    if late:
        lines.append("\nCon /recordar <número> le mando un recordatorio.")
    return "\n".join(lines)


def payables(q: dict) -> str:
    from src import bills

    rows = bills.list_all(unpaid_only=True)
    if q.get("name"):
        rows = [b for b in rows if q["name"].lower() in b["supplier_name"].lower()]
    if not rows:
        return "No debes nada a proveedores. 🎉"
    total = round_money(sum(_d(b["total"]) for b in rows))
    today = date.today().isoformat()
    late = [b for b in rows if b["due_date"] and b["due_date"] < today]
    lines = [f"Debes {_money(total)} a proveedores en {len(rows)} factura(s)"
             + (f"; {len(late)} vencida(s)." if late else ".")]
    for b in rows[:6]:
        when = date.fromisoformat(b["due_date"]).strftime("%d/%m") if b["due_date"] else "—"
        lines.append(f"• {b['supplier_name']}: {_money(b['total'])} (vence el {when})")
    return "\n".join(lines)


def vat(q: dict) -> str:
    from src import taxes

    year, quarter = _quarter_of(q)
    return taxes.summary_text(year, quarter)


def _quarter_of(q: dict) -> tuple[int, int]:
    from src import taxes

    p = period(q.get("period") or "this_quarter", q.get("from"), q.get("to"))
    return taxes.quarter_of(p.start)


def irpf(q: dict) -> str:
    return vat(q)


def client(q: dict) -> str:
    from src import contacts
    from src.totals import compute_totals

    name = (q.get("name") or "").strip()
    if not name:
        return "¿De qué cliente? Dime el nombre."
    found = contacts.find_candidates(name, contacts.CLIENT)
    if not found:
        return f"No tengo ningún cliente que se llame «{name}»."
    if len(found) > 1:
        return ("Tengo varios: " + ", ".join(c["name"] for c in found[:5])
                + ". ¿Cuál?")
    c = found[0]
    year = period("this_year")
    base, _, count = _turnover(_invoices(year, c["name"]))
    owed = [u for u in store.list_unpaid() if u["invoice"].client_name == c["name"]]
    owed_total = round_money(sum(_d(compute_totals(u["invoice"])[2]) for u in owed))
    details = " · ".join(v for v in (c.get("tax_id"), c.get("email"), c.get("phone")) if v)
    lines = [f"{c['name']}" + (f" ({details})" if details else ""),
             f"Facturado este año: {_money(base)} sin IVA en {count} factura(s).",
             (f"Te debe {_money(owed_total)} en {len(owed)} factura(s)." if owed
              else "No te debe nada.")]
    return "\n".join(lines)


def supplier(q: dict) -> str:
    q = dict(q, category=None)
    return expenses(dict(q, period=q.get("period") or "this_year")) + "\n" + payables(q)


def stock(q: dict) -> str:
    from src import catalog

    if q.get("name"):
        product = catalog.find_in_text(q["name"])
        if product is None:
            return f"No encuentro «{q['name']}» en el catálogo."
        if not product["track_stock"]:
            return f"{product['name']} es un servicio: no lleva stock."
        return f"Te quedan {product['stock_qty']:g} {product['unit']} de {product['name']}."
    low = catalog.low_stock()
    if not low:
        return "Todo el stock está por encima de su punto de pedido. 👌"
    return "Por reponer:\n" + "\n".join(
        f"• {p['name']}: quedan {p['stock_qty']:g} {p['unit']}" for p in low[:10])


def invoices(q: dict) -> str:
    from src.totals import compute_totals

    p = period(q.get("period") or "this_month", q.get("from"), q.get("to"))
    records = _invoices(p, q.get("name"))
    if not records:
        return f"No hay facturas {p.label}."
    lines = [f"Facturas {p.label}:"]
    for r in records[-10:]:
        inv = r["invoice"]
        state = "cobrada" if r["paid_at"] else "anulada" if r["rectified_by"] else "pendiente"
        lines.append(f"• {inv.invoice_number} {inv.client_name}: "
                     f"{_money(compute_totals(inv)[2])} ({state})")
    return "\n".join(lines)


def quotes(q: dict) -> str:
    from src import quotes as quote_store
    from src.totals import compute_totals

    open_ = quote_store.list_quotes(open_only=True)
    if not open_:
        return "No tienes presupuestos esperando respuesta."
    total = round_money(sum(_d(compute_totals(x["invoice"])[2]) for x in open_))
    return (f"Tienes {len(open_)} presupuesto(s) sin respuesta por {_money(total)}. "
            "Míralos con /presupuestos.")


HELP = ("Puedes preguntarme cosas como:\n"
        "• ¿Cuánto he facturado este mes?\n"
        "• ¿Quién me debe dinero?\n"
        "• ¿Cuánto me debe Talleres Puig?\n"
        "• ¿Cuánto he gastado en gasolina este año?\n"
        "• ¿Cuánto gano este trimestre?\n"
        "• ¿Qué IVA me toca pagar?\n"
        "• ¿Cuántos tornillos M8 me quedan?\n"
        "Y para anotar un gasto sin foto: «he pagado 45 euros de gasolina en Repsol».")

TOPICS = {
    "revenue": revenue, "expenses": expenses, "profit": profit,
    "receivables": receivables, "payables": payables, "vat": vat, "irpf": irpf,
    "client": client, "supplier": supplier, "stock": stock, "invoices": invoices,
    "quotes": quotes, "help": lambda q: HELP,
}


def answer(question: dict, user: Optional[dict] = None) -> str:
    """Answer a structured question, as far as `user` may be told."""
    from src import accounts

    topic = (question.get("topic") or "help").strip().lower()
    handler = TOPICS.get(topic, TOPICS["help"])
    needed = PERMISSION.get(topic)
    if needed and user is not None and not accounts.can(user, needed):
        label = accounts.PERMISSIONS.get(needed, ("", needed))[1]
        return f"Eso no te lo puedo decir: te falta el permiso «{label}»."
    return handler(question)
