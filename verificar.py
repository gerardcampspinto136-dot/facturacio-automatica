"""Comprueba que todo el sistema funciona. Ejecutar con:  py verificar.py

Va de lo que no necesita internet a lo que sí, y cada comprobación dice qué hacer si
falla. Es para responder a "¿esto funciona?" sin tener que leer código: lo prueba de
verdad -- crea una factura, mueve stock, genera un PDF y llama a las APIs -- sobre una
base de datos temporal, sin tocar los datos reales.

Con --rapido se salta todo lo que sale a internet.
"""

import os
import sys
import tempfile
import traceback
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

OK, FAIL, WARN, SKIP = "  [OK]  ", " [FALLA]", " [AVISO]", " [SALTA]"
_results: list[tuple[str, str, str]] = []


def section(title: str) -> None:
    print(f"\n{title}\n{'-' * len(title)}")


def run(name: str, fn) -> None:
    try:
        state, detail = fn()
    except Exception as exc:
        state, detail = "fail", f"{type(exc).__name__}: {exc}"
        if os.getenv("VERBOSE"):
            traceback.print_exc()
    mark = {"ok": OK, "warn": WARN, "skip": SKIP, "fail": FAIL}[state]
    print(f"{mark} {name}" + (f"\n         {detail}" if detail else ""))
    _results.append((state, name, detail))


# ── 1. Configuración ─────────────────────────────────────────────────────────

def check_config():
    from src.config_loader import get_config

    cfg = get_config()
    if cfg.is_placeholder:
        return "warn", (f"La empresa es todavía «{cfg.name}» (datos de prueba). "
                        "Las facturas saldrán marcadas como DOCUMENTO DE PRUEBA.")
    return "ok", f"{cfg.name} · CIF {cfg.cif} · IVA {cfg.tax_rate}%"


def check_env():
    missing = [k for k in ("TELEGRAM_BOT_TOKEN", "GROQ_API_KEY") if not os.getenv(k)]
    if missing == ["TELEGRAM_BOT_TOKEN"]:
        return "warn", "Sin TELEGRAM_BOT_TOKEN en .env (se puede poner en el panel)."
    if missing:
        return "fail", f"Faltan en .env: {', '.join(missing)}"
    return "ok", ""


def check_superadmin():
    value = (os.getenv("SUPERADMIN_EMAILS") or "").strip()
    if not value:
        return "fail", "SUPERADMIN_EMAILS vacío: no podrás entrar al panel /admin."
    return "ok", value


# ── 2. Base de datos y cuentas ───────────────────────────────────────────────

def check_database():
    from src import db

    tables = {r["name"] for r in db.connect().execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    needed = {"companies", "users", "contacts", "products", "stock_moves",
              "invoices", "invoice_items", "bills", "counters"}
    missing = needed - tables
    if missing:
        return "fail", f"Faltan tablas: {', '.join(sorted(missing))}"
    return "ok", f"{len(tables)} tablas"


def check_accounts():
    from src import accounts

    companies = accounts.list_companies()
    users = accounts.list_users()
    if not companies:
        return "warn", ("Todavía no hay ninguna empresa cliente. Créala en /admin "
                        "la primera vez que entres.")
    detail = f"{len(companies)} empresa(s), {len(users)} cuenta(s)"
    unconfigured = [c["name"] for c in companies if accounts.missing_settings(c)]
    if unconfigured:
        return "warn", f"{detail}. Sin configurar: {', '.join(unconfigured)}"
    return "ok", detail


def check_permissions():
    """Las reglas de acceso, sobre una base de datos de usar y tirar."""
    from src import accounts, db

    with _sandbox():
        company = accounts.create_company("Prueba S.L.", tax_id="B12345678")
        owner = accounts.load_context(
            accounts.create_user("jefe@prueba.es", accounts.ADMIN, company_id=company))
        worker = accounts.load_context(accounts.create_user(
            "curro@prueba.es", accounts.EMPLOYEE, company_id=company,
            permissions=["invoices.view"]))

        assert accounts.can(owner, "invoices.approve"), "el jefe debería poder aprobar"
        assert accounts.can(worker, "invoices.view"), "el empleado debería poder ver"
        assert not accounts.can(worker, "invoices.approve"), "¡el empleado NO debería aprobar!"
        assert not accounts.can(worker, "companies.manage"), "¡fuga de permisos!"
        accounts.set_company_status(company, accounts.SUSPENDED)
        assert not accounts.can(accounts.load_context(owner["id"]), "invoices.view"), \
            "¡una empresa suspendida sigue entrando!"
    return "ok", "roles, permisos y suspensión"


# ── 3. El circuito del dinero ────────────────────────────────────────────────

class _sandbox:
    """Base de datos temporal, para probar sin tocar los datos reales."""

    def __enter__(self):
        from src import db

        self._old = db.DB_PATH
        self._dir = tempfile.mkdtemp(prefix="verificar_")
        db.set_db_path(Path(self._dir) / "prueba.db")
        db.connect()
        return self

    def __exit__(self, *exc):
        from src import db

        db.close()
        db.set_db_path(self._old)
        return False


def check_vat():
    from src.config_loader import get_config
    from src.models import InvoiceData, InvoiceItem
    from src.totals import compute_totals, normalize_prices

    cfg = get_config()
    inv = InvoiceData(client_name="X", client_email="x@y.es",
                      items=[InvoiceItem("Servicio", 1, 100.0)],
                      prices_include_tax=False)
    base, tax, total = compute_totals(inv, cfg)
    expected = round(100 * (1 + cfg.tax_rate / 100), 2)
    assert total == expected, f"100 + IVA debería ser {expected}, sale {total}"

    inc = InvoiceData(client_name="X", client_email="x@y.es",
                      items=[InvoiceItem("Servicio", 1, 121.0)],
                      prices_include_tax=True)
    normalize_prices(inc, cfg)
    _, _, total_inc = compute_totals(inc, cfg)
    assert total_inc == 121.0, f"121 IVA incluido debería seguir siendo 121, sale {total_inc}"
    return "ok", f"IVA {cfg.tax_rate}% en ambos sentidos"


def check_numbering():
    from src import store
    from src.invoice_number import get_next_invoice_number

    with _sandbox():
        numbers = [get_next_invoice_number() for _ in range(3)]
        assert len(set(numbers)) == 3, f"números repetidos: {numbers}"
        tails = [int(n.split("-")[-1]) for n in numbers]
        assert tails == sorted(tails) and tails[-1] - tails[0] == 2, \
            f"la numeración tiene saltos: {numbers}"
    return "ok", f"correlativa y sin saltos ({numbers[0]} → {numbers[-1]})"


def check_stock():
    from src import catalog
    from src.models import InvoiceData, InvoiceItem

    with _sandbox():
        pid = catalog.create("Tornillos M8", unit_price=0.25, stock_qty=100,
                             reorder_point=20)
        moves = catalog.apply_invoice(InvoiceData(
            client_name="X", client_email="x@y.es", invoice_number="2026-0001",
            items=[InvoiceItem("20 tornillos M8 inoxidables", 20, 0.25)]))
        assert moves, "la factura no ha descontado stock"
        left = catalog.get(pid)["stock_qty"]
        assert left == 80, f"deberían quedar 80, quedan {left}"

        catalog.move(pid, 50, reason=catalog.PURCHASE)
        assert catalog.get(pid)["stock_qty"] == 130, "la entrada de material falla"
    return "ok", "descuento automático al facturar y entrada de material"


def check_bills():
    from src import bills

    with _sandbox():
        bid = bills.create("Ferretería Puig", 242.61, reference="F-2026/0412")
        assert bills.total_owed() == 242.61, "no cuadra lo pendiente de pagar"
        bills.mark_paid(bid)
        assert bills.total_owed() == 0, "marcar pagada no descuenta"
    return "ok", "gastos de proveedor y vencimientos"


def check_receipt_reading():
    """La parte del ticket que no necesita internet: las cuentas."""
    from src import receipts

    data = {"supplier_name": "Supermercat", "total": 22.93, "subtotal": 20.82,
            "tax_amount": 3.11, "date": "12/09/2026", "paid": True}
    r = receipts.build_receipt(data)
    assert r.date.isoformat() == "2026-09-12", f"fecha mal leída: {r.date}"
    assert r.tax_amount == 3.11, "se ha perdido el IVA deducible"
    assert abs(r.subtotal + r.tax_amount - r.total) < 0.02, "base + IVA no cuadra"
    assert receipts.build_receipt({**data, "total": None}).total is None, \
        "se está inventando un importe"
    return "ok", "fechas, IVA deducible y no inventar importes"


def check_pdf():
    from src.invoice_generator import generate_invoice_pdf
    from src.models import InvoiceData, InvoiceItem

    out = Path(tempfile.mkdtemp(prefix="verificar_")) / "factura.pdf"
    generate_invoice_pdf(InvoiceData(
        client_name="Cliente de prueba", client_email="c@x.es", client_id="B12345678",
        items=[InvoiceItem("Servicio", 1, 100.0)], invoice_number="PRUEBA-0001"), str(out))
    size = out.stat().st_size
    assert size > 1000, f"el PDF sale vacío ({size} bytes)"
    return "ok", f"generado, {size // 1024} KB"


def check_web():
    """Arranca la web en memoria y comprueba que el acceso está protegido."""
    os.environ.pop("WEB_DEV_NO_AUTH", None)
    from fastapi.testclient import TestClient

    from src.web import app as web

    client = TestClient(web.app, follow_redirects=False)
    for path in ("/", "/admin", "/team", "/bills"):
        code = client.get(path).status_code
        assert code == 303, f"{path} no pide identificarse (devuelve {code})"
    return "ok", "todas las páginas piden identificarse"


# ── 4. Servicios externos ────────────────────────────────────────────────────

def check_telegram():
    import urllib.request
    import json

    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        return "skip", "sin TELEGRAM_BOT_TOKEN"
    with urllib.request.urlopen(
            f"https://api.telegram.org/bot{token}/getMe", timeout=15) as response:
        info = json.load(response)
    if not info.get("ok"):
        return "fail", "Telegram rechaza el token"
    return "ok", f"@{info['result']['username']}"


def check_groq():
    from groq import Groq

    if not os.getenv("GROQ_API_KEY"):
        return "skip", "sin GROQ_API_KEY"
    models = {m.id for m in Groq(api_key=os.environ["GROQ_API_KEY"]).models.list().data}
    from src.parser import GROQ_MODEL
    from src.receipts import GROQ_VISION_MODEL

    missing = [m for m in (GROQ_MODEL, GROQ_VISION_MODEL) if m not in models]
    if missing:
        return "warn", f"Estos modelos ya no están disponibles: {', '.join(missing)}"
    return "ok", f"texto y visión disponibles ({len(models)} modelos)"


def check_google():
    token_path = os.getenv("GOOGLE_TOKEN_PATH", "config/credentials/google_token.json")
    if not Path(token_path).exists():
        return "warn", ("Sin autorizar. Ejecuta `py authorize_google.py` para "
                        "Sheets y Gmail.")
    from src.sheets import add_invoice_to_sheet  # noqa: F401  (import = credenciales ok)
    return "ok", "credenciales presentes"


# ── Ejecución ────────────────────────────────────────────────────────────────

def main() -> int:
    quick = "--rapido" in sys.argv or "--quick" in sys.argv

    print("\n" + "=" * 62)
    print("  COMPROBACIÓN DEL SISTEMA DE FACTURACIÓN")
    print("=" * 62)

    section("1. Configuración")
    run("Datos de la empresa", check_config)
    run("Claves en .env", check_env)
    run("Acceso de administrador", check_superadmin)

    section("2. Base de datos, cuentas y permisos")
    run("Estructura de la base de datos", check_database)
    run("Empresas y cuentas", check_accounts)
    run("Roles y permisos", check_permissions)

    section("3. Facturación")
    run("Cálculo del IVA", check_vat)
    run("Numeración de facturas", check_numbering)
    run("Stock: descuento automático", check_stock)
    run("Gastos de proveedor", check_bills)
    run("Lectura de tickets", check_receipt_reading)
    run("Generación del PDF", check_pdf)
    run("Web y control de acceso", check_web)

    section("4. Servicios externos" + (" (saltados)" if quick else ""))
    if quick:
        for name in ("Telegram", "Groq", "Google"):
            print(f"{SKIP} {name}")
    else:
        run("Bot de Telegram", check_telegram)
        run("Groq (voz y visión)", check_groq)
        run("Google (Sheets y Gmail)", check_google)

    failures = [r for r in _results if r[0] == "fail"]
    warnings = [r for r in _results if r[0] == "warn"]

    print("\n" + "=" * 62)
    if failures:
        print(f"  {len(failures)} COMPROBACIÓN(ES) FALLIDA(S)")
        for _, name, detail in failures:
            print(f"   ✗ {name}: {detail}")
    else:
        print("  TODO CORRECTO" + (f", con {len(warnings)} aviso(s)" if warnings else ""))
    if warnings:
        for _, name, detail in warnings:
            print(f"   ! {name}: {detail}")
    print("=" * 62 + "\n")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
