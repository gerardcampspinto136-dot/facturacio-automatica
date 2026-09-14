"""FastAPI web app for reviewing pending invoices.

Everyone signs in with Google. Who they are and what they may do comes from the accounts
table (see src/accounts.py), not from a list in a config file -- that is what lets a
client's owner add and remove their own staff without anyone editing YAML and restarting.

Every route states the one permission it needs via _guard(), so adding a route without
deciding who may use it is a visible omission rather than an accidental hole.

The account panels themselves live in src/web/admin.py.

Set WEB_DEV_NO_AUTH=1 to bypass Google login for local testing.
"""

import html
import os
from datetime import date

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

from src import accounts, bills, store
from src.totals import compute_totals, format_money as _fmt
from src.config_loader import get_config
from src.finalize import finalize_invoice
from src.invoice_generator import generate_invoice_pdf
from src.models import InvoiceData, InvoiceItem
from src.rectify import create_rectifying_invoice

app = FastAPI(title="Revisión de facturas")


def _install_middleware() -> None:
    from starlette.middleware.sessions import SessionMiddleware

    app.add_middleware(
        SessionMiddleware,
        secret_key=os.getenv("SESSION_SECRET", "dev-insecure-secret-change-me"),
    )


_install_middleware()


# ── Google OAuth ─────────────────────────────────────────────────────────────

_oauth = None


def _get_oauth():
    global _oauth
    if _oauth is None:
        from authlib.integrations.starlette_client import OAuth

        oauth = OAuth()
        oauth.register(
            name="google",
            client_id=os.getenv("WEB_OAUTH_CLIENT_ID"),
            client_secret=os.getenv("WEB_OAUTH_CLIENT_SECRET"),
            server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
            client_kwargs={"scope": "openid email profile"},
        )
        _oauth = oauth
    return _oauth


def _current(request: Request):
    """The account behind this request, or None.

    Resolved from the database on every request rather than trusted from the cookie, so
    revoking a permission or deactivating someone takes effect on their very next click
    instead of whenever they happen to log out.
    """
    if os.getenv("WEB_DEV_NO_AUTH") == "1" and not request.session.get("user"):
        return {"id": 0, "email": "dev@local", "name": "Desarrollo",
                "role": accounts.SUPERADMIN, "company_id": None,
                "permissions": [], "active": 1}

    email = request.session.get("user")
    if not email:
        return None

    found = accounts.find_by_email(email)
    user = accounts.load_context(found["id"]) if found else None
    if user is None or not user["active"]:
        return None
    # A suspended client is not "missing a permission", they have no access at all.
    # Treating them as signed out sends them back through the login, which is where
    # authenticate() explains, by name, that the company's access is suspended.
    if user.get("company_status") == accounts.SUSPENDED:
        return None
    return user


def _user(request: Request):
    """Backwards-compatible display name for the header."""
    user = _current(request)
    return user["email"] if user else None


# Managing accounts is safe with any number of companies; reading the books is not,
# until invoices, bills and stock carry a company_id. See _refuse_shared_data.
_ACCOUNT_PERMISSIONS = frozenset({"users.manage", "companies.manage"})


def _refuse_shared_data(user):
    """Stop a second company from being able to read the first one's books.

    Accounts are per-company, but invoices, supplier bills and stock are not yet: there
    is one set of records in the database and every query returns all of it. With a
    single client on the installation -- which is how the software is sold -- that is
    correct. The moment a second active company exists it would be a data leak between
    two customers, so the books are closed to everyone until the data is separated.

    Failing closed is the only safe direction here: the alternative is two clients
    quietly reading each other's invoices, which nobody would notice from the screen.
    """
    active = [c for c in accounts.list_companies() if c["status"] == accounts.ACTIVE]
    if len(active) < 2:
        return None

    names = ", ".join(html.escape(c["name"]) for c in active)
    return _page(
        "Pendiente de separar por empresa",
        "<div class='card'><b>Hay más de una empresa activa en esta instalación.</b>"
        f"<p class='muted'>Activas ahora mismo: {names}.</p>"
        "<p>Las cuentas y los permisos ya van por empresa, pero las facturas, los "
        "gastos y el stock todavía se guardan sin separar, así que una empresa vería "
        "los datos de la otra. Hasta que estén separadas, esta parte queda cerrada.</p>"
        "<p class='muted'>Para seguir: deja una sola empresa activa y suspende las "
        "demás, o instala una copia por cliente.</p>"
        "<div class='actions'><a class='btn btn-neutral' href='/admin'>Ver empresas</a>"
        "</div></div>",
        user,
    )


def _guard(request: Request, permission: str | None = None):
    """Resolve the user and check one permission.

    Returns (user, refusal). A route does nothing until it has checked `refusal`, which
    is either a redirect to the login page or a page explaining what is missing -- a
    blank 403 leaves the client's employee with nothing to tell their boss.
    """
    user = _current(request)
    if user is None:
        # Drop a cookie that no longer corresponds to usable access, so the next login
        # starts clean and can explain itself rather than silently looping.
        request.session.pop("user", None)
        return None, RedirectResponse("/login", status_code=303)

    if permission and permission not in _ACCOUNT_PERMISSIONS:
        blocked = _refuse_shared_data(user)
        if blocked is not None:
            return user, blocked

    if permission and not accounts.can(user, permission):
        label = accounts.PERMISSIONS.get(permission, ("", permission))[1]
        return user, _page(
            "Sin permiso",
            "<div class='card'><b>No tienes permiso para esta parte.</b>"
            f"<p class='muted'>Te falta: {html.escape(label)}</p>"
            "<p class='muted'>Si lo necesitas para tu trabajo, pídeselo a quien "
            "administra las cuentas de tu empresa.</p>"
            "<a class='btn btn-neutral' href='/'>Volver</a></div>",
            user,
        )
    return user, None


# ── HTML helpers ─────────────────────────────────────────────────────────────

_STYLE = """
:root { color-scheme: light dark; }
* { box-sizing: border-box; }
body { font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif; margin: 0;
  background: Canvas; color: CanvasText; }
.wrap { max-width: 820px; margin: 0 auto; padding: 20px 16px 60px; }
header { display: flex; justify-content: space-between; align-items: center; gap: 12px;
  border-bottom: 2px solid #1a3a5c; padding-bottom: 12px; margin-bottom: 20px; flex-wrap: wrap; }
h1 { font-size: 20px; margin: 0; color: #2b6cb0; }
a { color: #2b6cb0; }
.card { border: 1px solid #d0d7de; border-radius: 10px; padding: 16px; margin-bottom: 14px; }
.row { display: flex; justify-content: space-between; gap: 12px; flex-wrap: wrap; }
.muted { color: #6e7781; font-size: 13px; }
.total { font-weight: 700; font-size: 17px; }
.actions { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 12px; }
button, .btn { border: 0; border-radius: 8px; padding: 9px 14px; font-size: 14px; cursor: pointer;
  text-decoration: none; display: inline-block; }
.btn-primary { background: #2f855a; color: #fff; }
.btn-danger { background: #c53030; color: #fff; }
.btn-neutral { background: #e2e8f0; color: #1a202c; }
.btn-warn { background: #dd6b20; color: #fff; }
input, textarea { width: 100%; padding: 8px; border: 1px solid #cbd5e0; border-radius: 6px;
  background: Field; color: FieldText; font-size: 14px; }
label { font-size: 13px; color: #6e7781; display: block; margin: 10px 0 4px; }
table { width: 100%; border-collapse: collapse; font-size: 14px; }
th, td { text-align: left; padding: 6px 8px; border-bottom: 1px solid #e2e8f0; }
td.num, th.num { text-align: right; }
.empty { text-align: center; color: #6e7781; padding: 40px 0; }
.billform { display: grid; grid-template-columns: 2fr 1fr 1.5fr 1fr auto; gap: 8px;
  align-items: center; margin-top: 10px; }
@media (max-width: 700px) { .billform { grid-template-columns: 1fr; } }
.badge { font-size: 12px; padding: 2px 8px; border-radius: 999px; background: #e2e8f0; color: #1a202c; }
"""


def _page(title: str, body: str, user=None) -> HTMLResponse:
    """Render a page. `user` may be an account dict or just an email string.

    The navigation only offers what this account can actually open: showing an employee
    a "Proveedores" tab that refuses them is a worse experience than not showing it.
    """
    nav = ""
    if user:
        if isinstance(user, str):
            user = accounts.find_by_email(user) or {"email": user, "role": "", "permissions": []}

        links = []
        if accounts.can(user, "invoices.view"):
            links.append('<a href="/">Pendientes</a><a href="/issued">Emitidas</a>')
        if accounts.can(user, "bills.view"):
            links.append('<a href="/bills">Proveedores</a>')
        if accounts.can(user, "receivables.view"):
            links.append('<a href="/receivables">Cobros</a>')
        if accounts.can(user, "users.manage"):
            links.append('<a href="/team">Equipo</a>')
        if accounts.can(user, "companies.manage"):
            links.append('<a href="/admin">Empresas</a>')

        who = html.escape(user.get("name") or user.get("email", ""))
        where = user.get("company_name")
        if where:
            who += f" · {html.escape(where)}"

        nav = (
            f'<div class="row" style="gap:14px;align-items:center">'
            f'{"".join(links)}'
            f'<span class="muted">{who} · <a href="/logout">salir</a></span></div>'
        )
    return HTMLResponse(
        f"<!doctype html><html lang='es'><head><meta charset='utf-8'>"
        f"<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>{html.escape(title)}</title><style>{_STYLE}</style></head><body><div class='wrap'>"
        f"<header><h1>{html.escape(title)}</h1>{nav}</header>{body}</div></body></html>"
    )


def _money(value: float) -> str:
    return _fmt(value, get_config())


def _totals(invoice: InvoiceData):
    return compute_totals(invoice, get_config())


# ── Auth routes ──────────────────────────────────────────────────────────────

@app.get("/login")
async def login(request: Request):
    redirect_uri = get_config().web_base_url + "/auth/callback"
    return await _get_oauth().google.authorize_redirect(request, redirect_uri)


@app.get("/auth/callback")
async def auth_callback(request: Request):
    token = await _get_oauth().google.authorize_access_token(request)
    info = token.get("userinfo") or {}
    email = (info.get("email") or "").lower()

    # Access is decided by the accounts table, not by a list in a config file: that is
    # what lets a client's owner add and remove their own staff without an edit and a
    # restart. `refusal` is already phrased for whoever is reading it.
    user, refusal = accounts.authenticate(email)
    if user is None:
        return _page(
            "Sin acceso",
            f"<div class='card'>{html.escape(refusal or 'Cuenta no autorizada.')}</div>",
        )

    # Fill in a name from Google the first time, so the team list is readable.
    if not user.get("name") and info.get("name"):
        accounts.update_user(user["id"], name=info["name"])

    request.session["user"] = user["email"]
    return RedirectResponse("/", status_code=303)


@app.get("/logout")
async def logout(request: Request):
    request.session.pop("user", None)
    return RedirectResponse("/login", status_code=303)


# ── Pending review ───────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    user, refusal = _guard(request, "invoices.view")
    if refusal:
        return refusal

    pend = store.list_pending()
    if not pend:
        body = "<div class='empty'>No hay facturas pendientes de revisión. 🎉</div>"
        return _page("Facturas pendientes", body, user)

    cards = []
    for p in pend:
        inv = p["invoice"]
        _, _, total = _totals(inv)
        email = html.escape(inv.client_email or "⚠️ sin email")
        cards.append(
            f"<div class='card'><div class='row'>"
            f"<div><b>{html.escape(inv.client_name or 'Sin nombre')}</b><br>"
            f"<span class='muted'>{email} · {p.get('created','')[:10]}</span></div>"
            f"<div class='total'>{_money(total)}</div></div>"
            f"<div class='actions'>"
            f"<a class='btn btn-neutral' href='/invoice/{p['token']}/pdf' target='_blank'>Ver PDF</a>"
            f"<a class='btn btn-neutral' href='/invoice/{p['token']}'>Editar</a>"
            f"<form method='post' action='/invoice/{p['token']}/approve' style='display:inline'>"
            f"<button class='btn-primary'>Aprobar y enviar</button></form>"
            f"<form method='post' action='/invoice/{p['token']}/reject' style='display:inline' "
            f"onsubmit=\"return confirm('¿Descartar esta factura?')\">"
            f"<button class='btn-danger'>Rechazar</button></form>"
            f"</div></div>"
        )
    return _page("Facturas pendientes", "".join(cards), user)


@app.get("/invoice/{token}", response_class=HTMLResponse)
async def edit_form(request: Request, token: str):
    user, refusal = _guard(request, "invoices.create")
    if refusal:
        return refusal
    p = store.get_pending(token)
    if not p:
        return _page("No encontrada", "<div class='card'>Esa factura ya no está pendiente.</div>", user)
    inv = p["invoice"]

    item_rows = []
    for idx, it in enumerate(inv.items):
        item_rows.append(
            f"<tr><td><input name='item_desc' value='{html.escape(it.description)}'></td>"
            f"<td><input name='item_qty' value='{it.quantity:g}' style='width:80px'></td>"
            f"<td><input name='item_price' value='{it.unit_price:g}' style='width:100px'></td></tr>"
        )

    body = (
        f"<form method='post' action='/invoice/{token}/edit'><div class='card'>"
        f"<label>Nombre del cliente</label><input name='client_name' value='{html.escape(inv.client_name or '')}'>"
        f"<label>Email</label><input name='client_email' value='{html.escape(inv.client_email or '')}'>"
        f"<label>Dirección</label><input name='client_address' value='{html.escape(inv.client_address or '')}'>"
        f"<label>NIF/CIF</label><input name='client_id' value='{html.escape(inv.client_id or '')}'>"
        f"<label>Conceptos</label>"
        f"<table><tr><th>Descripción</th><th>Cant.</th><th>Precio unit.</th></tr>"
        f"{''.join(item_rows)}</table>"
        f"<label>Notas</label><textarea name='notes' rows='2'>{html.escape(inv.notes or '')}</textarea>"
        f"<div class='actions'><button class='btn-primary'>Guardar cambios</button>"
        f"<a class='btn btn-neutral' href='/'>Cancelar</a></div>"
        f"</div></form>"
    )
    return _page(f"Editar factura de {inv.client_name or ''}", body, user)


@app.post("/invoice/{token}/edit")
async def edit_submit(request: Request, token: str):
    user, refusal = _guard(request, "invoices.create")
    if refusal:
        return refusal
    p = store.get_pending(token)
    if not p:
        return RedirectResponse("/", status_code=303)

    form = await request.form()
    inv: InvoiceData = p["invoice"]
    inv.client_name = form.get("client_name", "").strip()
    inv.client_email = form.get("client_email", "").strip()
    inv.client_address = form.get("client_address", "").strip() or None
    inv.client_id = form.get("client_id", "").strip() or None
    inv.notes = form.get("notes", "").strip() or None

    descs = form.getlist("item_desc")
    qtys = form.getlist("item_qty")
    prices = form.getlist("item_price")
    items = []
    for d, q, pr in zip(descs, qtys, prices):
        if not d.strip():
            continue
        try:
            qty = float(str(q).replace(",", "."))
            price = float(str(pr).replace(",", "."))
        except ValueError:
            qty, price = 1.0, 0.0
        items.append(InvoiceItem(description=d.strip(), quantity=qty, unit_price=price,
                                 total=round(qty * price, 2)))
    if items:
        inv.items = items

    draft_path = p["draft_path"]
    generate_invoice_pdf(inv, draft_path)
    store.update_pending(token, inv, draft_path)
    return RedirectResponse("/", status_code=303)


@app.get("/invoice/{token}/pdf")
async def serve_pdf(request: Request, token: str):
    user, refusal = _guard(request, "invoices.view")
    if refusal:
        return refusal
    p = store.get_pending(token)
    if not p or not os.path.exists(p["draft_path"]):
        return _page("No encontrada", "<div class='card'>PDF no disponible.</div>", user)
    return FileResponse(p["draft_path"], media_type="application/pdf",
                        filename="Borrador_factura.pdf")


@app.post("/invoice/{token}/approve")
async def approve(request: Request, token: str):
    _user_, refusal = _guard(request, "invoices.approve")
    if refusal:
        return refusal
    p = store.get_pending(token)
    if not p:
        return RedirectResponse("/", status_code=303)
    inv: InvoiceData = p["invoice"]
    inv.invoice_number = None  # force a fresh gap-free number on finalize
    finalize_invoice(inv, token=token)
    return RedirectResponse("/", status_code=303)


@app.post("/invoice/{token}/reject")
async def reject(request: Request, token: str):
    _user_, refusal = _guard(request, "invoices.approve")
    if refusal:
        return refusal
    store.remove_pending(token)
    return RedirectResponse("/", status_code=303)


# ── Issued invoices + contra invoices ────────────────────────────────────────

@app.get("/issued", response_class=HTMLResponse)
async def issued(request: Request):
    user, refusal = _guard(request, "invoices.view")
    if refusal:
        return refusal
    records = store.list_issued()
    if not records:
        return _page("Facturas emitidas", "<div class='empty'>Aún no hay facturas emitidas.</div>", user)

    cards = []
    for r in records:
        inv = r["invoice"]
        _, _, total = _totals(inv)
        rectified = r.get("rectified_by")
        badge = f"<span class='badge'>Rectificada por {html.escape(rectified)}</span>" if rectified else ""
        action = ""
        if not rectified and not inv.rectifies:
            action = (
                f"<form method='post' action='/invoice/{html.escape(inv.invoice_number)}/rectify' "
                f"style='display:inline' onsubmit=\"return confirm('¿Emitir factura rectificativa "
                f"que anula {html.escape(inv.invoice_number)}?')\">"
                f"<button class='btn-warn'>Anular (rectificativa)</button></form>"
            )
        cards.append(
            f"<div class='card'><div class='row'>"
            f"<div><b>{html.escape(inv.invoice_number or '')}</b> — {html.escape(inv.client_name or '')}"
            f" {badge}<br><span class='muted'>{r.get('issued_at','')[:10]}</span></div>"
            f"<div class='total'>{_money(total)}</div></div>"
            f"<div class='actions'>{action}</div></div>"
        )
    return _page("Facturas emitidas", "".join(cards), user)


@app.post("/invoice/{number}/rectify")
async def rectify_route(request: Request, number: str):
    user, refusal = _guard(request, "invoices.rectify")
    if refusal:
        return refusal
    try:
        create_rectifying_invoice(number)
    except ValueError as exc:
        return _page("No se pudo anular", f"<div class='card'>{html.escape(str(exc))}</div>", user)
    return RedirectResponse("/issued", status_code=303)


# ── Supplier bills ───────────────────────────────────────────────────────────

@app.get("/bills", response_class=HTMLResponse)
async def bills_page(request: Request):
    user, refusal = _guard(request, "bills.view")
    if refusal:
        return refusal

    unpaid = bills.list_all(unpaid_only=True)
    owed = bills.total_owed()
    overdue = bills.total_owed(overdue_only=True)

    head = (
        f"<div class='card'><div class='row'>"
        f"<div><b>Pendiente de pagar</b><br>"
        f"<span class='muted'>{len(unpaid)} factura(s) de proveedor</span></div>"
        f"<div class='total'>{_money(owed)}</div></div>"
        + (f"<div class='muted'>De las cuales <b>{_money(overdue)}</b> ya vencidas.</div>"
           if overdue else "")
        + "</div>"
    )

    form = (
        "<div class='card'><b>Anotar una factura recibida</b>"
        "<form method='post' action='/bills/new' class='billform'>"
        "<input name='supplier' placeholder='Proveedor' required>"
        "<input name='total' type='number' step='0.01' placeholder='Importe total (con IVA)' required>"
        "<input name='reference' placeholder='Su nº de factura (opcional)'>"
        "<input name='due_date' type='date' title='Vencimiento (opcional)'>"
        "<button class='btn-primary'>Guardar</button>"
        "</form></div>"
    )

    cards = []
    today = date.today().isoformat()
    for b in unpaid:
        due = b["due_date"] or ""
        late = due and due < today
        when = (f"<span class='badge'>Vencida el {due}</span>" if late
                else f"<span class='muted'>Vence el {due}</span>" if due else "")
        ref = f" · {html.escape(b['reference'])}" if b.get("reference") else ""
        cards.append(
            f"<div class='card'><div class='row'>"
            f"<div><b>{html.escape(b['supplier_name'])}</b>{ref}<br>{when}</div>"
            f"<div class='total'>{_money(b['total'])}</div></div>"
            f"<div class='actions'>"
            f"<form method='post' action='/bills/{b['id']}/paid' style='display:inline'>"
            f"<button class='btn-primary'>Marcar pagada</button></form>"
            f"<form method='post' action='/bills/{b['id']}/delete' style='display:inline' "
            f"onsubmit=\"return confirm('¿Borrar esta factura de proveedor?')\">"
            f"<button class='btn-warn'>Borrar</button></form>"
            f"</div></div>"
        )
    if not cards:
        cards.append("<div class='card'>No hay facturas de proveedor pendientes.</div>")

    return _page("Pagos a proveedores", head + form + "".join(cards), user)


@app.post("/bills/new")
async def bills_new(request: Request):
    _user_, refusal = _guard(request, "bills.manage")
    if refusal:
        return refusal
    form = await request.form()
    try:
        total = float(str(form.get("total", "0")).replace(",", "."))
    except ValueError:
        total = 0.0
    supplier = (form.get("supplier") or "").strip()
    if supplier and total:
        raw_due = (form.get("due_date") or "").strip()
        bills.create(
            supplier, total,
            reference=(form.get("reference") or "").strip() or None,
            due_date=date.fromisoformat(raw_due) if raw_due else None,
        )
    return RedirectResponse("/bills", status_code=303)


@app.post("/bills/{bill_id}/paid")
async def bills_paid(request: Request, bill_id: int):
    _user_, refusal = _guard(request, "bills.manage")
    if refusal:
        return refusal
    bills.mark_paid(bill_id)
    return RedirectResponse("/bills", status_code=303)


@app.post("/bills/{bill_id}/delete")
async def bills_delete(request: Request, bill_id: int):
    _user_, refusal = _guard(request, "bills.manage")
    if refusal:
        return refusal
    bills.delete(bill_id)
    return RedirectResponse("/bills", status_code=303)


# ── Money owed to us ─────────────────────────────────────────────────────────

@app.get("/receivables", response_class=HTMLResponse)
async def receivables_page(request: Request):
    user, refusal = _guard(request, "receivables.view")
    if refusal:
        return refusal

    unpaid = store.list_unpaid()
    total = sum(_totals(u["invoice"])[2] for u in unpaid)
    late = [u for u in unpaid if u["days_overdue"] > 0]

    head = (
        f"<div class='card'><div class='row'>"
        f"<div><b>Pendiente de cobrar</b><br>"
        f"<span class='muted'>{len(unpaid)} factura(s), {len(late)} vencida(s)</span></div>"
        f"<div class='total'>{_money(total)}</div></div></div>"
    )

    cards = []
    for u in unpaid:
        inv = u["invoice"]
        when = (f"<span class='badge'>{u['days_overdue']} día(s) de retraso</span>"
                if u["days_overdue"] > 0
                else f"<span class='muted'>Vence el {u['due_date']}</span>")
        cards.append(
            f"<div class='card'><div class='row'>"
            f"<div><b>{html.escape(inv.invoice_number or '')}</b> — "
            f"{html.escape(inv.client_name or '')}<br>{when}</div>"
            f"<div class='total'>{_money(_totals(inv)[2])}</div></div>"
            f"<div class='actions'>"
            f"<form method='post' action='/receivables/{html.escape(inv.invoice_number)}/paid' "
            f"style='display:inline'><button class='btn-primary'>Marcar cobrada</button></form>"
            f"</div></div>"
        )
    if not cards:
        cards.append("<div class='card'>Todo cobrado. 🎉</div>")

    return _page("Cobros pendientes", head + "".join(cards), user)


@app.post("/receivables/{number}/paid")
async def receivables_paid(request: Request, number: str):
    _user_, refusal = _guard(request, "receivables.manage")
    if refusal:
        return refusal
    store.mark_paid(number)
    return RedirectResponse("/receivables", status_code=303)


# ── Account management ───────────────────────────────────────────────────────
# The team and vendor panels live in their own module; importing it registers its
# routes on `app`. Kept separate because managing who may do what has nothing to do
# with reviewing invoices, and this file is long enough already.

from src.web import admin as _admin  # noqa: E402,F401  (imported for its routes)
