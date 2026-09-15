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
import logging
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

logger = logging.getLogger(__name__)

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


_LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost", "testclient"}


def _dev_bypass_allowed(request: Request) -> bool:
    """Is WEB_DEV_NO_AUTH in force for this request?

    Only ever for a browser on the same machine. The flag is convenient while setting a
    client up -- it opens the panel with no Google round-trip -- and catastrophic if it
    is still set the day the panel is put on a public address, because the panel creates
    companies and reads every client's books.

    Tying it to the caller's address means a forgotten flag cannot expose anything: a
    remote visitor is asked to log in regardless of what .env says.
    """
    if os.getenv("WEB_DEV_NO_AUTH") != "1":
        return False
    client = getattr(request, "client", None)
    host = (client.host if client else "") or ""
    if host not in _LOCAL_HOSTS:
        logger.warning(
            "WEB_DEV_NO_AUTH is set but %s is not local: requiring a real login. "
            "Remove WEB_DEV_NO_AUTH from .env on anything reachable from outside.",
            host,
        )
        return False
    return True


def _current(request: Request):
    """The account behind this request, or None.

    Resolved from the database on every request rather than trusted from the cookie, so
    revoking a permission or deactivating someone takes effect on their very next click
    instead of whenever they happen to log out.
    """
    if _dev_bypass_allowed(request) and not request.session.get("user"):
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
/* ── Design tokens ──────────────────────────────────────────────────────────
   One place for colour, spacing and type. Everything below refers to these, so
   restyling for a client is changing a handful of values, not hunting hex codes. */
:root {
  --bg: #f6f7f9;
  --surface: #ffffff;
  --surface-2: #fbfcfd;
  --border: #e3e8ee;
  --border-strong: #d3dae3;
  --text: #1a2233;
  --text-muted: #667085;
  --text-faint: #98a2b3;
  --brand: #1f3a5f;
  --brand-soft: #eef2f7;
  /* The primary button needs its own pair: in dark mode --brand becomes a pale tint
     for text and headings, and white lettering on top of that is unreadable. */
  --btn-bg: #1f3a5f;
  --btn-fg: #ffffff;
  --accent: #2563eb;
  --ok: #067647;
  --ok-soft: #ecfdf3;
  --warn: #b54708;
  --warn-soft: #fffaeb;
  --danger: #b42318;
  --danger-soft: #fef3f2;
  --shadow: 0 1px 2px rgba(16,24,40,.06), 0 1px 3px rgba(16,24,40,.04);
  --shadow-lg: 0 4px 12px rgba(16,24,40,.08);
  --radius: 10px;
  --sidebar: 248px;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #0f1420; --surface: #161c2a; --surface-2: #1a2130;
    --border: #262f42; --border-strong: #33405a;
    --text: #e8ecf4; --text-muted: #94a3b8; --text-faint: #64748b;
    --brand: #b9cbe6; --brand-soft: #1c2739;
    --btn-bg: #2f6feb; --btn-fg: #ffffff;
    --accent: #6ea8fe;
    --ok: #4ade80; --ok-soft: #10261c;
    --warn: #fbbf24; --warn-soft: #2a2010;
    --danger: #f87171; --danger-soft: #2b1614;
    --shadow: none; --shadow-lg: none;
  }
}

* { box-sizing: border-box; }
html, body { margin: 0; padding: 0; }
body {
  font-family: "Inter", system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
  background: var(--bg); color: var(--text);
  font-size: 15px; line-height: 1.5;
  -webkit-font-smoothing: antialiased;
}
a { color: var(--accent); text-decoration: none; }
a:hover { text-decoration: underline; }

/* ── Shell: sidebar + content ─────────────────────────────────────────────── */
.shell { display: flex; min-height: 100vh; }

.sidebar {
  width: var(--sidebar); flex: 0 0 var(--sidebar);
  background: var(--surface); border-right: 1px solid var(--border);
  display: flex; flex-direction: column; position: sticky; top: 0; height: 100vh;
}
.brand {
  display: flex; align-items: center; gap: 10px;
  padding: 18px 18px 14px; border-bottom: 1px solid var(--border);
}
.brand-mark {
  width: 32px; height: 32px; border-radius: 8px; flex: 0 0 32px;
  background: var(--brand); color: #fff; display: grid; place-items: center;
  font-weight: 700; font-size: 14px; letter-spacing: .5px;
}
.brand-name { font-weight: 650; font-size: 14px; line-height: 1.2; }
.brand-sub { font-size: 12px; color: var(--text-faint); }

.nav { padding: 12px 10px; overflow-y: auto; flex: 1; }
.nav-group { margin-bottom: 14px; }
.nav-label {
  font-size: 11px; font-weight: 600; letter-spacing: .06em; text-transform: uppercase;
  color: var(--text-faint); padding: 0 10px 6px;
}
.nav a {
  display: flex; align-items: center; gap: 10px;
  padding: 8px 10px; border-radius: 8px; margin-bottom: 2px;
  color: var(--text); font-size: 14px; text-decoration: none;
}
.nav a:hover { background: var(--brand-soft); text-decoration: none; }
.nav a.active { background: var(--brand-soft); color: var(--brand); font-weight: 600; }
.nav .ico { width: 18px; text-align: center; flex: 0 0 18px; opacity: .85; }
.nav .count {
  margin-left: auto; font-size: 11px; font-weight: 600;
  background: var(--danger-soft); color: var(--danger);
  padding: 1px 7px; border-radius: 999px;
}

.whoami {
  border-top: 1px solid var(--border); padding: 12px 14px;
  display: flex; align-items: center; gap: 10px;
}
.avatar {
  width: 32px; height: 32px; flex: 0 0 32px; border-radius: 50%;
  background: var(--brand-soft); color: var(--brand);
  display: grid; place-items: center; font-weight: 650; font-size: 13px;
}
.whoami-text { min-width: 0; }
.whoami-name {
  font-size: 13px; font-weight: 600;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.whoami-sub { font-size: 12px; color: var(--text-faint); }

.content { flex: 1; min-width: 0; padding: 26px 30px 60px; max-width: 1100px; }
.page-head {
  display: flex; align-items: flex-start; justify-content: space-between;
  gap: 16px; flex-wrap: wrap; margin-bottom: 22px;
}
h1 { font-size: 22px; font-weight: 650; margin: 0; letter-spacing: -.01em; }
.page-sub { color: var(--text-muted); font-size: 14px; margin-top: 3px; }
h2 { font-size: 16px; font-weight: 650; margin: 26px 0 12px; }

/* ── Cards, stats, tables ─────────────────────────────────────────────────── */
.card {
  background: var(--surface); border: 1px solid var(--border);
  border-radius: var(--radius); padding: 18px; margin-bottom: 14px;
  box-shadow: var(--shadow);
}
.card-title { font-weight: 650; font-size: 15px; margin-bottom: 4px; }
.card-hint { color: var(--text-muted); font-size: 13px; margin-bottom: 14px; }

.stats { display: grid; gap: 12px; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr)); margin-bottom: 8px; }
.stat {
  background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius);
  padding: 15px 16px; box-shadow: var(--shadow); display: block; color: inherit;
}
a.stat:hover { border-color: var(--border-strong); box-shadow: var(--shadow-lg); text-decoration: none; }
.stat-label { font-size: 12.5px; color: var(--text-muted); display: flex; align-items: center; gap: 6px; }
.stat-value { font-size: 25px; font-weight: 680; letter-spacing: -.02em; margin-top: 5px; }
.stat-note { font-size: 12px; color: var(--text-faint); margin-top: 2px; }
.stat-note.bad { color: var(--danger); }
.stat-note.good { color: var(--ok); }

table { width: 100%; border-collapse: collapse; font-size: 14px; }
thead th {
  text-align: left; font-size: 11.5px; font-weight: 600; text-transform: uppercase;
  letter-spacing: .05em; color: var(--text-faint);
  padding: 8px 10px; border-bottom: 1px solid var(--border);
}
tbody td { padding: 11px 10px; border-bottom: 1px solid var(--border); vertical-align: middle; }
tbody tr:last-child td { border-bottom: 0; }
tbody tr:hover { background: var(--surface-2); }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
.table-wrap { overflow-x: auto; }
.strong { font-weight: 600; }

/* ── Buttons ──────────────────────────────────────────────────────────────── */
button, .btn {
  border: 1px solid transparent; border-radius: 8px; padding: 8px 14px;
  font-size: 13.5px; font-weight: 550; font-family: inherit; cursor: pointer;
  text-decoration: none; display: inline-flex; align-items: center; gap: 6px;
  line-height: 1.4;
}
button:hover, .btn:hover { text-decoration: none; filter: brightness(.96); }
.btn-primary { background: var(--btn-bg); color: var(--btn-fg); }
.btn-danger { background: var(--danger); color: #fff; }
.btn-warn { background: var(--warn); color: #fff; }
.btn-neutral { background: var(--surface); color: var(--text); border-color: var(--border-strong); }
.btn-sm { padding: 5px 10px; font-size: 12.5px; }
.actions { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
.card > .actions { margin-top: 14px; }

/* ── Forms ────────────────────────────────────────────────────────────────── */
label { display: block; font-size: 13px; font-weight: 550; margin: 14px 0 5px; }
label:first-of-type { margin-top: 0; }
input, textarea, select {
  width: 100%; padding: 9px 11px; font-size: 14px; font-family: inherit;
  border: 1px solid var(--border-strong); border-radius: 8px;
  background: var(--surface); color: var(--text);
}
input:focus, textarea:focus, select:focus {
  outline: none; border-color: var(--accent); box-shadow: 0 0 0 3px rgba(37,99,235,.12);
}
.field-hint { font-weight: 400; color: var(--text-faint); }
.grid-2 { display: grid; gap: 0 16px; grid-template-columns: 1fr 1fr; }
.billform { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; margin-top: 12px; }
.billform input { width: auto; flex: 1 1 160px; }
.perms { columns: 2; column-gap: 26px; margin-top: 8px; }
.perm-group { break-inside: avoid; margin-bottom: 14px; }
.perm-group b { font-size: 12px; text-transform: uppercase; letter-spacing: .05em; color: var(--text-faint); }
.perm {
  display: flex; gap: 9px; align-items: center; margin: 7px 0;
  font-size: 13.5px; font-weight: 400; cursor: pointer;
}
.perm input { width: auto; flex: 0 0 auto; }

/* ── Badges and notices ───────────────────────────────────────────────────── */
.badge {
  display: inline-flex; align-items: center; gap: 4px;
  font-size: 11.5px; font-weight: 600; padding: 2px 9px; border-radius: 999px;
  background: var(--brand-soft); color: var(--brand); white-space: nowrap;
}
.badge-ok { background: var(--ok-soft); color: var(--ok); }
.badge-warn { background: var(--warn-soft); color: var(--warn); }
.badge-danger { background: var(--danger-soft); color: var(--danger); }
.badge-muted { background: var(--bg); color: var(--text-faint); }

.notice {
  border: 1px solid var(--border); border-left: 3px solid var(--text-faint);
  background: var(--surface); border-radius: 8px; padding: 13px 15px; margin-bottom: 14px;
  font-size: 14px;
}
.notice-ok { border-left-color: var(--ok); background: var(--ok-soft); }
.notice-warn { border-left-color: var(--warn); background: var(--warn-soft); }
.notice-danger { border-left-color: var(--danger); background: var(--danger-soft); }
.notice b { display: block; margin-bottom: 2px; }

.muted { color: var(--text-muted); font-size: 13px; }
.total { font-weight: 650; font-size: 16px; font-variant-numeric: tabular-nums; }
.row { display: flex; justify-content: space-between; gap: 14px; flex-wrap: wrap; align-items: flex-start; }
.empty {
  text-align: center; color: var(--text-muted); padding: 48px 20px;
  background: var(--surface); border: 1px dashed var(--border-strong); border-radius: var(--radius);
}
.empty-title { font-weight: 600; color: var(--text); margin-bottom: 4px; }

/* ── Small screens ────────────────────────────────────────────────────────── */
@media (max-width: 860px) {
  .shell { flex-direction: column; }
  .sidebar { width: 100%; flex: none; height: auto; position: static; border-right: 0;
             border-bottom: 1px solid var(--border); }
  .nav { display: flex; gap: 4px; overflow-x: auto; padding: 8px 10px; }
  .nav-group { margin: 0; display: flex; gap: 4px; }
  .nav-label { display: none; }
  .nav a { white-space: nowrap; padding: 7px 12px; }
  .nav .count { margin-left: 6px; }
  .whoami { border-top: 0; border-bottom: 1px solid var(--border); }
  .content { padding: 18px 16px 50px; }
  .grid-2, .perms { grid-template-columns: 1fr; columns: 1; }
}
"""

# Sidebar entries: (permission, href, icon, label). A None permission is always shown.
_NAV = (
    ("Facturación", (
        (None, "/", "◎", "Inicio"),
        ("invoices.view", "/pending", "◷", "Pendientes"),
        ("invoices.view", "/issued", "▤", "Emitidas"),
        ("receivables.view", "/receivables", "↓", "Cobros"),
    )),
    ("Gastos", (
        ("bills.view", "/bills", "↑", "Proveedores"),
    )),
    ("Administración", (
        ("users.manage", "/team", "◍", "Equipo"),
        ("companies.manage", "/admin", "⌂", "Empresas"),
    )),
)


def _initials(user: dict) -> str:
    source = (user.get("name") or user.get("email") or "?").strip()
    parts = [p for p in source.replace(".", " ").replace("@", " ").split() if p]
    if len(parts) >= 2:
        return (parts[0][0] + parts[1][0]).upper()
    return source[:2].upper()


def _role_name(user: dict) -> str:
    return {accounts.SUPERADMIN: "Proveedor",
            accounts.ADMIN: "Responsable",
            accounts.EMPLOYEE: "Empleado"}.get(user.get("role"), "")


def _sidebar(user: dict, current: str) -> str:
    """The navigation, showing only what this account can actually open.

    Offering an employee a tab that refuses them is worse than not offering it, so the
    permission that guards each page is the same one that decides whether it is listed.
    """
    groups = []
    for label, entries in _NAV:
        links = []
        for permission, href, icon, text in entries:
            if permission and not accounts.can(user, permission):
                continue
            active = " active" if href == current else ""
            badge = ""
            if href == "/pending":
                try:
                    waiting = len(store.list_pending())
                except Exception:
                    waiting = 0
                if waiting:
                    badge = f"<span class='count'>{waiting}</span>"
            links.append(
                f"<a class='{active.strip()}' href='{href}'>"
                f"<span class='ico'>{icon}</span>{html.escape(text)}{badge}</a>"
            )
        if links:
            groups.append(
                f"<div class='nav-group'><div class='nav-label'>{html.escape(label)}"
                f"</div>{''.join(links)}</div>"
            )

    company = user.get("company_name") or ("Panel del proveedor"
                                           if user.get("role") == accounts.SUPERADMIN
                                           else "")
    return (
        "<aside class='sidebar'>"
        "<div class='brand'><div class='brand-mark'>FA</div><div>"
        "<div class='brand-name'>Facturación</div>"
        f"<div class='brand-sub'>{html.escape(company or 'Sin empresa')}</div>"
        "</div></div>"
        f"<nav class='nav'>{''.join(groups)}</nav>"
        "<div class='whoami'>"
        f"<div class='avatar'>{html.escape(_initials(user))}</div>"
        "<div class='whoami-text'>"
        f"<div class='whoami-name'>{html.escape(user.get('name') or user.get('email',''))}</div>"
        f"<div class='whoami-sub'>{html.escape(_role_name(user))} · "
        "<a href='/logout'>salir</a></div></div></div>"
        "</aside>"
    )


def _page(title: str, body: str, user=None, subtitle: str = "",
          current: str = "", actions: str = "") -> HTMLResponse:
    """Render one page inside the shell.

    `user` may be an account dict or an email string, because a couple of callers only
    have the address to hand.
    """
    if isinstance(user, str):
        user = accounts.find_by_email(user) or {
            "email": user, "role": "", "permissions": [], "active": 1}

    head = (
        f"<div class='page-head'><div><h1>{html.escape(title)}</h1>"
        + (f"<div class='page-sub'>{subtitle}</div>" if subtitle else "")
        + "</div>"
        + (f"<div class='actions'>{actions}</div>" if actions else "")
        + "</div>"
    )

    if user:
        shell = (f"<div class='shell'>{_sidebar(user, current)}"
                 f"<main class='content'>{head}{body}</main></div>")
    else:
        # Signed out: no navigation to offer, so the message stands on its own.
        shell = (f"<div class='content' style='max-width:560px;margin:60px auto'>"
                 f"{head}{body}</div>")

    return HTMLResponse(
        "<!doctype html><html lang='es'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>{html.escape(title)} · Facturación</title>"
        f"<style>{_STYLE}</style></head><body>{shell}</body></html>"
    )


def _stat(label: str, value: str, note: str = "", tone: str = "",
          href: str = "") -> str:
    """One headline number on the dashboard."""
    inner = (f"<div class='stat-label'>{html.escape(label)}</div>"
             f"<div class='stat-value'>{value}</div>"
             + (f"<div class='stat-note {tone}'>{note}</div>" if note else ""))
    if href:
        return f"<a class='stat' href='{href}'>{inner}</a>"
    return f"<div class='stat'>{inner}</div>"


def _empty(title: str, hint: str = "") -> str:
    return (f"<div class='empty'><div class='empty-title'>{html.escape(title)}</div>"
            + (f"<div class='muted'>{hint}</div>" if hint else "") + "</div>")


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
    """The home page: the state of the business in one screen.

    Deliberately asks for no particular permission. It shows only the sections the
    account can see, so someone who only books supplier bills lands somewhere useful
    instead of on a refusal -- the old home page was the pending-invoice list, which
    told a bookkeeper nothing and refused half the staff outright.
    """
    user, refusal = _guard(request)
    if refusal:
        return refusal
    blocked = _refuse_shared_data(user)
    if blocked is not None:
        return blocked

    config = get_config()
    tiles, sections = [], []

    if accounts.can(user, "invoices.view"):
        pending = store.list_pending()
        waiting = sum(_totals(p["invoice"])[2] for p in pending)
        tiles.append(_stat(
            "Pendientes de revisar", str(len(pending)),
            _money(waiting) if pending else "nada esperando",
            "bad" if pending else "", "/pending"))

    if accounts.can(user, "receivables.view"):
        unpaid = store.list_unpaid()
        owed = sum(_totals(u["invoice"])[2] for u in unpaid)
        late = [u for u in unpaid if u["days_overdue"] > 0]
        tiles.append(_stat(
            "Pendiente de cobrar", _money(owed),
            f"{len(late)} vencida(s)" if late else f"{len(unpaid)} factura(s)",
            "bad" if late else "", "/receivables"))

    if accounts.can(user, "bills.view"):
        to_pay = bills.total_owed()
        overdue = bills.total_owed(overdue_only=True)
        tiles.append(_stat(
            "Pendiente de pagar", _money(to_pay),
            f"{_money(overdue)} ya vencido" if overdue else "nada vencido",
            "bad" if overdue else "good", "/bills"))

    if accounts.can(user, "stock.view"):
        from src import catalog

        low = catalog.low_stock()
        tiles.append(_stat(
            "Productos por reponer", str(len(low)),
            ", ".join(p["name"] for p in low[:2]) if low else "todo por encima del mínimo",
            "bad" if low else "good"))

    if config.is_placeholder:
        sections.append(
            "<div class='notice notice-warn'><b>Esta instalación no está configurada.</b>"
            "Los datos de la empresa siguen siendo los de ejemplo, así que cada factura "
            "sale marcada como <b>DOCUMENTO DE PRUEBA</b>. "
            + ("<a href='/admin'>Configúrala aquí</a>."
               if accounts.can(user, "companies.manage") else
               "Avisa a quien administra el sistema.") + "</div>")

    if accounts.can(user, "invoices.view"):
        pending = store.list_pending()
        if pending:
            sections.append("<h2>Facturas esperando tu revisión</h2>"
                            + _pending_table(pending, user))
        else:
            sections.append(
                "<h2>Facturas pendientes</h2>"
                + _empty("Nada pendiente de revisar",
                         "Cuando dictes una factura al bot aparecerá aquí."))

    return _page("Inicio", f"<div class='stats'>{''.join(tiles)}</div>"
                 + "".join(sections), user,
                 subtitle=f"{html.escape(config.name)} · "
                          f"{date.today().strftime('%d/%m/%Y')}",
                 current="/")


def _pending_table(pending, user) -> str:
    """Pending invoices as a table: scannable, and the actions line up."""
    may_approve = accounts.can(user, "invoices.approve")
    may_edit = accounts.can(user, "invoices.create")

    rows = []
    for p in pending:
        inv = p["invoice"]
        _, _, total = _totals(inv)
        email = (f"<span class='muted'>{html.escape(inv.client_email)}</span>"
                 if inv.client_email else
                 "<span class='badge badge-warn'>sin email</span>")
        buttons = [f"<a class='btn btn-neutral btn-sm' href='/invoice/{p['token']}/pdf' "
                   f"target='_blank'>PDF</a>"]
        if may_edit:
            buttons.append(f"<a class='btn btn-neutral btn-sm' "
                           f"href='/invoice/{p['token']}'>Editar</a>")
        if may_approve:
            buttons.append(
                f"<form method='post' action='/invoice/{p['token']}/approve'>"
                f"<button class='btn-primary btn-sm'>Aprobar y enviar</button></form>")
            buttons.append(
                f"<form method='post' action='/invoice/{p['token']}/reject' "
                f"onsubmit=\"return confirm('¿Descartar esta factura?')\">"
                f"<button class='btn-neutral btn-sm'>Descartar</button></form>")
        rows.append(
            f"<tr><td><div class='strong'>"
            f"{html.escape(inv.client_name or 'Sin nombre')}</div>{email}</td>"
            f"<td class='muted'>{p.get('created','')[:10]}</td>"
            f"<td class='num total'>{_money(total)}</td>"
            f"<td><div class='actions'>{''.join(buttons)}</div></td></tr>"
        )
    return ("<div class='card'><div class='table-wrap'><table><thead><tr>"
            "<th>Cliente</th><th>Creada</th><th class='num'>Total</th><th></th>"
            f"</tr></thead><tbody>{''.join(rows)}</tbody></table></div></div>")


@app.get("/pending", response_class=HTMLResponse)
async def pending_page(request: Request):
    user, refusal = _guard(request, "invoices.view")
    if refusal:
        return refusal

    pending = store.list_pending()
    body = (_pending_table(pending, user) if pending else
            _empty("No hay nada pendiente de revisar",
                   "Cuando dictes una factura al bot aparecerá aquí para que la "
                   "apruebes antes de enviarse."))
    return _page("Facturas pendientes", body, user,
                 subtitle="Revisa y aprueba antes de que salgan al cliente",
                 current="/pending")


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
        return _page("Facturas emitidas", _empty(
            "Todavía no has emitido ninguna factura",
            "Las que apruebes aparecerán aquí, con su número definitivo."),
            user, current="/issued")

    may_rectify = accounts.can(user, "invoices.rectify")
    total_issued = sum(_totals(r["invoice"])[2] for r in records)

    rows = []
    for r in records:
        inv = r["invoice"]
        _, _, total = _totals(inv)
        rectified = r.get("rectified_by")

        if rectified:
            state = (f"<span class='badge badge-muted'>anulada por "
                     f"{html.escape(rectified)}</span>")
        elif inv.rectifies:
            state = (f"<span class='badge badge-warn'>rectificativa de "
                     f"{html.escape(inv.rectifies)}</span>")
        else:
            state = "<span class='badge badge-ok'>emitida</span>"

        action = ""
        if may_rectify and not rectified and not inv.rectifies:
            action = (
                f"<form method='post' action='/invoice/"
                f"{html.escape(inv.invoice_number)}/rectify' "
                f"onsubmit=\"return confirm('¿Emitir una factura rectificativa que "
                f"anula {html.escape(inv.invoice_number)}?')\">"
                f"<button class='btn-neutral btn-sm'>Anular</button></form>")

        rows.append(
            f"<tr><td class='strong'>{html.escape(inv.invoice_number or '')}</td>"
            f"<td>{html.escape(inv.client_name or '')}</td>"
            f"<td class='muted'>{r.get('issued_at','')[:10]}</td>"
            f"<td>{state}</td>"
            f"<td class='num total'>{_money(total)}</td>"
            f"<td><div class='actions'>{action}</div></td></tr>"
        )

    body = (
        f"<div class='stats'>"
        f"{_stat('Facturas emitidas', str(len(records)))}"
        f"{_stat('Facturado en total', _money(total_issued))}"
        f"</div>"
        "<div class='card'><div class='table-wrap'><table><thead><tr>"
        "<th>Número</th><th>Cliente</th><th>Fecha</th><th>Estado</th>"
        "<th class='num'>Total</th><th></th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table></div></div>"
    )
    return _page("Facturas emitidas", body, user,
                 subtitle="Ya enviadas al cliente. Anular emite una rectificativa.",
                 current="/issued")


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

    may_manage = accounts.can(user, "bills.manage")

    tiles = (
        f"<div class='stats'>"
        f"{_stat('Pendiente de pagar', _money(owed), f'{len(unpaid)} factura(s)')}"
        f"{_stat('Ya vencido', _money(overdue), 'páguelo cuanto antes' if overdue else 'nada vencido', 'bad' if overdue else 'good')}"
        f"</div>"
    )

    form = ("<div class='card'><div class='card-title'>Anotar una factura recibida</div>"
            "<div class='card-hint'>O mándale una foto del ticket al bot y la anota "
            "él solo.</div>"
            "<form method='post' action='/bills/new' class='billform'>"
            "<input name='supplier' placeholder='Proveedor' required>"
            "<input name='total' type='number' step='0.01' "
            "placeholder='Importe con IVA' required>"
            "<input name='reference' placeholder='Su nº de factura'>"
            "<input name='due_date' type='date' title='Vencimiento'>"
            "<button class='btn-primary'>Guardar</button></form></div>"
            ) if may_manage else ""

    today = date.today().isoformat()
    rows = []
    for b in unpaid:
        due = b["due_date"] or ""
        if due and due < today:
            when = f"<span class='badge badge-danger'>venció el {due}</span>"
        elif due:
            when = f"<span class='muted'>vence el {due}</span>"
        else:
            when = "<span class='muted'>sin vencimiento</span>"

        buttons = ""
        if may_manage:
            buttons = (
                f"<form method='post' action='/bills/{b['id']}/paid'>"
                f"<button class='btn-primary btn-sm'>Marcar pagada</button></form>"
                f"<form method='post' action='/bills/{b['id']}/delete' "
                f"onsubmit=\"return confirm('¿Borrar esta factura de proveedor?')\">"
                f"<button class='btn-neutral btn-sm'>Borrar</button></form>")

        rows.append(
            f"<tr><td><div class='strong'>{html.escape(b['supplier_name'])}</div>"
            + (f"<span class='muted'>{html.escape(b['reference'])}</span>"
               if b.get("reference") else "")
            + f"</td><td>{when}</td>"
            f"<td class='num total'>{_money(b['total'])}</td>"
            f"<td><div class='actions'>{buttons}</div></td></tr>"
        )

    table = (
        "<div class='card'><div class='table-wrap'><table><thead><tr>"
        "<th>Proveedor</th><th>Vencimiento</th><th class='num'>Importe</th><th></th>"
        f"</tr></thead><tbody>{''.join(rows)}</tbody></table></div></div>"
    ) if rows else _empty("No debes nada a proveedores",
                          "Todo lo anotado está pagado.")

    return _page("Pagos a proveedores", tiles + form + table, user,
                 subtitle="Lo que tu empresa debe y cuándo vence",
                 current="/bills")


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

    overdue_total = sum(_totals(u["invoice"])[2] for u in late)
    may_manage = accounts.can(user, "receivables.manage")

    tiles = (
        f"<div class='stats'>"
        f"{_stat('Pendiente de cobrar', _money(total), f'{len(unpaid)} factura(s)')}"
        f"{_stat('Vencido', _money(overdue_total), f'{len(late)} factura(s) con retraso' if late else 'nadie te debe con retraso', 'bad' if late else 'good')}"
        f"</div>"
    )

    rows = []
    for u in unpaid:
        inv = u["invoice"]
        if u["days_overdue"] > 0:
            when = (f"<span class='badge badge-danger'>{u['days_overdue']} día(s) "
                    f"de retraso</span>")
        else:
            when = f"<span class='muted'>vence el {u['due_date']}</span>"
        button = ""
        if may_manage:
            button = (
                f"<form method='post' action='/receivables/"
                f"{html.escape(inv.invoice_number)}/paid'>"
                f"<button class='btn-primary btn-sm'>Marcar cobrada</button></form>")
        rows.append(
            f"<tr><td class='strong'>{html.escape(inv.invoice_number or '')}</td>"
            f"<td>{html.escape(inv.client_name or '')}</td>"
            f"<td>{when}</td>"
            f"<td class='num total'>{_money(_totals(inv)[2])}</td>"
            f"<td><div class='actions'>{button}</div></td></tr>"
        )

    table = (
        "<div class='card'><div class='table-wrap'><table><thead><tr>"
        "<th>Número</th><th>Cliente</th><th>Vencimiento</th>"
        "<th class='num'>Importe</th><th></th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table></div></div>"
    ) if rows else _empty("Todo cobrado", "No hay ninguna factura pendiente de cobro.")

    return _page("Cobros pendientes", tiles + table, user,
                 subtitle="Lo que te deben tus clientes",
                 current="/receivables")


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
