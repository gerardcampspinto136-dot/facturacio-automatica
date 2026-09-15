"""Companies, accounts and who is allowed to do what.

The software is sold to companies, so there are three kinds of account and they are not
the same shape:

  superadmin  the vendor. Creates a client company and its first owner account, and can
              suspend one. Belongs to no company.
  admin       the client's owner. Everything inside their own company, including giving
              their staff accounts and taking them away again.
  employee    only what they have been granted, one permission at a time -- so the
              person who books supplier bills need not also be able to issue invoices.

Permissions are stored as a newline-separated list of keys on the user row rather than
in a join table. There are a couple of dozen of them and they are always read all at
once for the logged-in user, so a table would add joins and migrations for nothing.

Nothing here knows about HTTP. `can()` takes a user dict and a permission key and
returns a bool, which is what makes the rules testable without a browser.
"""

import logging
import os
from typing import Optional

from src import db

logger = logging.getLogger(__name__)

SUPERADMIN = "superadmin"
ADMIN = "admin"
EMPLOYEE = "employee"
ROLES = (SUPERADMIN, ADMIN, EMPLOYEE)

ACTIVE = "active"
SUSPENDED = "suspended"


# ── What there is to permit ──────────────────────────────────────────────────

# key -> (group, label shown in the admin panel)
# Labels are Spanish because the people ticking these boxes are the client's staff.
PERMISSIONS: dict[str, tuple[str, str]] = {
    "invoices.view":      ("Facturas", "Ver las facturas"),
    "invoices.create":    ("Facturas", "Crear facturas (bot y web)"),
    "invoices.approve":   ("Facturas", "Aprobar y enviar facturas"),
    "invoices.rectify":   ("Facturas", "Anular con una rectificativa"),
    "receivables.view":   ("Cobros", "Ver lo pendiente de cobrar"),
    "receivables.manage": ("Cobros", "Marcar facturas como cobradas"),
    "bills.view":         ("Proveedores", "Ver las facturas de proveedor"),
    "bills.manage":       ("Proveedores", "Anotar, pagar y borrar gastos"),
    "stock.view":         ("Stock", "Ver el stock y el catálogo"),
    "stock.manage":       ("Stock", "Dar de alta productos y mover stock"),
    "contacts.view":      ("Clientes", "Ver clientes y proveedores"),
    "contacts.manage":    ("Clientes", "Crear y editar clientes"),
    "users.manage":       ("Administración", "Gestionar las cuentas del equipo"),
    "settings.manage":    ("Administración", "Cambiar la configuración de la empresa"),
}

# A sensible starting point when the owner adds someone, so the common case is one
# click rather than fourteen. Deliberately read-mostly: granting is a decision, and
# nobody should get approval rights by default.
DEFAULT_EMPLOYEE_PERMISSIONS = (
    "invoices.view", "invoices.create",
    "receivables.view", "bills.view", "stock.view", "stock.manage",
    "contacts.view",
)

# An owner is never limited by the permission list -- the checkboxes do not apply to
# them -- but this is what they effectively hold.
_ADMIN_PERMISSIONS = frozenset(PERMISSIONS)

# Only the vendor may do these, whatever else a company owner is granted.
VENDOR_ONLY = frozenset({"companies.manage"})


def permission_groups() -> dict[str, list[tuple[str, str]]]:
    """Permissions arranged by heading, for rendering the checkbox list."""
    grouped: dict[str, list[tuple[str, str]]] = {}
    for key, (group, label) in PERMISSIONS.items():
        grouped.setdefault(group, []).append((key, label))
    return grouped


# ── Reading and writing users ────────────────────────────────────────────────

def _row(row) -> Optional[dict]:
    if row is None:
        return None
    user = dict(row)
    user["permissions"] = _split(user.get("permissions"))
    return user


def _split(raw) -> list[str]:
    return [line.strip() for line in (raw or "").splitlines() if line.strip()]


def _clean_permissions(keys) -> str:
    """Keep only keys we actually define, in a stable order, one per line.

    An unknown key is dropped rather than stored: permissions arrive from a form, and a
    string nothing ever checks is worse than no string at all -- it reads like access
    that was granted when it does nothing.
    """
    wanted = {k for k in (keys or []) if k in PERMISSIONS}
    return "\n".join(k for k in PERMISSIONS if k in wanted)


def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def create_company(name: str, tax_id: Optional[str] = None,
                   contact_email: Optional[str] = None,
                   notes: Optional[str] = None) -> int:
    if not name or not name.strip():
        raise ValueError("La empresa necesita un nombre")
    with db.transaction() as conn:
        cur = conn.execute(
            "INSERT INTO companies (name, tax_id, contact_email, notes) "
            "VALUES (?, ?, ?, ?)",
            (name.strip(), (tax_id or "").strip() or None,
             normalize_email(contact_email) or None, notes),
        )
        return cur.lastrowid


def get_company(company_id: int) -> Optional[dict]:
    row = db.connect().execute(
        "SELECT * FROM companies WHERE id = ?", (company_id,)
    ).fetchone()
    return dict(row) if row else None


def list_companies(include_suspended: bool = True) -> list[dict]:
    sql = "SELECT * FROM companies"
    if not include_suspended:
        sql += f" WHERE status = '{ACTIVE}'"
    sql += " ORDER BY name COLLATE NOCASE"
    return [dict(r) for r in db.connect().execute(sql).fetchall()]


# What the admin panel can set on a client company. Everything an invoice needs to be
# legally complete, plus the client's own Telegram bot, so preparing a client is one
# form rather than a hand-edited YAML file on their machine.
SETTINGS_FIELDS = (
    "name", "tax_id", "address", "phone", "invoice_email", "iban",
    "tax_rate", "payment_terms", "prices_include_tax", "invoice_series",
    "review_mode", "telegram_bot_token", "telegram_chat_id", "logo_path",
    "contact_email", "notes",
)

# Without these an invoice is not a valid Spanish invoice, so they gate "configured".
REQUIRED_SETTINGS = ("name", "tax_id", "address")


def update_company(company_id: int, **fields) -> None:
    """Change a client's settings. Unknown keys are ignored, blanks clear a field."""
    changes = {k: v for k, v in fields.items() if k in SETTINGS_FIELDS}
    if "name" in changes:
        if not (changes["name"] or "").strip():
            raise ValueError("La empresa necesita un nombre")
        changes["name"] = changes["name"].strip()
    for key in ("tax_id", "address", "phone", "iban", "payment_terms",
                "invoice_series", "review_mode", "telegram_bot_token",
                "telegram_chat_id", "logo_path", "notes"):
        if key in changes and isinstance(changes[key], str):
            changes[key] = changes[key].strip() or None
    for key in ("invoice_email", "contact_email"):
        if key in changes:
            changes[key] = normalize_email(changes[key]) or None
    if not changes:
        return

    assignments = ", ".join(f"{k} = ?" for k in changes)
    with db.transaction() as conn:
        cur = conn.execute(
            f"UPDATE companies SET {assignments} WHERE id = ?",
            (*changes.values(), company_id),
        )
        if cur.rowcount == 0:
            raise KeyError(f"Company {company_id} not found")
        # Stamped once the fiscal details are all present, so the panel can show at a
        # glance which clients are ready to invoice and which are half set up.
        row = conn.execute(
            "SELECT name, tax_id, address FROM companies WHERE id = ?", (company_id,)
        ).fetchone()
        if all((row[f] or "").strip() for f in REQUIRED_SETTINGS):
            conn.execute(
                "UPDATE companies SET configured_at = COALESCE(configured_at, "
                "datetime('now')) WHERE id = ?", (company_id,)
            )
        else:
            conn.execute(
                "UPDATE companies SET configured_at = NULL WHERE id = ?", (company_id,)
            )


def missing_settings(company: dict) -> list[str]:
    """Which required fiscal details are still blank, in words the panel can show."""
    labels = {"name": "nombre", "tax_id": "CIF", "address": "dirección"}
    return [labels[f] for f in REQUIRED_SETTINGS if not (company.get(f) or "").strip()]


def active_company() -> Optional[dict]:
    """The single company this installation serves, if there is exactly one.

    Settings are read through this: with one active client -- how the software is sold
    -- the panel drives the invoices. With none or several it returns None and the
    config file stays in charge, which is what keeps two clients from sharing settings.
    """
    active = [c for c in list_companies() if c["status"] == ACTIVE]
    return active[0] if len(active) == 1 else None


def set_company_status(company_id: int, status: str) -> None:
    """Suspend or reactivate a client.

    Suspending leaves every account and every invoice in place and simply stops anyone
    from that company signing in -- a customer who stops paying should not lose their
    records, and may well come back.
    """
    if status not in (ACTIVE, SUSPENDED):
        raise ValueError(f"Estado desconocido: {status}")
    with db.transaction() as conn:
        cur = conn.execute(
            "UPDATE companies SET status = ? WHERE id = ?", (status, company_id)
        )
        if cur.rowcount == 0:
            raise KeyError(f"Company {company_id} not found")


def create_user(email: str, role: str = EMPLOYEE, *,
                company_id: Optional[int] = None, name: Optional[str] = None,
                permissions=None, created_by: Optional[int] = None) -> int:
    """Add an account. A superadmin has no company; everyone else must have one."""
    email = normalize_email(email)
    if not email or "@" not in email:
        raise ValueError("Hace falta un email válido")
    if role not in ROLES:
        raise ValueError(f"Rol desconocido: {role}")
    if role == SUPERADMIN and company_id is not None:
        raise ValueError("Un superadmin no pertenece a ninguna empresa")
    if role != SUPERADMIN and company_id is None:
        raise ValueError("Hace falta decir a qué empresa pertenece la cuenta")

    if role == EMPLOYEE and permissions is None:
        permissions = DEFAULT_EMPLOYEE_PERMISSIONS

    with db.transaction() as conn:
        cur = conn.execute(
            "INSERT INTO users (company_id, email, name, role, permissions, created_by) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (company_id, email, (name or "").strip() or None, role,
             _clean_permissions(permissions), created_by),
        )
        return cur.lastrowid


def get_user(user_id: int) -> Optional[dict]:
    return _row(db.connect().execute(
        "SELECT * FROM users WHERE id = ?", (user_id,)
    ).fetchone())


def find_by_email(email: str) -> Optional[dict]:
    return _row(db.connect().execute(
        "SELECT * FROM users WHERE email = ?", (normalize_email(email),)
    ).fetchone())


def list_users(company_id: Optional[int] = None,
               include_inactive: bool = True) -> list[dict]:
    """Accounts for one company, or every account when company_id is None."""
    sql = "SELECT * FROM users WHERE 1 = 1"
    params: list = []
    if company_id is not None:
        sql += " AND company_id = ?"
        params.append(company_id)
    if not include_inactive:
        sql += " AND active = 1"
    sql += " ORDER BY role, email"
    return [_row(r) for r in db.connect().execute(sql, params).fetchall()]


def update_user(user_id: int, *, name=None, role=None, permissions=None,
                active=None) -> None:
    changes: dict[str, object] = {}
    if name is not None:
        changes["name"] = name.strip() or None
    if role is not None:
        if role not in ROLES:
            raise ValueError(f"Rol desconocido: {role}")
        changes["role"] = role
    if permissions is not None:
        changes["permissions"] = _clean_permissions(permissions)
    if active is not None:
        changes["active"] = 1 if active else 0
    if not changes:
        return

    assignments = ", ".join(f"{k} = ?" for k in changes)
    with db.transaction() as conn:
        cur = conn.execute(
            f"UPDATE users SET {assignments} WHERE id = ?",
            (*changes.values(), user_id),
        )
        if cur.rowcount == 0:
            raise KeyError(f"User {user_id} not found")


def record_login(user_id: int) -> None:
    with db.transaction() as conn:
        conn.execute(
            "UPDATE users SET last_login_at = datetime('now') WHERE id = ?", (user_id,)
        )


def count_active_admins(company_id: int, excluding: Optional[int] = None) -> int:
    sql = ("SELECT COUNT(*) FROM users WHERE company_id = ? AND role = ? AND active = 1")
    params: list = [company_id, ADMIN]
    if excluding is not None:
        sql += " AND id <> ?"
        params.append(excluding)
    return db.connect().execute(sql, params).fetchone()[0]


# ── The rules ────────────────────────────────────────────────────────────────

def can(user: Optional[dict], permission: str) -> bool:
    """Is this user allowed to do this?

    Deactivated accounts and suspended companies are refused before anything else, so
    revoking access is one flag rather than a sweep through every permission list.
    """
    if not user or not user.get("active", 1):
        return False

    role = user.get("role")

    if role == SUPERADMIN:
        return True
    if permission in VENDOR_ONLY:
        return False

    # A suspended client keeps their data but nobody can act on it.
    if user.get("company_status") == SUSPENDED:
        return False

    if role == ADMIN:
        return permission in _ADMIN_PERMISSIONS
    return permission in set(user.get("permissions") or ())


def effective_permissions(user: Optional[dict]) -> set[str]:
    """Everything this user can do, for showing them their own access."""
    if not user or not user.get("active", 1):
        return set()
    if user.get("role") == SUPERADMIN:
        return set(PERMISSIONS) | set(VENDOR_ONLY)
    if user.get("company_status") == SUSPENDED:
        return set()
    if user.get("role") == ADMIN:
        return set(_ADMIN_PERMISSIONS)
    return {p for p in (user.get("permissions") or ()) if p in PERMISSIONS}


def may_manage(actor: Optional[dict], target: dict) -> bool:
    """May `actor` edit or deactivate the account `target`?

    An owner manages their own company's staff and no one else's; the vendor manages
    anyone. Nobody may edit their own account here -- that is how someone accidentally
    removes their own last admin rights and locks the company out.
    """
    if not can(actor, "users.manage"):
        return False
    if actor["id"] == target["id"]:
        return False
    if actor["role"] == SUPERADMIN:
        return True
    if target["role"] == SUPERADMIN:
        return False
    return actor.get("company_id") is not None and \
        actor["company_id"] == target.get("company_id")


def authenticate(email: str) -> tuple[Optional[dict], Optional[str]]:
    """Resolve a signed-in Google identity to an account.

    Returns (user, refusal). The refusal is written for the person reading it, because
    "no estás autorizado" with no reason generates a support call every time.
    """
    email = normalize_email(email)
    user = find_by_email(email)

    if user is None:
        user = _bootstrap_superadmin(email)
    if user is None:
        return None, (
            f"La cuenta {email} no tiene acceso. Pide a quien administra el sistema "
            "que te dé de alta."
        )
    if not user["active"]:
        return None, f"La cuenta {email} está desactivada."

    if user["company_id"] is not None:
        company = get_company(user["company_id"])
        if company is None:
            return None, "La empresa de esta cuenta ya no existe."
        if company["status"] == SUSPENDED:
            return None, (
                f"El acceso de {company['name']} está suspendido. "
                "Ponte en contacto con el proveedor del software."
            )

    record_login(user["id"])
    return load_context(user["id"]), None


def _bootstrap_superadmin(email: str) -> Optional[dict]:
    """Let the vendor in the very first time, before any account exists.

    Without this the admin panel is unreachable: there is no account, and creating one
    needs the panel. The addresses come from SUPERADMIN_EMAILS in .env, which is not
    committed, and the row is created once and then behaves like any other account.
    """
    allowed = {normalize_email(e) for e in
               (os.getenv("SUPERADMIN_EMAILS") or "").replace(";", ",").split(",")}
    allowed.discard("")
    if email not in allowed:
        return None

    logger.info("Creating the first superadmin account for %s", email)
    return get_user(create_user(email, SUPERADMIN))


def load_context(user_id: int) -> Optional[dict]:
    """A user plus the company fields the permission rules and the UI need."""
    user = get_user(user_id)
    if user is None:
        return None
    if user["company_id"] is not None:
        company = get_company(user["company_id"])
        if company:
            user["company_name"] = company["name"]
            user["company_status"] = company["status"]
    return user
