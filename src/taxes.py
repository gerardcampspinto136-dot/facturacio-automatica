"""The quarter's taxes, worked out from what the software already knows.

Every quarter an autónomo or a small company files the VAT return (modelo 303) and, if
they are a person in "estimación directa", the IRPF instalment (modelo 130). Doing it
means collecting every invoice issued and every expense received that quarter and
adding them up -- hours of work, usually handed to a gestor with a shoebox of papers.
Everything needed is already in the database, so this does the adding up.

The figures are an *estimate for the gestor*, not the filed return: the software knows
the invoices and the expenses it was given, not the ones it was not (the autónomo's
social security, say, or a bank's fees). The pack says so.

  303  IVA devengado (by rate)  -  IVA deducible  = resultado
  130  [01] ingresos  [02] gastos  [03] rendimiento  [04] 20 %
       [05] pagos de trimestres anteriores  [06] retenciones  [07] a ingresar
       (all running totals from 1 January, as the form asks)
"""

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Optional

from src import db
from src.config_loader import get_config
from src.models import percent_of, round_money

MONTHS = ("enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto",
          "septiembre", "octubre", "noviembre", "diciembre")


# ── Quarters and deadlines ───────────────────────────────────────────────────

def quarter_of(day: date) -> tuple[int, int]:
    return day.year, (day.month - 1) // 3 + 1


def quarter_bounds(year: int, quarter: int) -> tuple[date, date]:
    start = date(year, 3 * quarter - 2, 1)
    end = (date(year + 1, 1, 1) if quarter == 4 else date(year, 3 * quarter + 1, 1)) \
        - timedelta(days=1)
    return start, end


def label(year: int, quarter: int) -> str:
    return f"{quarter}T {year}"


def filing_window(year: int, quarter: int) -> tuple[date, date]:
    """When the 303 and 130 for a quarter are filed: 1-20 of the following month, and
    1-30 January for the fourth quarter. (A deadline falling on a holiday moves to the
    next working day; the gestor will know.)"""
    if quarter == 4:
        return date(year + 1, 1, 1), date(year + 1, 1, 30)
    month = 3 * quarter + 1
    return date(year, month, 1), date(year, month, 20)


def quarter_to_file(today: Optional[date] = None) -> Optional[tuple[int, int]]:
    """The quarter whose returns are due right now, if today is in a filing window."""
    today = today or date.today()
    year, quarter = last_closed_quarter(today)
    start, end = filing_window(year, quarter)
    return (year, quarter) if start <= today <= end else None


def last_closed_quarter(today: Optional[date] = None) -> tuple[int, int]:
    today = today or date.today()
    year, quarter = quarter_of(today)
    return (year - 1, 4) if quarter == 1 else (year, quarter - 1)


def deadline_text(year: int, quarter: int) -> str:
    start, end = filing_window(year, quarter)
    return f"del {start.day} al {end.day} de {MONTHS[end.month - 1]} de {end.year}"


def rate_name(rate: float) -> str:
    """How a VAT rate is named on the return: an exempt sale is not "VAT at 0%"."""
    from src.totals import rate_label

    return "Exento de IVA" if not rate else f"IVA al {rate_label(rate)}%"


def is_company(tax_id: Optional[str]) -> bool:
    """A company (S.L., S.A., cooperative...) rather than a person?

    Companies' tax ids start with a letter that names their legal form; a person's NIF
    starts with digits, and a foreigner's NIE with X, Y or Z. Only people file the 130:
    a company's instalments are the modelo 202, on its profit.
    """
    cleaned = re.sub(r"[\s.\-]", "", (tax_id or "")).upper()
    return bool(cleaned) and cleaned[0] in "ABCDEFGHJNPQRSUVW"


# ── The figures ──────────────────────────────────────────────────────────────

def _d(value) -> Decimal:
    return Decimal(str(value or 0))


def issued_between(start: date, end: date) -> list[dict]:
    """Issued invoices dated within [start, end], rectifying ones included."""
    from src import store

    conn = db.connect()
    rows = conn.execute(
        "SELECT * FROM invoices WHERE status = 'issued' AND date BETWEEN ? AND ? "
        "ORDER BY date, number", (start.isoformat(), end.isoformat()),
    ).fetchall()
    return [store._issued_payload(conn, row) for row in rows]


def bills_between(start: date, end: date) -> list[dict]:
    """Supplier bills dated within [start, end], with the supplier's tax id."""
    rows = db.connect().execute(
        "SELECT b.*, c.tax_id AS supplier_tax_id FROM bills b "
        "LEFT JOIN contacts c ON c.id = b.supplier_id "
        "WHERE b.date BETWEEN ? AND ? ORDER BY b.date, b.id",
        (start.isoformat(), end.isoformat()),
    ).fetchall()
    return [dict(r) for r in rows]


@dataclass
class VatReturn:
    """Modelo 303, as far as this software's records go."""

    year: int
    quarter: int
    by_rate: dict = field(default_factory=dict)      # rate -> [base, tax]
    # Sales without VAT by why (src/exemptions key, "" when never said): the return
    # puts intra-EU sales, exports and reverse charge in different boxes.
    without_vat: dict = field(default_factory=dict)  # reason -> base
    output_base: float = 0.0
    output_tax: float = 0.0
    input_base: float = 0.0
    input_tax: float = 0.0
    invoices: int = 0
    bills: int = 0
    bills_without_vat: int = 0

    @property
    def result(self) -> float:
        return round_money(_d(self.output_tax) - _d(self.input_tax))


def vat_return(year: int, quarter: int) -> VatReturn:
    from src.totals import breakdown

    config = get_config()
    start, end = quarter_bounds(year, quarter)
    out = VatReturn(year, quarter)

    for record in issued_between(start, end):
        t = breakdown(record["invoice"], config)
        base, tax = out.by_rate.setdefault(t.tax_rate, [0.0, 0.0])
        out.by_rate[t.tax_rate] = [round_money(_d(base) + _d(t.base)),
                                   round_money(_d(tax) + _d(t.tax))]
        if not t.tax_rate:
            why = record["invoice"].vat_reason or ""
            out.without_vat[why] = round_money(_d(out.without_vat.get(why)) + _d(t.base))
        out.invoices += 1
    out.output_base = round_money(sum(_d(b) for b, _ in out.by_rate.values()))
    out.output_tax = round_money(sum(_d(t) for _, t in out.by_rate.values()))

    for bill in bills_between(start, end):
        out.input_base = round_money(_d(out.input_base) + _d(bill["subtotal"]))
        out.input_tax = round_money(_d(out.input_tax) + _d(bill["tax_amount"]))
        out.bills += 1
        if not bill["tax_amount"]:
            out.bills_without_vat += 1
    return out


def without_vat_rows(vat: VatReturn) -> list[tuple[str, float]]:
    """Sales without VAT as (name, base), by reason in the order of src/exemptions."""
    from src import exemptions

    order = list(exemptions.REASONS)
    rows = []
    for why in sorted(vat.without_vat, key=lambda k: order.index(k) if k in order
                      else len(order)):
        reason = exemptions.get(why)
        name = f"Sin IVA: {reason.label}" if reason else "Sin IVA (sin motivo indicado)"
        rows.append((name, vat.without_vat[why]))
    return rows


def vat_rows(vat: VatReturn) -> list[tuple[str, float, float]]:
    """The return's sales lines as (name, base, VAT): charged by rate, then without."""
    return ([(rate_name(rate), *vat.by_rate[rate])
             for rate in sorted(vat.by_rate, reverse=True) if rate]
            + [(name, base, 0.0) for name, base in without_vat_rows(vat)])


@dataclass
class IrpfInstalment:
    """Modelo 130 boxes 01-07, running totals from 1 January."""

    year: int
    quarter: int
    income: float = 0.0            # [01]
    expenses: float = 0.0          # [02]
    withheld: float = 0.0          # [06]
    previous_payments: float = 0.0  # [05]

    @property
    def net(self) -> float:         # [03]
        return round_money(_d(self.income) - _d(self.expenses))

    @property
    def twenty_percent(self) -> float:  # [04]
        return percent_of(max(self.net, 0), 20)

    @property
    def result(self) -> float:      # [07]
        return round_money(_d(self.twenty_percent) - _d(self.previous_payments)
                           - _d(self.withheld))

    @property
    def to_pay(self) -> float:
        return max(self.result, 0.0)


def irpf_instalment(year: int, quarter: int) -> IrpfInstalment:
    from src.totals import breakdown

    config = get_config()
    start, _ = quarter_bounds(year, 1)
    _, end = quarter_bounds(year, quarter)
    out = IrpfInstalment(year, quarter)

    for record in issued_between(start, end):
        t = breakdown(record["invoice"], config)
        out.income = round_money(_d(out.income) + _d(t.base))
        out.withheld = round_money(_d(out.withheld) + _d(t.irpf))
    for bill in bills_between(start, end):
        out.expenses = round_money(_d(out.expenses) + _d(bill["subtotal"]))
    out.previous_payments = round_money(sum(
        _d(irpf_instalment(year, q).to_pay) for q in range(1, quarter)))
    return out


# ── In words ─────────────────────────────────────────────────────────────────

def summary_text(year: int, quarter: int) -> str:
    """The quarter in a few lines, for Telegram and for the email to the gestor."""
    from src.totals import format_money, rate_label

    config = get_config()
    vat = vat_return(year, quarter)
    lines = [f"📊 IMPUESTOS DEL {label(year, quarter)}",
             f"Plazo: {deadline_text(year, quarter)}", ""]

    lines.append("Modelo 303 (IVA)")
    if vat.by_rate:
        for rate in sorted(vat.by_rate, reverse=True):
            base, tax = vat.by_rate[rate]
            if rate:
                lines.append(f"  IVA repercutido {rate_label(rate)}%: "
                             f"{format_money(tax, config)} (base {format_money(base, config)})")
        for name, base in without_vat_rows(vat):
            lines.append(f"  {name}: {format_money(base, config)}")
    else:
        lines.append("  Sin facturas emitidas en el trimestre.")
    lines.append(f"  IVA soportado deducible: {format_money(vat.input_tax, config)}"
                 f" ({vat.bills} gasto(s))")
    word = "A INGRESAR" if vat.result > 0 else "A COMPENSAR / DEVOLVER"
    lines.append(f"  → {word}: {format_money(abs(vat.result), config)}")
    if vat.bills_without_vat:
        lines.append(f"  ⚠️ {vat.bills_without_vat} gasto(s) sin el IVA desglosado: si lo "
                     "llevaban, se está perdiendo IVA deducible.")

    lines.append("")
    if is_company(config.cif):
        lines.append("Como sociedad no presentas el modelo 130: tus pagos a cuenta son "
                     "el modelo 202, sobre el beneficio (lo calcula tu gestor).")
    else:
        irpf = irpf_instalment(year, quarter)
        lines.append("Modelo 130 (IRPF, acumulado desde enero)")
        lines.append(f"  Ingresos: {format_money(irpf.income, config)} · "
                     f"Gastos: {format_money(irpf.expenses, config)}")
        lines.append(f"  Rendimiento: {format_money(irpf.net, config)} · "
                     f"20%: {format_money(irpf.twenty_percent, config)}")
        if irpf.previous_payments:
            lines.append(f"  Pagado en trimestres anteriores: "
                         f"−{format_money(irpf.previous_payments, config)}")
        if irpf.withheld:
            lines.append(f"  Retenciones que te han hecho: "
                         f"−{format_money(irpf.withheld, config)}")
        lines.append(f"  → A INGRESAR: {format_money(irpf.to_pay, config)}")

    lines.append("")
    lines.append("Cálculo orientativo con lo registrado aquí: tu gestor lo revisa y lo "
                 "presenta.")
    return "\n".join(lines)
