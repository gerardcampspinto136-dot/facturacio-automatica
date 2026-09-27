"""FastAPI web app for reviewing pending invoices.

Everyone signs in with Google. Who they are and what they may do comes from the accounts
table (see src/accounts.py), not from a list in a config file -- that is what lets a
client's owner add and remove their own staff without anyone editing YAML and restarting.

Every route states the one permission it needs via _guard(), so adding a route without
deciding who may use it is a visible omission rather than an accidental hole.

The account panels themselves live in src/web/admin.py.

Set WEB_DEV_NO_AUTH=1 to bypass Google login for local testing.
"""

import asyncio
import html
import logging
import os
from datetime import date

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

from src import accounts, bills, finalize, rectify, store
from src.totals import compute_totals, format_money as _fmt, irpf_rate, vat_rate
from src.config_loader import get_config
from src.invoice_generator import generate_invoice_pdf
from src.models import InvoiceData, InvoiceItem

logger = logging.getLogger(__name__)

app = FastAPI(title="Revisión de facturas")


# The values shipped in .env.example and old code: anyone can read them, so a session
# signed with one could be forged -- a login as anybody.
_KNOWN_SECRETS = {"", "change-me-to-a-long-random-string", "dev-insecure-secret-change-me"}
SECRET_FILE = "data/.session_secret"


def _session_secret() -> str:
    """The key that signs the login cookie: .env's, else one made for this machine."""
    import secrets
    from pathlib import Path

    configured = (os.getenv("SESSION_SECRET") or "").strip()
    if configured not in _KNOWN_SECRETS:
        return configured
    path = Path(SECRET_FILE)
    try:
        if path.exists() and path.read_text(encoding="utf-8").strip():
            return path.read_text(encoding="utf-8").strip()
        path.parent.mkdir(parents=True, exist_ok=True)
        secret = secrets.token_hex(32)
        path.write_text(secret, encoding="utf-8")
        return secret
    except OSError:
        # Unwritable: still secret, but sessions will not survive a restart.
        return secrets.token_hex(32)


def _install_middleware() -> None:
    from starlette.middleware.sessions import SessionMiddleware

    app.add_middleware(SessionMiddleware, secret_key=_session_secret())


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
        ("invoices.view", "/quotes", "✎", "Presupuestos"),
        ("invoices.view", "/recurring", "↻", "Recurrentes"),
        ("receivables.view", "/receivables", "↓", "Cobros"),
    )),
    ("Gastos", (
        ("bills.view", "/bills", "↑", "Proveedores"),
    )),
    ("Hacienda", (
        ("taxes.view", "/taxes", "§", "Impuestos"),
        ("invoices.view", "/verifactu", "▣", "Verifactu"),
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
        "<a href='/me'>mi cuenta</a> · "
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


def _es_date(iso: str) -> str:
    """2026-10-02 -> 02/10/2026, the way dates are written everywhere else here."""
    try:
        return date.fromisoformat(iso).strftime("%d/%m/%Y")
    except (TypeError, ValueError):
        return iso or ""


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
        f"{''.join(item_rows)}"
        # One empty row, so a line forgotten in the dictation can be added here.
        f"<tr><td><input name='item_desc' placeholder='Añadir un concepto'></td>"
        f"<td><input name='item_qty' value='1' style='width:80px'></td>"
        f"<td><input name='item_price' style='width:100px'></td></tr></table>"
        f"<div class='grid-2'>"
        f"<div><label>IVA (%)</label><input name='tax_rate' "
        f"value='{vat_rate(inv):g}'></div>"
        f"<div><label>Retención IRPF (%) <span class='field-hint'>0 si no lleva</span>"
        f"</label><input name='irpf_rate' value='{irpf_rate(inv):g}'></div></div>"
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
    for field, top in (("tax_rate", 30), ("irpf_rate", 50)):
        raw = str(form.get(field, "")).strip().replace(",", ".")
        if raw:
            try:
                value = float(raw)
            except ValueError:
                continue
            if 0 <= value <= top:
                setattr(inv, field, value)

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


def _person(user) -> str:
    return (user or {}).get("name") or (user or {}).get("email") or "Un responsable"


@app.post("/invoice/{token}/approve")
async def approve(request: Request, token: str):
    user, refusal = _guard(request, "invoices.approve")
    if refusal:
        return refusal
    p = store.get_pending(token)
    if not p:
        return RedirectResponse("/", status_code=303)
    inv: InvoiceData = p["invoice"]
    inv.invoice_number = None  # force a fresh gap-free number on finalize
    try:
        result = await asyncio.to_thread(finalize.issue, inv, token)
    except KeyError:
        # Approved from Telegram, or by a colleague, a moment earlier.
        return RedirectResponse("/", status_code=303)

    from src import notify

    await asyncio.to_thread(
        notify.tell_creator, p,
        f"✅ {_person(user)} ha aprobado tu factura para {inv.client_name}: "
        f"{result.number}.\n{result.email_status}")
    return RedirectResponse("/issued", status_code=303)


@app.post("/invoice/{token}/reject")
async def reject(request: Request, token: str):
    user, refusal = _guard(request, "invoices.approve")
    if refusal:
        return refusal
    p = store.get_pending(token)
    store.remove_pending(token)
    if p:
        from src import notify

        await asyncio.to_thread(
            notify.tell_creator, p,
            f"❌ {_person(user)} ha descartado tu factura para "
            f"{p['invoice'].client_name}. No se ha enviado nada.")
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
    may_send = accounts.can(user, "invoices.approve")
    # Turnover is the taxable base: what the business earned, before the VAT it only
    # collects for Hacienda and the IRPF its clients keep back. Rectifying invoices
    # count negatively, so a cancelled sale is not counted as earned.
    this_year = str(date.today().year)
    turnover = sum(_totals(r["invoice"])[0] for r in records
                   if r["invoice"].date.isoformat().startswith(this_year))

    rows = []
    for r in records:
        inv = r["invoice"]
        _, _, total = _totals(inv)
        rectified = r.get("rectified_by")
        number = html.escape(inv.invoice_number or "")

        if rectified:
            state = (f"<span class='badge badge-muted'>anulada por "
                     f"{html.escape(rectified)}</span>")
        elif inv.rectifies:
            state = (f"<span class='badge badge-warn'>rectificativa de "
                     f"{html.escape(inv.rectifies)}</span>")
        else:
            state = "<span class='badge badge-ok'>emitida</span>"
        # The email is the one part of issuing that can fail on its own; say so here
        # rather than let a client wait for an invoice that never left.
        if r.get("email_error") and not r.get("email_sent_at"):
            state += (f" <span class='badge badge-danger' "
                      f"title='{html.escape(r['email_error'])}'>email no enviado</span>")

        actions = [f"<a class='btn btn-neutral btn-sm' href='/issued/{number}/pdf' "
                   f"target='_blank'>PDF</a>"]
        if may_send and inv.client_email:
            label = "Reintentar envío" if r.get("email_error") else "Reenviar"
            actions.append(
                f"<form method='post' action='/issued/{number}/resend' "
                f"onsubmit=\"return confirm('¿Enviar otra vez la factura {number} a "
                f"{html.escape(inv.client_email)}?')\">"
                f"<button class='btn-neutral btn-sm'>{label}</button></form>")
        if may_rectify and not rectified and not inv.rectifies:
            actions.append(
                f"<form method='post' action='/invoice/{number}/rectify' "
                f"onsubmit=\"var m = prompt('Se emitirá una factura rectificativa que "
                f"anula la {number}. Motivo:', 'Anulación de la factura'); "
                f"if (m === null) return false; this.reason.value = m; return true;\">"
                f"<input type='hidden' name='reason'>"
                f"<button class='btn-neutral btn-sm'>Anular</button></form>")

        rows.append(
            f"<tr><td class='strong'>{number}</td>"
            f"<td>{html.escape(inv.client_name or '')}</td>"
            f"<td class='muted'>{inv.date.strftime('%d/%m/%Y')}</td>"
            f"<td>{state}</td>"
            f"<td class='num total'>{_money(total)}</td>"
            f"<td><div class='actions'>{''.join(actions)}</div></td></tr>"
        )

    body = (
        f"<div class='stats'>"
        f"{_stat('Facturas emitidas', str(len(records)))}"
        f"{_stat(f'Facturado en {this_year} (sin IVA)', _money(turnover))}"
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
    form = await request.form()
    try:
        await asyncio.to_thread(rectify.rectify, number,
                                (form.get("reason") or "").strip() or None)
    except ValueError as exc:
        return _page("No se pudo anular", f"<div class='card'>{html.escape(str(exc))}</div>", user)
    return RedirectResponse("/issued", status_code=303)


@app.get("/issued/{number}/pdf")
async def issued_pdf(request: Request, number: str):
    user, refusal = _guard(request, "invoices.view")
    if refusal:
        return refusal
    path = await asyncio.to_thread(finalize.pdf_for, number)
    if path is None:
        return _page("No encontrada", "<div class='card'>Esa factura no existe.</div>", user)
    return FileResponse(path, media_type="application/pdf",
                        filename=f"Factura_{number}.pdf")


@app.post("/issued/{number}/resend")
async def issued_resend(request: Request, number: str):
    user, refusal = _guard(request, "invoices.approve")
    if refusal:
        return refusal
    sent, error = await asyncio.to_thread(finalize.resend, number)
    if not sent:
        return _page("No se pudo enviar",
                     f"<div class='card'>{html.escape(error or 'Error desconocido')}"
                     "<div class='actions'><a class='btn btn-neutral' href='/issued'>"
                     "Volver</a></div></div>", user)
    return RedirectResponse("/issued", status_code=303)


# ── Quotes ───────────────────────────────────────────────────────────────────

@app.get("/quotes", response_class=HTMLResponse)
async def quotes_page(request: Request):
    from src import quotes

    user, refusal = _guard(request, "invoices.view")
    if refusal:
        return refusal
    may_convert = accounts.can(user, "invoices.create")
    listed = quotes.list_quotes(limit=200)

    badge = {"enviado": "", "aceptado": "badge-ok", "facturado": "badge-ok",
             "rechazado": "badge-muted", "caducado": "badge-warn"}
    rows = []
    for q in listed:
        inv = q["invoice"]
        state = quotes.status_label(q)
        number = html.escape(q["number"])
        actions = [f"<a class='btn btn-neutral btn-sm' href='/quotes/{number}/pdf' "
                   f"target='_blank'>PDF</a>"]
        if may_convert and q["status"] in ("sent", "accepted"):
            actions.append(
                f"<form method='post' action='/quotes/{number}/invoice'>"
                f"<button class='btn-primary btn-sm'>Aceptado → facturar</button></form>")
            actions.append(
                f"<form method='post' action='/quotes/{number}/reject'>"
                f"<button class='btn-neutral btn-sm'>Rechazado</button></form>")
        invoiced = (f"<div class='muted'>factura {html.escape(q['invoice_number'])}</div>"
                    if q["invoice_number"] else "")
        rows.append(
            f"<tr><td class='strong'>{number}</td>"
            f"<td>{html.escape(inv.client_name)}{invoiced}</td>"
            f"<td class='muted'>{inv.date.strftime('%d/%m/%Y')}</td>"
            f"<td class='muted'>{q['valid_until'].strftime('%d/%m/%Y')}</td>"
            f"<td><span class='badge {badge.get(state, '')}'>{state}</span></td>"
            f"<td class='num total'>{_money(_totals(inv)[2])}</td>"
            f"<td><div class='actions'>{''.join(actions)}</div></td></tr>")

    open_total = sum(_totals(q["invoice"])[2] for q in listed
                     if q["status"] in ("sent", "accepted") and not q["expired"])
    tiles = (f"<div class='stats'>"
             f"{_stat('Presupuestos abiertos', _money(open_total), 'esperando respuesta')}"
             f"</div>")
    table = ("<div class='card'><div class='table-wrap'><table><thead><tr>"
             "<th>Número</th><th>Cliente</th><th>Fecha</th><th>Válido hasta</th>"
             "<th>Estado</th><th class='num'>Total</th><th></th></tr></thead>"
             f"<tbody>{''.join(rows)}</tbody></table></div></div>"
             if rows else _empty("Todavía no hay presupuestos",
                                 "Díctale al bot «presupuesto para…» como si fuera una "
                                 "factura."))
    return _page("Presupuestos", tiles + table, user,
                 subtitle="Cuando el cliente acepta, se convierte en la factura",
                 current="/quotes")


@app.get("/quotes/{number}/pdf")
async def quote_pdf(request: Request, number: str):
    from src import quotes

    user, refusal = _guard(request, "invoices.view")
    if refusal:
        return refusal
    path = await asyncio.to_thread(quotes.pdf_for, number)
    if path is None:
        return _page("No encontrado", "<div class='card'>Ese presupuesto no existe.</div>",
                     user)
    return FileResponse(path, media_type="application/pdf",
                        filename=f"Presupuesto_{number}.pdf")


@app.post("/quotes/{number}/invoice")
async def quote_to_invoice(request: Request, number: str):
    """Accepted: the quote becomes a draft invoice, to review and approve as usual."""
    from src import quotes

    user, refusal = _guard(request, "invoices.create")
    if refusal:
        return refusal
    try:
        invoice = quotes.to_invoice(number)
    except (KeyError, ValueError) as exc:
        return _page("No se pudo facturar",
                     f"<div class='card'>{html.escape(str(exc))}</div>", user)
    token = store.new_token()
    draft = finalize.draft_path(token)
    await asyncio.to_thread(generate_invoice_pdf, invoice, draft)
    store.add_pending(invoice, draft, token=token, created_by=user.get("id"),
                      created_by_name=_person(user))
    return RedirectResponse(f"/invoice/{token}", status_code=303)


@app.post("/quotes/{number}/reject")
async def quote_reject(request: Request, number: str):
    from src import quotes

    _user_, refusal = _guard(request, "invoices.create")
    if refusal:
        return refusal
    quotes.reject(number)
    return RedirectResponse("/quotes", status_code=303)


# ── Recurring invoices ───────────────────────────────────────────────────────

@app.get("/recurring", response_class=HTMLResponse)
async def recurring_page(request: Request):
    from src import recurring

    user, refusal = _guard(request, "invoices.view")
    if refusal:
        return refusal
    may_manage = accounts.can(user, "invoices.approve")
    templates = recurring.list_active()

    rows = []
    for t in templates:
        invoice = recurring.invoice_for(t, date.today())
        how = ("<span class='badge badge-warn'>se envía sola</span>" if t["auto_send"]
               else "<span class='badge'>se prepara para aprobar</span>")
        actions = ""
        if may_manage:
            flip = "manual" if t["auto_send"] else "auto"
            actions = (
                f"<form method='post' action='/recurring/{t['id']}/{flip}'>"
                f"<button class='btn-neutral btn-sm'>"
                f"{'Prepararla para aprobar' if t['auto_send'] else 'Que se envíe sola'}"
                f"</button></form>"
                f"<form method='post' action='/recurring/{t['id']}/cancel' "
                f"onsubmit=\"return confirm('¿Cancelar esta factura recurrente?')\">"
                f"<button class='btn-neutral btn-sm'>Cancelar</button></form>")
        rows.append(
            f"<tr><td class='strong'>{html.escape(t['client_name'])}"
            f"<div class='muted'>{html.escape(recurring.FREQUENCIES[t['frequency']][0])}"
            f" · desde {html.escape(t['source_number'] or '')}</div></td>"
            f"<td class='muted'>{date.fromisoformat(t['next_date']).strftime('%d/%m/%Y')}"
            f"</td><td>{how}</td>"
            f"<td class='num total'>{_money(_totals(invoice)[2])}</td>"
            f"<td><div class='actions'>{actions}</div></td></tr>")

    table = ("<div class='card'><div class='table-wrap'><table><thead><tr>"
             "<th>Cliente</th><th>Próxima</th><th>Cómo</th><th class='num'>Importe</th>"
             f"<th></th></tr></thead><tbody>{''.join(rows)}</tbody></table></div></div>"
             if rows else _empty("No hay facturas recurrentes",
                                  "Emite la factura una vez y pulsa «Repetir cada mes» "
                                  "debajo de ella en Telegram, o créala aquí."))

    form = ""
    if may_manage:
        form = (
            "<div class='card'><div class='card-title'>Repetir una factura</div>"
            "<div class='card-hint'>Cuotas de mantenimiento, alquileres, igualas: se "
            "emite una vez y se repite sola.</div>"
            "<form method='post' action='/recurring/new' class='billform'>"
            "<input name='number' placeholder='Nº de factura, p. ej. 2026-0007' required>"
            "<select name='frequency' style='width:auto'>"
            "<option value='monthly'>cada mes</option>"
            "<option value='quarterly'>cada trimestre</option>"
            "<option value='yearly'>cada año</option></select>"
            "<button class='btn-primary'>Crear</button></form></div>")

    return _page("Facturas recurrentes", form + table, user,
                 subtitle="Las que se repiten solas: te las preparo o se envían",
                 current="/recurring")


@app.post("/recurring/new")
async def recurring_new(request: Request):
    from src import recurring

    user, refusal = _guard(request, "invoices.approve")
    if refusal:
        return refusal
    form = await request.form()
    number = (form.get("number") or "").strip()
    frequency = form.get("frequency") or recurring.MONTHLY
    record = store.get_issued(number)
    if record is None or record["invoice"].rectifies or frequency not in recurring.FREQUENCIES:
        return _page("No se pudo crear",
                     f"<div class='card'>No encuentro la factura «{html.escape(number)}»."
                     "<div class='actions'><a class='btn btn-neutral' href='/recurring'>"
                     "Volver</a></div></div>", user)
    recurring.create_from_invoice(record["invoice"], frequency, created_by=user.get("id"))
    return RedirectResponse("/recurring", status_code=303)


@app.post("/recurring/{template_id}/{action}")
async def recurring_action(request: Request, template_id: int, action: str):
    from src import recurring

    _user_, refusal = _guard(request, "invoices.approve")
    if refusal:
        return refusal
    if action == "cancel":
        recurring.cancel(template_id)
    elif action in ("auto", "manual"):
        recurring.set_auto_send(template_id, action == "auto")
    return RedirectResponse("/recurring", status_code=303)


# ── Taxes and the gestor ─────────────────────────────────────────────────────

def _recent_quarters(count: int = 6) -> list[tuple[int, int]]:
    from src import taxes

    year, quarter = taxes.quarter_of(date.today())
    out = []
    for _ in range(count):
        out.append((year, quarter))
        year, quarter = (year - 1, 4) if quarter == 1 else (year, quarter - 1)
    return out


@app.get("/taxes", response_class=HTMLResponse)
async def taxes_page(request: Request, y: int = 0, q: int = 0):
    """The quarter's 303 and 130, and the pack for the gestor."""
    from src import gestor_pack, taxes

    user, refusal = _guard(request, "taxes.view")
    if refusal:
        return refusal
    config = get_config()

    if not (y and 1 <= q <= 4):
        y, q = taxes.quarter_to_file() or taxes.quarter_of(date.today())

    tabs = "".join(
        f"<a class='btn btn-sm {'btn-primary' if (yy, qq) == (y, q) else 'btn-neutral'}' "
        f"href='/taxes?y={yy}&q={qq}'>{taxes.label(yy, qq)}"
        f"{' (en curso)' if (yy, qq) == taxes.quarter_of(date.today()) else ''}</a>"
        for yy, qq in _recent_quarters()
    )

    vat = await asyncio.to_thread(taxes.vat_return, y, q)
    company = taxes.is_company(config.cif)
    irpf = None if company else await asyncio.to_thread(taxes.irpf_instalment, y, q)

    tiles = [_stat("IVA a ingresar" if vat.result > 0 else "IVA a compensar",
                   _money(abs(vat.result)),
                   f"repercutido {_money(vat.output_tax)} · soportado {_money(vat.input_tax)}",
                   "bad" if vat.result > 0 else "good")]
    if irpf is not None:
        tiles.append(_stat("IRPF a ingresar (130)", _money(irpf.to_pay),
                           f"rendimiento {_money(irpf.net)} desde enero"))
    window_start, window_end = taxes.filing_window(y, q)
    tiles.append(_stat("Fecha límite", window_end.strftime("%d/%m/%Y"),
                       f"se presenta desde el {window_start.strftime('%d/%m')}"))

    rate_rows = "".join(
        f"<tr><td>{taxes.rate_name(rate)}</td><td class='num'>{_money(base)}</td>"
        f"<td class='num'>{_money(tax)}</td></tr>"
        for rate, (base, tax) in sorted(vat.by_rate.items(), reverse=True)
    ) or "<tr><td colspan='3' class='muted'>Sin facturas emitidas en el trimestre</td></tr>"
    vat_card = (
        "<div class='card'><div class='card-title'>Modelo 303 — IVA</div>"
        "<div class='table-wrap'><table><thead><tr><th></th><th class='num'>Base</th>"
        "<th class='num'>Cuota</th></tr></thead><tbody>" + rate_rows +
        f"<tr><td class='strong'>IVA deducible ({vat.bills} gasto(s))</td>"
        f"<td class='num'>{_money(vat.input_base)}</td>"
        f"<td class='num'>−{_money(vat.input_tax)}</td></tr>"
        f"<tr><td class='strong'>Resultado</td><td></td>"
        f"<td class='num total'>{_money(vat.result)}</td></tr></tbody></table></div>"
        + (f"<div class='notice notice-warn' style='margin-top:12px'>"
           f"<b>{vat.bills_without_vat} gasto(s) sin el IVA desglosado</b>Si lo llevaban, "
           "ese IVA no se está deduciendo. Mándale la foto del ticket al bot.</div>"
           if vat.bills_without_vat else "")
        + "</div>")

    if irpf is None:
        irpf_card = ("<div class='card'><div class='card-title'>Modelo 130</div>"
                     "<p class='muted'>Como sociedad no presentas el 130: los pagos a "
                     "cuenta son el modelo 202, sobre el beneficio.</p></div>")
    else:
        boxes = (("[01] Ingresos", irpf.income), ("[02] Gastos", irpf.expenses),
                 ("[03] Rendimiento neto", irpf.net), ("[04] 20 %", irpf.twenty_percent),
                 ("[05] Pagado en trimestres anteriores", irpf.previous_payments),
                 ("[06] Retenciones que te han hecho", irpf.withheld),
                 ("[07] Resultado", irpf.result))
        irpf_card = (
            "<div class='card'><div class='card-title'>Modelo 130 — IRPF "
            "<span class='muted'>(acumulado desde enero)</span></div>"
            "<div class='table-wrap'><table><tbody>"
            + "".join(f"<tr><td>{html.escape(label)}</td>"
                      f"<td class='num'>{_money(value)}</td></tr>"
                      for label, value in boxes)
            + "</tbody></table></div></div>")

    sent = gestor_pack.sent_on(y, q)
    gestor = getattr(config, "gestor_email", "")
    send_button = (
        f"<form method='post' action='/taxes/{y}/{q}/send' onsubmit=\"return confirm("
        f"'¿Enviar la documentación del {taxes.label(y, q)} a {html.escape(gestor)}?')\">"
        f"<button class='btn-primary'>📧 Enviar al gestor</button></form>"
        if gestor else
        "<span class='muted'>Pon el email del gestor en la configuración de la empresa "
        "para enviárselo con un clic.</span>")
    pack_card = (
        "<div class='card'><div class='card-title'>Paquete para el gestor</div>"
        "<div class='card-hint'>Un ZIP con los libros registro de facturas emitidas y "
        "recibidas en Excel, el PDF de cada factura y la foto de cada ticket.</div>"
        + (f"<div class='notice notice-ok'><b>Enviado</b>{html.escape(sent)}</div>"
           if sent else "")
        + "<div class='actions'>"
        f"<a class='btn btn-neutral' href='/taxes/{y}/{q}/pack'>📦 Descargar ZIP</a>"
        f"{send_button}</div></div>")

    body = (f"<div class='actions' style='margin-bottom:16px'>{tabs}</div>"
            f"<div class='stats'>{''.join(tiles)}</div>"
            f"{vat_card}{irpf_card}{pack_card}"
            "<p class='muted'>Cálculo orientativo con lo registrado en el programa: tu "
            "gestor lo revisa y lo presenta.</p>")
    return _page(f"Impuestos del {taxes.label(y, q)}", body, user,
                 subtitle=f"Plazo de presentación: {taxes.deadline_text(y, q)}",
                 current="/taxes")


@app.get("/taxes/{year}/{quarter}/pack")
async def taxes_pack(request: Request, year: int, quarter: int):
    from src import gestor_pack

    user, refusal = _guard(request, "taxes.view")
    if refusal:
        return refusal
    if not 1 <= quarter <= 4:
        return RedirectResponse("/taxes", status_code=303)
    path = await asyncio.to_thread(gestor_pack.build_pack, year, quarter)
    return FileResponse(path, media_type="application/zip", filename=path.name)


@app.post("/taxes/{year}/{quarter}/send")
async def taxes_send(request: Request, year: int, quarter: int):
    from src import gestor_pack

    user, refusal = _guard(request, "taxes.view")
    if refusal:
        return refusal
    sent, message = await asyncio.to_thread(gestor_pack.email_to_gestor, year, quarter)
    if not sent:
        return _page("No se ha enviado",
                     f"<div class='card'>{html.escape(message)}<div class='actions'>"
                     f"<a class='btn btn-neutral' href='/taxes?y={year}&q={quarter}'>"
                     "Volver</a></div></div>", user)
    return RedirectResponse(f"/taxes?y={year}&q={quarter}", status_code=303)


# ── Verifactu ────────────────────────────────────────────────────────────────

@app.get("/verifactu", response_class=HTMLResponse)
async def verifactu_page(request: Request):
    """The register Hacienda will ask about: every record, and whether the chain holds."""
    from src import verifactu

    user, refusal = _guard(request, "invoices.view")
    if refusal:
        return refusal

    intact, problems = await asyncio.to_thread(verifactu.verify_chain)
    records = verifactu.list_records(200)

    if not records:
        status = ("<div class='notice'><b>Todavía no hay registros.</b>Cada factura que "
                  "se emita a partir de ahora queda registrada aquí, encadenada con la "
                  "anterior.</div>")
    elif intact:
        status = ("<div class='notice notice-ok'><b>✅ Registro íntegro.</b>"
                  f"Las {len(records)} huellas se han recalculado ahora mismo y cada una "
                  "corresponde a su factura y enlaza con la anterior: no se ha modificado "
                  "ni borrado nada.</div>")
    else:
        items = "".join(f"<li>{html.escape(p)}</li>" for p in problems[:20])
        status = ("<div class='notice notice-danger'><b>⚠️ El registro NO está íntegro."
                  "</b><ul style='margin:6px 0 0 18px;padding:0'>" + items + "</ul></div>")

    mode = ("<div class='card'><div class='card-title'>Estado</div>"
            "<p>Cada factura se registra al emitirse con su huella SHA-256 encadenada, "
            "calculada según la especificación técnica de la AEAT, y lleva el código QR "
            "tributario arriba del todo. Las facturas y sus registros no se pueden "
            "modificar ni borrar.</p>"
            "<p class='muted'>Pendiente: el envío automático de los registros a la AEAT "
            "(modalidad VERI*FACTU), que necesita el certificado digital de la empresa. "
            "Obligatorio desde el 1-1-2027 para sociedades y el 1-7-2027 para "
            "autónomos.</p></div>")

    rows = "".join(
        f"<tr><td class='strong'>{html.escape(r['invoice_number'])}</td>"
        f"<td>{html.escape(r['invoice_type'] or r['kind'])}</td>"
        f"<td class='muted'>{html.escape(r['issue_date'])}</td>"
        f"<td class='num'>{html.escape(r['amount_total'] or '')}</td>"
        f"<td class='muted' style='font-family:monospace;font-size:12px'>"
        f"{html.escape(r['hash'][:16])}…</td>"
        f"<td class='muted'>{html.escape(r['generated_at'])}</td></tr>"
        for r in records
    )
    table = ("<div class='card'><div class='table-wrap'><table><thead><tr>"
             "<th>Factura</th><th>Tipo</th><th>Fecha</th><th class='num'>Importe</th>"
             "<th>Huella</th><th>Registrada</th></tr></thead>"
             f"<tbody>{rows}</tbody></table></div></div>") if records else ""

    return _page("Verifactu", status + mode + table, user,
                 subtitle="El registro de facturación que exige Hacienda",
                 current="/verifactu")


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
            when = f"<span class='badge badge-danger'>venció el {_es_date(due)}</span>"
        elif due:
            when = f"<span class='muted'>vence el {_es_date(due)}</span>"
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
        number = html.escape(inv.invoice_number or "")
        if u["days_overdue"] > 0:
            when = (f"<span class='badge badge-danger'>{u['days_overdue']} día(s) "
                    f"de retraso</span>")
        else:
            when = f"<span class='muted'>vence el {_es_date(u['due_date'])}</span>"

        chasing = ""
        if u["reminder_count"]:
            last = (u["last_reminder_at"] or "")[:10]
            chasing = (f"<div class='muted'>recordada {u['reminder_count']} vez/veces"
                       f"{f' · última el {last}' if last else ''}</div>")
        if u["reminders_paused"]:
            chasing += "<div class='muted'>⏸ sin recordatorios</div>"

        buttons = []
        if may_manage:
            buttons.append(
                f"<form method='post' action='/receivables/{number}/paid'>"
                f"<button class='btn-primary btn-sm'>Marcar cobrada</button></form>")
            if inv.client_email and u["days_overdue"] > 0:
                buttons.append(
                    f"<form method='post' action='/receivables/{number}/remind' "
                    f"onsubmit=\"return confirm('¿Mandar a {html.escape(inv.client_email)}"
                    f" un recordatorio de pago con la factura adjunta?')\">"
                    f"<button class='btn-neutral btn-sm'>Recordar</button></form>")
            toggle = "resume" if u["reminders_paused"] else "pause"
            buttons.append(
                f"<form method='post' action='/receivables/{number}/{toggle}'>"
                f"<button class='btn-neutral btn-sm'>"
                f"{'Reanudar recordatorios' if toggle == 'resume' else 'No insistir'}"
                f"</button></form>")
        rows.append(
            f"<tr><td class='strong'>{number}</td>"
            f"<td>{html.escape(inv.client_name or '')}{chasing}</td>"
            f"<td>{when}</td>"
            f"<td class='num total'>{_money(_totals(inv)[2])}</td>"
            f"<td><div class='actions'>{''.join(buttons)}</div></td></tr>"
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


@app.post("/receivables/{number}/remind")
async def receivables_remind(request: Request, number: str):
    from src import payment_reminders

    user, refusal = _guard(request, "receivables.manage")
    if refusal:
        return refusal
    sent, message = await asyncio.to_thread(payment_reminders.send, number)
    if not sent:
        return _page("No se ha enviado",
                     f"<div class='card'>{html.escape(message)}<div class='actions'>"
                     "<a class='btn btn-neutral' href='/receivables'>Volver</a></div></div>",
                     user)
    return RedirectResponse("/receivables", status_code=303)


@app.post("/receivables/{number}/pause")
async def receivables_pause(request: Request, number: str):
    from src import payment_reminders

    _user_, refusal = _guard(request, "receivables.manage")
    if refusal:
        return refusal
    payment_reminders.pause(number)
    return RedirectResponse("/receivables", status_code=303)


@app.post("/receivables/{number}/resume")
async def receivables_resume(request: Request, number: str):
    from src import payment_reminders

    _user_, refusal = _guard(request, "receivables.manage")
    if refusal:
        return refusal
    payment_reminders.resume(number)
    return RedirectResponse("/receivables", status_code=303)


# ── Account management ───────────────────────────────────────────────────────
# The team and vendor panels live in their own module; importing it registers its
# routes on `app`. Kept separate because managing who may do what has nothing to do
# with reviewing invoices, and this file is long enough already.

from src.web import admin as _admin  # noqa: E402,F401  (imported for its routes)
