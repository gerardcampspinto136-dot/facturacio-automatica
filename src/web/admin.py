"""The two account panels.

`/team`  — a client's own owner managing their staff: who works here, what each of them
           may do, and taking access away when someone leaves.
`/admin` — the vendor managing client companies: creating one with its first owner, and
           suspending a company without destroying anything it owns.

Both render with the same helpers as the rest of the site. Importing this module
registers its routes on the FastAPI app in src/web/app.py.
"""

import html

from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse

from src import accounts
from src.web.app import _empty, _guard, _page, app

_LAST_ADMIN_WARNING = (
    "<div class='card'><b>Es la única cuenta de responsable que queda.</b>"
    "<p class='muted'>Si la quitas, nadie de la empresa podrá gestionar cuentas ni dar "
    "de alta a nadie. Nombra antes a otro responsable.</p>"
    "<a class='btn btn-neutral' href='/team'>Volver</a></div>"
)


def _error(user, title: str, message: str, back: str) -> HTMLResponse:
    return _page(title, f"<div class='card'>{html.escape(message)}"
                        f"<div class='actions'><a class='btn btn-neutral' href='{back}'>"
                        f"Volver</a></div></div>", user)


def _role_badge(user: dict) -> str:
    labels = {
        accounts.SUPERADMIN: ("Proveedor", "#805ad5"),
        accounts.ADMIN: ("Responsable", "#2b6cb0"),
        accounts.EMPLOYEE: ("Empleado", "#4a5568"),
    }
    label, colour = labels.get(user["role"], (user["role"], "#4a5568"))
    return (f"<span class='badge' style='background:{colour};color:#fff'>"
            f"{html.escape(label)}</span>")


def _permission_checkboxes(granted) -> str:
    """The grant list, grouped by heading, as it appears on the employee form."""
    granted = set(granted or ())
    blocks = []
    for group, entries in accounts.permission_groups().items():
        rows = []
        for key, label in entries:
            checked = " checked" if key in granted else ""
            rows.append(
                f"<label class='perm'><input type='checkbox' name='perm' "
                f"value='{key}'{checked}>{html.escape(label)}</label>"
            )
        blocks.append(f"<div class='perm-group'><b>{html.escape(group)}</b>"
                      f"{''.join(rows)}</div>")
    return f"<div class='perms'>{''.join(blocks)}</div>"


def _describe(member: dict) -> str:
    if member["role"] != accounts.EMPLOYEE:
        return "Acceso completo a la empresa"
    labels = [accounts.PERMISSIONS[p][1] for p in member["permissions"]]
    return ", ".join(labels) if labels else "Sin permisos todavía"


# ── The client's own team ────────────────────────────────────────────────────

@app.get("/team", response_class=HTMLResponse)
async def team(request: Request):
    user, refusal = _guard(request, "users.manage")
    if refusal:
        return refusal

    if user["company_id"] is None:
        return _page("Equipo",
                     "<div class='card'>Tu cuenta no pertenece a ninguna empresa. "
                     "Gestiona las empresas cliente en "
                     "<a href='/admin'>Empresas</a>.</div>", user)

    members = accounts.list_users(user["company_id"])
    rows = []
    for member in members:
        state = ("" if member["active"] else
                 " <span class='badge badge-muted'>desactivada</span>")
        manage = (f"<a class='btn btn-neutral btn-sm' href='/team/{member['id']}'>"
                  f"Editar</a>"
                  if accounts.may_manage(user, member) else
                  "<span class='muted'>tu cuenta</span>"
                  if member["id"] == user["id"] else "")
        rows.append(
            f"<tr><td><div class='strong'>"
            f"{html.escape(member['name'] or member['email'])}</div>"
            f"<span class='muted'>{html.escape(member['email'])}</span></td>"
            f"<td>{_role_badge(member)}{state}</td>"
            f"<td class='muted'>{html.escape(_describe(member))}</td>"
            f"<td>{manage}</td></tr>"
        )
    table = (
        "<div class='card'><div class='table-wrap'><table><thead><tr>"
        "<th>Persona</th><th>Rol</th><th>Puede</th><th></th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table></div></div>"
    )

    form = (
        "<div class='card'>"
        "<div class='card-title'>Dar de alta a alguien del equipo</div>"
        "<div class='card-hint'>Entra con su cuenta de Google, así que no hay "
        "contraseñas que repartir ni que cambiar. Cuando alguien se va, desactivas su "
        "cuenta y deja de entrar, sin perder nada de lo que hizo.</div>"
        "<form method='post' action='/team/new'>"
        "<div class='grid-2'>"
        "<div><label>Email de su cuenta de Google</label>"
        "<input name='email' type='email' required placeholder='nombre@empresa.com'>"
        "</div>"
        "<div><label>Nombre</label>"
        "<input name='name' placeholder='Nombre y apellido'></div></div>"
        "<label>Puede…</label>"
        + _permission_checkboxes(accounts.DEFAULT_EMPLOYEE_PERMISSIONS) +
        "<div class='actions'><button class='btn-primary'>Crear cuenta</button></div>"
        "</form></div>"
    )
    return _page("Equipo", form + table, user,
                 subtitle=f"{len(members)} cuenta(s) en "
                          f"{html.escape(user.get('company_name') or 'tu empresa')}",
                 current="/team")


@app.post("/team/new")
async def team_new(request: Request):
    user, refusal = _guard(request, "users.manage")
    if refusal:
        return refusal
    if user["company_id"] is None:
        return RedirectResponse("/admin", status_code=303)

    form = await request.form()
    email = (form.get("email") or "").strip()
    try:
        accounts.create_user(
            email, accounts.EMPLOYEE,
            company_id=user["company_id"],
            name=(form.get("name") or "").strip() or None,
            permissions=form.getlist("perm"),
            created_by=user["id"],
        )
    except ValueError as exc:
        return _error(user, "No se pudo crear", str(exc), "/team")
    except Exception:
        return _error(user, "No se pudo crear",
                      f"Ya existe una cuenta con el email {email}.", "/team")
    return RedirectResponse("/team", status_code=303)


@app.get("/team/{user_id}", response_class=HTMLResponse)
async def team_edit(request: Request, user_id: int):
    user, refusal = _guard(request, "users.manage")
    if refusal:
        return refusal

    target = accounts.get_user(user_id)
    if target is None or not accounts.may_manage(user, target):
        return _error(user, "No disponible",
                      "No puedes gestionar esa cuenta.", "/team")

    if target["role"] == accounts.EMPLOYEE:
        perms = f"<label>Puede…</label>{_permission_checkboxes(target['permissions'])}"
    else:
        perms = ("<p class='muted'>Es responsable de la empresa, así que tiene acceso "
                 "completo. Cámbialo a empleado si quieres limitar lo que ve.</p>")

    options = "".join(
        f"<option value='{role}'{' selected' if target['role'] == role else ''}>"
        f"{'Responsable' if role == accounts.ADMIN else 'Empleado'}</option>"
        for role in (accounts.ADMIN, accounts.EMPLOYEE)
    )

    deactivating = bool(target["active"])
    body = (
        f"<form method='post' action='/team/{user_id}'><div class='card'>"
        f"<b>{html.escape(target['email'])}</b> {_role_badge(target)}"
        f"<label>Nombre</label>"
        f"<input name='name' value='{html.escape(target['name'] or '')}'>"
        f"<label>Rol</label>"
        f"<select name='role' style='width:100%;padding:8px'>{options}</select>"
        f"{perms}"
        f"<div class='actions'><button class='btn-primary'>Guardar</button>"
        f"<a class='btn btn-neutral' href='/team'>Cancelar</a></div></div></form>"

        f"<form method='post' action='/team/{user_id}/active' "
        f"onsubmit=\"return confirm('¿Seguro?')\"><div class='card'>"
        f"<b>{'Desactivar' if deactivating else 'Reactivar'} la cuenta</b>"
        f"<p class='muted'>"
        f"{'Dejará de poder entrar. No se borra nada de lo que haya hecho.' if deactivating else 'Podrá volver a entrar con su cuenta de Google.'}"
        f"</p>"
        f"<input type='hidden' name='active' value='{0 if deactivating else 1}'>"
        f"<button class='{'btn-danger' if deactivating else 'btn-primary'}'>"
        f"{'Desactivar' if deactivating else 'Reactivar'}</button></div></form>"
    )
    return _page("Editar cuenta", body, user)


@app.post("/team/{user_id}")
async def team_save(request: Request, user_id: int):
    user, refusal = _guard(request, "users.manage")
    if refusal:
        return refusal
    target = accounts.get_user(user_id)
    if target is None or not accounts.may_manage(user, target):
        return RedirectResponse("/team", status_code=303)

    form = await request.form()
    role = form.get("role") or target["role"]
    if role not in (accounts.ADMIN, accounts.EMPLOYEE):
        role = target["role"]

    # Demoting the last owner would leave the company unable to manage its own
    # accounts, with no way back except asking the vendor.
    if (target["role"] == accounts.ADMIN and role != accounts.ADMIN
            and accounts.count_active_admins(
                target["company_id"], excluding=target["id"]) == 0):
        return _page("No se puede", _LAST_ADMIN_WARNING, user)

    accounts.update_user(
        user_id,
        name=form.get("name") or "",
        role=role,
        permissions=form.getlist("perm") if role == accounts.EMPLOYEE else [],
    )
    return RedirectResponse("/team", status_code=303)


@app.post("/team/{user_id}/active")
async def team_active(request: Request, user_id: int):
    user, refusal = _guard(request, "users.manage")
    if refusal:
        return refusal
    target = accounts.get_user(user_id)
    if target is None or not accounts.may_manage(user, target):
        return RedirectResponse("/team", status_code=303)

    form = await request.form()
    active = (form.get("active") or "0") == "1"

    if (not active and target["role"] == accounts.ADMIN
            and accounts.count_active_admins(
                target["company_id"], excluding=target["id"]) == 0):
        return _page("No se puede", _LAST_ADMIN_WARNING, user)

    accounts.update_user(user_id, active=active)
    return RedirectResponse("/team", status_code=303)


# ── The vendor's client companies ────────────────────────────────────────────

def _company_card(company: dict) -> str:
    members = accounts.list_users(company["id"])
    owners = [m for m in members if m["role"] == accounts.ADMIN and m["active"]]
    suspended = company["status"] == accounts.SUSPENDED

    badge = ("<span class='badge badge-danger'>suspendida</span>" if suspended
             else "<span class='badge badge-ok'>activa</span>")

    problems = []
    if not owners:
        problems.append("no tiene ningún responsable activo, así que nadie de la "
                        "empresa puede dar de alta a su equipo")
    missing = accounts.missing_settings(company)
    if missing:
        problems.append("le faltan datos fiscales ("
                        + html.escape(", ".join(missing))
                        + "), así que sus facturas saldrían marcadas como prueba")
    if not (company.get("telegram_bot_token") or "").strip():
        problems.append("no tiene bot de Telegram conectado")
    else:
        badge += " <span class='badge badge-ok'>bot ✓</span>"

    warning = ""
    if problems:
        items = "".join(f"<li>{p}</li>" for p in problems)
        warning = (f"<div class='notice notice-warn'><b>Todavía no está lista</b>"
                   f"<ul style='margin:6px 0 0 18px;padding:0'>{items}</ul></div>")

    rows = "".join(
        f"<tr><td>{html.escape(m['name'] or '—')}</td>"
        f"<td>{html.escape(m['email'])}</td>"
        f"<td>{_role_badge(m)}{'' if m['active'] else ' (desactivada)'}</td>"
        f"<td class='num'>{(m['last_login_at'] or '—')[:10]}</td></tr>"
        for m in members
    ) or "<tr><td colspan='4' class='muted'>Sin cuentas todavía</td></tr>"

    actions = (
        f"<a class='btn btn-neutral btn-sm' href='/admin/{company['id']}/settings'>"
        f"Configurar empresa y bot</a>"
        f"<form method='post' action='/admin/{company['id']}/status' "
        f"onsubmit=\"return confirm('&iquest;Cambiar el acceso de esta empresa?')\">"
        f"<input type='hidden' name='status' value='"
        f"{accounts.ACTIVE if suspended else accounts.SUSPENDED}'>"
        f"<button class='{'btn-primary' if suspended else 'btn-neutral'} btn-sm'>"
        f"{'Reactivar acceso' if suspended else 'Suspender'}</button></form>"
    )
    return (
        f"<div class='card'><div class='row'>"
        f"<div><div class='card-title'>{html.escape(company['name'])} {badge}</div>"
        f"<div class='muted'>{html.escape(company['tax_id'] or 'sin CIF')} &middot; "
        f"{len(members)} cuenta(s) &middot; {len(owners)} responsable(s)</div></div>"
        f"<div class='actions'>{actions}</div></div>"
        f"{warning}"
        f"<div class='table-wrap'><table><thead><tr><th>Nombre</th><th>Email</th>"
        f"<th>Rol</th><th class='num'>&Uacute;ltimo acceso</th></tr></thead>"
        f"<tbody>{rows}</tbody></table></div>"
        f"<form method='post' action='/admin/{company['id']}/owner' class='billform'>"
        f"<input name='email' type='email' placeholder='email del responsable' required>"
        f"<input name='name' placeholder='nombre'>"
        f"<button class='btn-neutral btn-sm'>A&ntilde;adir responsable</button>"
        f"</form></div>"
    )


@app.get("/admin", response_class=HTMLResponse)
async def admin(request: Request):
    user, refusal = _guard(request, "companies.manage")
    if refusal:
        return refusal

    companies = accounts.list_companies()
    active = [c for c in companies if c["status"] == accounts.ACTIVE]

    form = (
        "<div class='card'>"
        "<div class='card-title'>Dar de alta una empresa cliente</div>"
        "<div class='card-hint'>Crea la empresa y su primer responsable. A partir de "
        "ah&iacute; el responsable da de alta a su propio equipo sin pasar por ti.</div>"
        "<form method='post' action='/admin/new'>"
        "<div class='grid-2'>"
        "<div><label>Nombre de la empresa</label><input name='name' required></div>"
        "<div><label>CIF</label><input name='tax_id' placeholder='B12345678'></div>"
        "</div>"
        "<label>Email del responsable</label>"
        "<input name='owner_email' type='email' required "
        "placeholder='responsable@empresa.com'>"
        "<div class='actions'><button class='btn-primary'>Crear empresa</button></div>"
        "</form></div>"
    )
    listing = "".join(_company_card(c) for c in companies) or _empty(
        "Todav&iacute;a no hay ninguna empresa cliente",
        "Crea la primera con el formulario de arriba.")
    return _page("Empresas cliente", form + listing, user,
                 subtitle=f"{len(companies)} empresa(s) &middot; {len(active)} activa(s)",
                 current="/admin")


@app.post("/admin/new")
async def admin_new(request: Request):
    user, refusal = _guard(request, "companies.manage")
    if refusal:
        return refusal

    form = await request.form()
    owner_email = (form.get("owner_email") or "").strip()
    try:
        company_id = accounts.create_company(
            (form.get("name") or "").strip(),
            tax_id=(form.get("tax_id") or "").strip() or None,
            contact_email=owner_email or None,
        )
    except ValueError as exc:
        return _error(user, "No se pudo crear", str(exc), "/admin")

    if owner_email:
        try:
            accounts.create_user(owner_email, accounts.ADMIN, company_id=company_id,
                                 created_by=user["id"])
        except ValueError as exc:
            return _error(user, "Empresa creada, responsable no",
                          f"La empresa se ha creado, pero {exc}.", "/admin")
        except Exception:
            return _error(user, "Empresa creada, responsable no",
                          f"La empresa se ha creado, pero el email {owner_email} ya "
                          "tiene una cuenta en el sistema.", "/admin")
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/{company_id}/owner")
async def admin_add_owner(request: Request, company_id: int):
    user, refusal = _guard(request, "companies.manage")
    if refusal:
        return refusal
    if accounts.get_company(company_id) is None:
        return _error(user, "No encontrada", "Esa empresa ya no existe.", "/admin")

    form = await request.form()
    try:
        accounts.create_user(
            (form.get("email") or "").strip(), accounts.ADMIN,
            company_id=company_id, name=(form.get("name") or "").strip() or None,
            created_by=user["id"],
        )
    except ValueError as exc:
        return _error(user, "No se pudo añadir", str(exc), "/admin")
    except Exception:
        return _error(user, "No se pudo añadir",
                      "Ese email ya tiene una cuenta en el sistema.", "/admin")
    return RedirectResponse("/admin", status_code=303)


def _field(label: str, name: str, value, hint: str = "", kind: str = "text") -> str:
    note = f"<span class='muted'> — {html.escape(hint)}</span>" if hint else ""
    return (f"<label>{html.escape(label)}{note}</label>"
            f"<input name='{name}' type='{kind}' "
            f"value='{html.escape(str(value or ''))}'>")


@app.get("/admin/{company_id}/settings", response_class=HTMLResponse)
async def company_settings(request: Request, company_id: int):
    """Everything needed to put a client live, on one page."""
    user, refusal = _guard(request, "companies.manage")
    if refusal:
        return refusal

    company = accounts.get_company(company_id)
    if company is None:
        return _error(user, "No encontrada", "Esa empresa ya no existe.", "/admin")

    missing = accounts.missing_settings(company)
    banner = (
        f"<div class='card' style='border-color:#c53030'>"
        f"<b>Faltan datos obligatorios: {html.escape(', '.join(missing))}.</b>"
        f"<p class='muted'>Hasta que estén, sus facturas salen marcadas como "
        f"DOCUMENTO DE PRUEBA.</p></div>" if missing else
        "<div class='card' style='border-color:#2f855a'>"
        "<b>✅ Lista para facturar.</b></div>"
    )

    inclusive = bool(company.get("prices_include_tax"))
    manual = (company.get("review_mode") or "manual") == "manual"

    body = (
        banner +
        f"<form method='post' action='/admin/{company_id}/settings'>"

        "<div class='card'><b>Datos fiscales</b>"
        "<p class='muted'>Salen impresos en cada factura que emita esta empresa.</p>"
        + _field("Nombre fiscal", "name", company["name"])
        + _field("CIF / NIF", "tax_id", company["tax_id"], "B12345678")
        + _field("Dirección", "address", company.get("address"))
        + _field("Teléfono", "phone", company.get("phone"))
        + _field("Email desde el que factura", "invoice_email",
                 company.get("invoice_email"), "aparece en la factura", "email")
        + _field("IBAN", "iban", company.get("iban"), "para que le paguen")
        + "</div>"

        "<div class='card'><b>Facturación</b>"
        + _field("IVA por defecto (%)", "tax_rate", company.get("tax_rate") or 21,
                 "21, 10 o 4", "number")
        + _field("Forma de pago", "payment_terms",
                 company.get("payment_terms") or "30 días")
        + _field("Serie de numeración", "invoice_series",
                 company.get("invoice_series"), "en blanco = sin serie")
        + "<label>¿Los precios que dictan ya llevan el IVA dentro?</label>"
        + "<select name='prices_include_tax' style='width:100%;padding:8px'>"
        + f"<option value='0'{'' if inclusive else ' selected'}>No — el IVA se suma aparte</option>"
        + f"<option value='1'{' selected' if inclusive else ''}>Sí — el precio ya lleva IVA</option>"
        + "</select>"
        + "<label>¿Las facturas se revisan antes de enviarse?</label>"
        + "<select name='review_mode' style='width:100%;padding:8px'>"
        + f"<option value='manual'{' selected' if manual else ''}>Sí — quedan pendientes de aprobar</option>"
        + f"<option value='auto'{'' if manual else ' selected'}>No — se envían al momento</option>"
        + "</select>"
        + "</div>"

        "<div class='card'><b>Su bot de Telegram</b>"
        "<p class='muted'>Cada empresa tiene su propio bot, con su propio nombre. "
        "Se crea en Telegram hablando con <b>@BotFather</b> (/newbot) y él da el token. "
        "Pégalo aquí y esta empresa queda conectada a su bot.</p>"
        + _field("Token del bot", "telegram_bot_token",
                 company.get("telegram_bot_token"), "1234567890:AA...")
        + _field("Chat de avisos", "telegram_chat_id",
                 company.get("telegram_chat_id"),
                 "el /chatid que dice el bot")
        + "</div>"

        "<div class='card'><b>Marca</b>"
        + _field("Ruta del logo", "logo_path",
                 company.get("logo_path") or "config/logo.png")
        + _field("Email de contacto (para ti)", "contact_email",
                 company.get("contact_email"), "no sale en la factura", "email")
        + "<label>Notas internas</label>"
        + f"<textarea name='notes' rows='2'>{html.escape(company.get('notes') or '')}</textarea>"
        + "</div>"

        "<div class='actions'><button class='btn-primary'>Guardar</button>"
        "<a class='btn btn-neutral' href='/admin'>Volver</a></div></form>"
    )
    return _page(f"Configurar {company['name']}", body, user)


@app.post("/admin/{company_id}/settings")
async def company_settings_save(request: Request, company_id: int):
    user, refusal = _guard(request, "companies.manage")
    if refusal:
        return refusal
    if accounts.get_company(company_id) is None:
        return _error(user, "No encontrada", "Esa empresa ya no existe.", "/admin")

    form = await request.form()
    values = {key: form.get(key) for key in accounts.SETTINGS_FIELDS
              if form.get(key) is not None}

    try:
        values["tax_rate"] = float(str(values.get("tax_rate", "21")).replace(",", "."))
    except ValueError:
        values.pop("tax_rate", None)
    values["prices_include_tax"] = 1 if form.get("prices_include_tax") == "1" else 0
    if values.get("review_mode") not in ("manual", "auto"):
        values.pop("review_mode", None)

    try:
        accounts.update_company(company_id, **values)
    except ValueError as exc:
        return _error(user, "No se pudo guardar", str(exc),
                      f"/admin/{company_id}/settings")

    # The settings are cached for the whole process, so a change here would otherwise
    # only show up after a restart -- and the person who just saved would think it
    # had not worked.
    from src.config_loader import reload_config

    reload_config()
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/{company_id}/status")
async def admin_status(request: Request, company_id: int):
    user, refusal = _guard(request, "companies.manage")
    if refusal:
        return refusal
    form = await request.form()
    try:
        accounts.set_company_status(company_id, (form.get("status") or "").strip())
    except (ValueError, KeyError) as exc:
        return _error(user, "No se pudo cambiar", str(exc), "/admin")
    return RedirectResponse("/admin", status_code=303)
