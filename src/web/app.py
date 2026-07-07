"""FastAPI web app for reviewing pending invoices.

Reviewers sign in with Google (email allowlisted in config.reviewers), then approve, edit,
or reject each pending invoice, and can issue contra / rectifying invoices for issued ones.

Set WEB_DEV_NO_AUTH=1 to bypass Google login for local testing.
"""

import html
import os
from datetime import date

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

from src import store
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


def _user(request: Request):
    if os.getenv("WEB_DEV_NO_AUTH") == "1":
        return request.session.get("user", "dev@local")
    return request.session.get("user")


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
.badge { font-size: 12px; padding: 2px 8px; border-radius: 999px; background: #e2e8f0; color: #1a202c; }
"""


def _page(title: str, body: str, user: str | None = None) -> HTMLResponse:
    nav = ""
    if user:
        nav = (
            f'<div class="row" style="gap:14px;align-items:center">'
            f'<a href="/">Pendientes</a><a href="/issued">Emitidas</a>'
            f'<span class="muted">{html.escape(user)} · <a href="/logout">salir</a></span></div>'
        )
    return HTMLResponse(
        f"<!doctype html><html lang='es'><head><meta charset='utf-8'>"
        f"<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>{html.escape(title)}</title><style>{_STYLE}</style></head><body><div class='wrap'>"
        f"<header><h1>{html.escape(title)}</h1>{nav}</header>{body}</div></body></html>"
    )


def _money(value: float) -> str:
    cfg = get_config()
    return f"{value:,.2f} {cfg.currency_symbol}"


def _totals(invoice: InvoiceData):
    cfg = get_config()
    subtotal = invoice.subtotal
    tax = round(subtotal * cfg.tax_rate / 100, 2)
    return subtotal, tax, round(subtotal + tax, 2)


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
    reviewers = [r.lower() for r in get_config().reviewers]
    if reviewers and email not in reviewers:
        return _page("Acceso denegado",
                     f"<div class='card'>La cuenta <b>{html.escape(email)}</b> no está autorizada "
                     "para revisar facturas.</div>")
    request.session["user"] = email
    return RedirectResponse("/", status_code=303)


@app.get("/logout")
async def logout(request: Request):
    request.session.pop("user", None)
    return RedirectResponse("/login", status_code=303)


# ── Pending review ───────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    user = _user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

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
    user = _user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
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
    user = _user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
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
    user = _user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    p = store.get_pending(token)
    if not p or not os.path.exists(p["draft_path"]):
        return _page("No encontrada", "<div class='card'>PDF no disponible.</div>", user)
    return FileResponse(p["draft_path"], media_type="application/pdf",
                        filename="Borrador_factura.pdf")


@app.post("/invoice/{token}/approve")
async def approve(request: Request, token: str):
    user = _user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    p = store.get_pending(token)
    if not p:
        return RedirectResponse("/", status_code=303)
    inv: InvoiceData = p["invoice"]
    inv.invoice_number = None  # force a fresh gap-free number on finalize
    finalize_invoice(inv)
    store.remove_pending(token)
    return RedirectResponse("/", status_code=303)


@app.post("/invoice/{token}/reject")
async def reject(request: Request, token: str):
    user = _user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    store.remove_pending(token)
    return RedirectResponse("/", status_code=303)


# ── Issued invoices + contra invoices ────────────────────────────────────────

@app.get("/issued", response_class=HTMLResponse)
async def issued(request: Request):
    user = _user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
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
    user = _user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    try:
        create_rectifying_invoice(number)
    except ValueError as exc:
        return _page("No se pudo anular", f"<div class='card'>{html.escape(str(exc))}</div>", user)
    return RedirectResponse("/issued", status_code=303)
