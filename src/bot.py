import asyncio
import logging
import os
import re
import tempfile

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegram.helpers import escape_markdown

from src import (accounts, bills, conversation, finalize, notify, receipts, rectify,
                 store, telegram_access)
from src.config_loader import get_config
from src.invoice_generator import generate_invoice_pdf
from src.parser import ParseError, parse_invoice_from_transcript
from src.totals import compute_totals, format_money
from src.transcription import transcribe_audio

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

_WELCOME = (
    "Hola! Soy tu asistente de facturación.\n\n"
    "Mándame un *audio* o escríbeme en texto, como se lo dirías a un compañero:\n"
    "_«Factura para Talleres Puig, 300 euros más IVA por la reparación»_\n\n"
    "Si me falta algo obligatorio (email, NIF...) te lo pido antes de emitir nada. "
    "Cuando esté completa te la enseño y tú decides si se envía.\n\n"
    "Para un *gasto*, mándame directamente una *foto* del ticket o de la factura "
    "del proveedor: la leo y la anoto yo.\n\n"
    "Comandos:\n"
    "• /ayuda — cómo hablarme y ejemplos\n"
    "• /pendientes — facturas esperando aprobación\n"
    "• /factura <número> — el PDF de una factura emitida\n"
    "• /clientes — clientes que ya tengo guardados\n"
    "• /cancelar — descartar la factura en curso\n"
    "• /anular <número> — emitir una rectificativa\n"
    "• /gasto <proveedor> <importe> — anotar una factura de proveedor\n"
    "• /pagos — qué debes y qué te deben\n"
    "• /cobrada — marcar una factura como cobrada · /recordar — reclamar un pago\n"
    "• /trimestre — el IVA y el IRPF del trimestre, y el paquete para el gestor\n"
    "• /stock — qué tienes en stock (se descuenta solo al facturar)\n"
    "• /producto — dar de alta un producto · /entrada — te ha llegado material"
)

_HELP = (
    "*Cómo pedirme una factura*\n"
    "Por audio o por texto, da igual:\n"
    "_«Factura para Juan García, email juan arroba ejemplo punto es, "
    "NIF 12345678A, tres horas a 50 euros la hora»_\n\n"
    "*El IVA*\n"
    "• «300 euros *más IVA*» → 300 de base, 363 en total\n"
    "• «300 euros *IVA incluido*» → 247,93 de base, 300 en total\n"
    "• Si no dices nada, uso lo que tengas puesto en `company.yaml` "
    "(`prices_include_tax`).\n"
    "También puedes cambiarlo con el botón *Cambiar IVA* antes de enviar.\n"
    "• «IVA del 10%» o «exento de IVA» → otro tipo solo para esa factura\n\n"
    "*Retención de IRPF*\n"
    "«con retención del 15%» o «sin retención». Si tu empresa la aplica siempre, "
    "sale sola; el botón *Quitar retención* la quita, y me acuerdo de lo de cada "
    "cliente para la próxima vez.\n\n"
    "*Datos obligatorios*\n"
    "Nombre, concepto e importe, email y NIF/CIF. Si falta algo te lo pregunto "
    "uno a uno. Lo que exijo se configura en `required_fields`.\n\n"
    "*Clientes*\n"
    "Guardo cada cliente la primera vez. Después basta con «factura para Talleres Puig» "
    "y ya sé su email y su NIF. Si hay dos con el mismo nombre, te pregunto cuál.\n\n"
    "*Para enviarla*\n"
    "Pulsa *Enviar* o contéstame «sí, envíala». Para descartarla, «no» o /cancelar. "
    "*Guardar sin enviar* la deja en /pendientes para enviarla más tarde.\n"
    "Si tu cuenta no puede enviar facturas, el botón es *Mandar a revisión*: le llega "
    "a tu responsable con un botón para aprobarla, y te aviso cuando lo haga.\n\n"
    "*Gastos: mándame una foto*\n"
    "Haz una foto del ticket o de la factura del proveedor y mándamela. Leo el "
    "proveedor, el número, la fecha, la base, el IVA y el total, y te lo enseño "
    "antes de anotar nada. Si algo no se lee, te lo pregunto.\n"
    "Puedes añadir un pie de foto para darme contexto: _«comida con cliente»_.\n"
    "Los tickets de tarjeta o efectivo los doy por pagados; una factura con "
    "vencimiento queda pendiente y entra en /pagos.\n\n"
    "*Stock*\n"
    "`/producto Tornillos M8 0,25 100` — dar de alta un producto: nombre, precio "
    "y cuántos tienes ahora. Sin la cantidad se crea como servicio, sin stock.\n"
    "`/entrada Tornillos M8 50` — te ha llegado material\n"
    "`/salida Tornillos M8 3` — se ha roto o lo has usado tú\n"
    "`/inventario Tornillos M8 87` — cuadrar con lo que hay en la estantería\n"
    "`/stock` — qué tienes · `/producto` — el catálogo entero\n\n"
    "Cuando factures algo que esté en el catálogo *lo descuento solo* y te digo "
    "cuánto queda. No hace falta que digas el nombre exacto: _«20 tornillos M8»_ "
    "encuentra el producto igual.\n\n"
    "*Otras cosas*\n"
    "`/anular 2026-0007 motivo` — factura rectificativa (devuelve el stock)\n"
    "`/factura 2026-0007` — el PDF de una factura · "
    "`/reenviar 2026-0007` — mandársela otra vez al cliente\n"
    "`/gasto Ferretería Puig 242,50 F-2026/88` — anotar un gasto sin foto\n"
    "`/pagos` — cobros y pagos pendientes"
)

# Shown to anyone the bot does not know. It says nothing about the company: a stranger
# who found the bot learns only how a real employee gets in.
_STRANGER = (
    "🔒 Este asistente de facturación es privado.\n\n"
    "Si trabajas en la empresa: entra en el panel web con tu cuenta de Google y pulsa "
    "*Mi cuenta → Conectar Telegram*. Si todavía no tienes cuenta, pídesela a tu "
    "responsable.\n\n"
    "Tu ID de Telegram: `{uid}`"
)


def _md(text) -> str:
    """Escape user-provided text for Telegram's Markdown, so a name with _ or * cannot
    break the message it appears in."""
    return escape_markdown(str(text or ""), version=1)


# ── Who is talking ───────────────────────────────────────────────────────────

def _who(update: Update):
    """The account this Telegram message acts as, or None for a stranger."""
    tg_user = update.effective_user
    chat = update.effective_chat
    return telegram_access.resolve(tg_user.id if tg_user else None,
                                   chat.id if chat else None)


def _target(update: Update):
    """Where to answer: the message, or the message a pressed button belongs to."""
    if update.message is not None:
        return update.message
    if update.callback_query is not None:
        return update.callback_query.message
    return None


async def _refuse_stranger(update: Update) -> None:
    tg_user = update.effective_user
    uid = tg_user.id if tg_user else 0
    target = _target(update)
    if target is not None:
        await target.reply_text(_STRANGER.format(uid=uid), parse_mode="Markdown")
    if tg_user is not None:
        # Plain HTTP to Telegram: off the event loop so other chats are not held up.
        await asyncio.to_thread(telegram_access.report_stranger,
                                uid, tg_user.full_name, tg_user.username)


async def _gate(update: Update, permission: str | None = None):
    """Resolve the caller and check one permission. Returns the account, or None.

    A handler does nothing until this has returned an account: the refusal has already
    been sent. Every handler states its permission here, the same keys the web uses,
    so what an employee may do is one decision in one place.
    """
    user = _who(update)
    if user is None:
        await _refuse_stranger(update)
        return None
    if permission and not accounts.can(user, permission):
        label = accounts.PERMISSIONS.get(permission, ("", permission))[1]
        target = _target(update)
        if target is not None:
            await target.reply_text(
                f"No tienes permiso para esto (te falta: «{label}»).\n"
                "Si lo necesitas para tu trabajo, pídeselo a tu responsable."
            )
        return None
    return user


def _display_name(user: dict) -> str:
    return user.get("name") or user.get("email") or "Responsable"


# ── Simple commands ──────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/start, or /start <code> from the "Conectar Telegram" link in the panel."""
    code = (context.args[0] if context.args else "").strip()
    if code:
        tg_user = update.effective_user
        user, refusal = accounts.link_telegram(
            code, tg_user.id if tg_user else 0, tg_user.username if tg_user else None)
        if user is None:
            await update.message.reply_text(f"⚠️ {refusal}")
            return
        company = user.get("company_name")
        await update.message.reply_text(
            f"✅ Hola, {_display_name(user)}. Tu Telegram ya está conectado a tu cuenta"
            + (f" de {company}" if company else "") + ".\n\n"
            "Mándame un audio o escríbeme para hacer una factura, o una foto de un "
            "ticket para anotar un gasto. /ayuda para ver todo lo que sé hacer."
        )
        return

    if await _gate(update) is None:
        return
    await update.message.reply_text(_WELCOME, parse_mode="Markdown")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _gate(update) is None:
        return
    await update.message.reply_text(_HELP, parse_mode="Markdown")


async def cmd_chatid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Report this chat's id — used to configure TELEGRAM_CHAT_ID.

    Open to everyone on purpose: it is how the owner's chat is set up in the first
    place, and it tells a stranger nothing but their own id.
    """
    await update.message.reply_text(
        f"El ID de este chat es: `{update.effective_chat.id}`\n"
        "Ponlo en el panel de administración (Configurar empresa → Chat de avisos) o en "
        "el archivo `.env` como `TELEGRAM_CHAT_ID` para recibir aquí los avisos.",
        parse_mode="Markdown",
    )


async def cmd_gasto(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log a supplier bill: /gasto <proveedor> <importe> [referencia]

    The amount is taken as the last numeric argument so the supplier name can contain
    spaces without needing quotes -- dictating "Ferreteria Puig 242" on a phone is the
    whole point of having this as a command.
    """
    if await _gate(update, "bills.manage") is None:
        return
    args = list(context.args or [])
    amount = None
    for i in range(len(args) - 1, -1, -1):
        try:
            amount = float(args[i].replace(",", ".").replace("€", ""))
            reference = " ".join(args[i + 1:]).strip() or None
            supplier = " ".join(args[:i]).strip()
            break
        except ValueError:
            continue

    if amount is None or not supplier:
        await update.message.reply_text(
            "Uso: `/gasto <proveedor> <importe> [su nº de factura]`\n"
            "Ejemplo: `/gasto Ferretería Puig 242,50 F-2026/88`",
            parse_mode="Markdown",
        )
        return

    bill_id = bills.create(supplier, amount, reference=reference)
    bill = bills.get(bill_id)
    config = get_config()
    await update.message.reply_text(
        f"✅ Anotado: *{_md(bill['supplier_name'])}* — {amount:.2f} {config.currency_symbol}\n"
        f"Vence el *{bill['due_date']}*.\n"
        f"Total pendiente de pagar: {format_money(bills.total_owed(), config)}",
        parse_mode="Markdown",
    )


async def cmd_pagos(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show what is owed out and what is owed in, on demand -- as far as the caller may see."""
    from src.notify import build_money_digest

    user = await _gate(update)
    if user is None:
        return
    see_bills = accounts.can(user, "bills.view")
    see_receivables = accounts.can(user, "receivables.view")
    if not (see_bills or see_receivables):
        await update.message.reply_text(
            "No tienes permiso para ver los cobros ni los pagos. "
            "Si lo necesitas, pídeselo a tu responsable.")
        return

    config = get_config()
    digest = build_money_digest(
        bills.due_soon(within_days=config.bills_due_within_days) if see_bills else [],
        store.list_unpaid() if see_receivables else [],
    )
    await update.message.reply_text(
        digest or "No hay pagos ni cobros pendientes. 🎉"
    )


def _trailing_numbers(args: list[str], how_many: int):
    """Split "Tornillos inox M8 0,25 100" into ("Tornillos inox M8", [0.25, 100.0]).

    The numbers are taken from the end so a product name can contain spaces and even
    digits ("Broca 10mm") without needing quotes -- the same trick /gasto uses, because
    typing quotation marks on a phone is nobody's idea of a good time.
    """
    numbers: list[float] = []
    index = len(args)
    while index > 0 and len(numbers) < how_many:
        candidate = args[index - 1].replace("€", "").replace(",", ".")
        try:
            numbers.insert(0, float(candidate))
        except ValueError:
            break
        index -= 1
    return " ".join(args[:index]).strip(), numbers


def _stock_line(product: dict) -> str:
    mark = "⚠️" if (product["reorder_point"] > 0
                    and product["stock_qty"] <= product["reorder_point"]) else "•"
    line = f"  {mark} {_md(product['name'])}: {product['stock_qty']:g} {product['unit']}"
    if product["reorder_point"] > 0:
        line += f" (pedir a {product['reorder_point']:g})"
    return line


async def cmd_stock(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show what is in stock, with anything at its reorder point flagged first."""
    from src import catalog

    if await _gate(update, "stock.view") is None:
        return

    tracked = [p for p in catalog.list_all() if p["track_stock"]]
    if not tracked:
        await update.message.reply_text(
            "Todavía no tienes productos con stock.\n\n"
            "Añade uno así:\n"
            "`/producto Tornillos M8 0,25 100`\n"
            "(nombre, precio de venta y cuántos tienes ahora)",
            parse_mode="Markdown",
        )
        return

    low = [p for p in tracked
           if p["reorder_point"] > 0 and p["stock_qty"] <= p["reorder_point"]]
    rest = [p for p in tracked if p not in low]

    lines = []
    if low:
        lines.append("⚠️ *Por reponer*")
        lines.extend(_stock_line(p) for p in low)
        lines.append("")
    lines.append(f"📦 *Stock* ({len(tracked)} productos)")
    lines.extend(_stock_line(p) for p in rest[:40])
    if len(rest) > 40:
        lines.append(f"  … y {len(rest) - 40} más")
    # Value is at cost, and cost is only known if it was entered. Printing "0,00 €"
    # over a full shelf reads like a bug, so the line is simply left out.
    value = catalog.stock_value()
    if value:
        lines.append(f"\nValor del stock (a coste): "
                     f"{format_money(value, get_config())}")
    lines.append("\n`/entrada <producto> <cantidad>` cuando te llegue material")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_producto(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Add a product, or list the catalog: /producto <nombre> <precio> [stock inicial]"""
    from src import catalog

    args = list(context.args or [])
    if not args:
        if await _gate(update, "stock.view") is None:
            return
        products = catalog.list_all()
        if not products:
            await update.message.reply_text(
                "No tienes ningún producto todavía.\n\n"
                "Añade uno así:\n`/producto Tornillos M8 0,25 100`\n"
                "→ nombre, precio de venta, y cuántos tienes ahora (opcional).",
                parse_mode="Markdown",
            )
            return
        config = get_config()
        lines = [f"*Catálogo ({len(products)})*", ""]
        for p in products[:40]:
            stock = (f" — {p['stock_qty']:g} {p['unit']}" if p["track_stock"]
                     else " — servicio")
            lines.append(f"• {_md(p['name'])}: "
                         f"{format_money(p['unit_price'], config)}{stock}")
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
        return

    if await _gate(update, "stock.manage") is None:
        return

    name, numbers = _trailing_numbers(args, 2)
    if not name or not numbers:
        await update.message.reply_text(
            "Uso: `/producto <nombre> <precio> [stock inicial]`\n"
            "Ejemplo: `/producto Tornillos M8 0,25 100`\n\n"
            "Para un servicio sin stock: `/producto Mano de obra 45`",
            parse_mode="Markdown",
        )
        return

    # One number is the price; two are price then opening stock.
    price = numbers[0]
    opening = numbers[1] if len(numbers) > 1 else 0.0

    if catalog.find_by_name(name):
        await update.message.reply_text(
            f"Ya tienes un producto llamado *{_md(name)}*. "
            f"Para añadirle stock usa `/entrada {name} <cantidad>`.",
            parse_mode="Markdown",
        )
        return

    product_id = catalog.create(
        name, unit_price=price, stock_qty=opening,
        track_stock=1 if len(numbers) > 1 else 0,
    )
    product = catalog.get(product_id)
    config = get_config()

    if product["track_stock"]:
        body = (f"✅ Producto creado: *{_md(product['name'])}*\n"
                f"Precio: {format_money(product['unit_price'], config)}\n"
                f"Stock inicial: {product['stock_qty']:g} {product['unit']}\n\n"
                f"Lo descontaré solo cuando lo factures.")
    else:
        body = (f"✅ Servicio creado: *{_md(product['name'])}*\n"
                f"Precio: {format_money(product['unit_price'], config)}\n\n"
                f"Sin control de stock. Si querías llevar stock, dime también "
                f"cuántos tienes: `/producto {product['name']} "
                f"{product['unit_price']:g} 100`")
    await update.message.reply_text(body, parse_mode="Markdown")


async def _adjust_stock(update, context, sign: int, verb: str) -> None:
    """Shared body of /entrada and /salida: <producto> <cantidad>."""
    from src import catalog

    if await _gate(update, "stock.manage") is None:
        return

    name, numbers = _trailing_numbers(list(context.args or []), 1)
    if not name or not numbers:
        await update.message.reply_text(
            f"Uso: `/{verb} <producto> <cantidad>`\n"
            f"Ejemplo: `/{verb} Tornillos M8 50`",
            parse_mode="Markdown",
        )
        return

    product = catalog.find_in_text(name)
    if product is None:
        await update.message.reply_text(
            f"No encuentro ningún producto que se llame «{_md(name)}». "
            "Mira `/producto` para ver los que tienes, o créalo con "
            f"`/producto {name} <precio> <cantidad>`.",
            parse_mode="Markdown",
        )
        return

    quantity = abs(numbers[0])
    balance = catalog.move(
        product["id"], sign * quantity,
        reason=catalog.PURCHASE if sign > 0 else catalog.ADJUSTMENT,
    )
    arrow = "➕" if sign > 0 else "➖"
    text = (f"{arrow} *{_md(product['name'])}*: {'+' if sign > 0 else '−'}{quantity:g}\n"
            f"Quedan *{balance:g} {product['unit']}*.")
    if product["reorder_point"] > 0 and balance <= product["reorder_point"]:
        text += f"\n⚠️ Estás en el punto de pedido ({product['reorder_point']:g})."
    await update.message.reply_text(text, parse_mode="Markdown")


async def cmd_entrada(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Receive stock: /entrada <producto> <cantidad>"""
    await _adjust_stock(update, context, +1, "entrada")


async def cmd_salida(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Take stock out by hand (breakage, own use): /salida <producto> <cantidad>"""
    await _adjust_stock(update, context, -1, "salida")


async def cmd_inventario(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Set a counted level: /inventario <producto> <cantidad contada>"""
    from src import catalog

    if await _gate(update, "stock.manage") is None:
        return

    name, numbers = _trailing_numbers(list(context.args or []), 1)
    if not name or not numbers:
        await update.message.reply_text(
            "Uso: `/inventario <producto> <cantidad contada>`\n"
            "Ejemplo: `/inventario Tornillos M8 87`\n"
            "Sirve para cuadrar el stock con lo que hay de verdad en la estantería.",
            parse_mode="Markdown",
        )
        return

    product = catalog.find_in_text(name)
    if product is None:
        await update.message.reply_text(f"No encuentro «{name}» en tus productos.")
        return

    before = product["stock_qty"]
    balance = catalog.set_level(product["id"], numbers[0])
    difference = round(balance - before, 4)
    text = (f"📋 *{_md(product['name'])}*: {before:g} → *{balance:g} {product['unit']}*\n"
            f"Diferencia: {difference:+g}")
    await update.message.reply_text(text, parse_mode="Markdown")


def _resend_button(result) -> list:
    """A retry button when the email failed -- the invoice itself already exists."""
    if result.emailed or not result.invoice.client_email:
        return []
    return [("🔁 Reintentar el envío", f"resend:{result.number}")]


async def cmd_anular(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/anular <número> [motivo]: cancel an issued invoice with a rectifying one."""
    if await _gate(update, "invoices.rectify") is None:
        return
    if not context.args:
        await update.message.reply_text(
            "Uso: `/anular <número de factura> [motivo]`\n"
            "Ejemplo: `/anular 2026-0007 precio equivocado`",
            parse_mode="Markdown",
        )
        return

    number = context.args[0].strip()
    reason = " ".join(context.args[1:]).strip() or None
    status = await update.message.reply_text(
        f"Emitiendo factura rectificativa que anula *{_md(number)}*...",
        parse_mode="Markdown",
    )
    try:
        result = await asyncio.to_thread(rectify.rectify, number, reason)
        config = get_config()
        _, _, total = compute_totals(result.invoice, config)
        with open(result.pdf_path, "rb") as pdf_file:
            await update.message.reply_document(
                document=pdf_file,
                filename=f"Factura_{result.number}.pdf",
                caption=(
                    f"✅ Factura rectificativa {result.number}\n"
                    f"Anula la factura {number}\n"
                    f"Importe: {format_money(total, config)}\n"
                    f"{result.email_status}"
                ),
                reply_markup=_keyboard(_resend_button(result)),
            )
        await status.delete()
    except ValueError as exc:
        await status.edit_text(f"⚠️ {exc}")
    except Exception as exc:
        logger.error("Error creating rectifying invoice", exc_info=True)
        await status.edit_text(f"Error al anular la factura:\n`{exc}`", parse_mode="Markdown")


async def cmd_reenviar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/reenviar <número>: email an issued invoice to its client again."""
    if await _gate(update, "invoices.approve") is None:
        return
    if not context.args:
        await update.message.reply_text(
            "Uso: `/reenviar <número de factura>`\nEjemplo: `/reenviar 2026-0007`",
            parse_mode="Markdown",
        )
        return
    await _resend(update.message, context.args[0].strip())


async def _resend(target, number: str) -> None:
    status = await target.reply_text(f"Reenviando la factura {number}...")
    sent, error = await asyncio.to_thread(finalize.resend, number)
    if sent:
        record = store.get_issued(number)
        await status.edit_text(
            f"📧 Factura {number} enviada a {record['invoice'].client_email}.")
    else:
        await status.edit_text(f"⚠️ No se ha podido enviar la factura {number}: {error}")


def _which_quarter(args: list[str]) -> tuple[int, int] | None:
    """/trimestre, /trimestre 3, /trimestre 3T, /trimestre 2026 3 -> (year, quarter).

    With nothing said: the quarter being filed right now if it is filing time, else
    the one in progress -- "how much VAT am I carrying this quarter?".
    """
    from datetime import date

    from src import taxes

    today = date.today()
    if not args:
        return taxes.quarter_to_file(today) or taxes.quarter_of(today)
    numbers = [int(n) for n in re.findall(r"\d+", " ".join(args))]
    year = next((n for n in numbers if n > 2000), today.year)
    quarter = next((n for n in numbers if 1 <= n <= 4), None)
    return (year, quarter) if quarter else None


async def cmd_trimestre(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """The quarter's VAT and IRPF, with the pack for the gestor a tap away."""
    from src import gestor_pack, taxes

    if await _gate(update, "taxes.view") is None:
        return
    which = _which_quarter(list(context.args or []))
    if which is None:
        await update.message.reply_text(
            "Uso: `/trimestre` (el que toca), `/trimestre 3` o `/trimestre 2026 2`",
            parse_mode="Markdown")
        return
    year, quarter = which
    text = await asyncio.to_thread(taxes.summary_text, year, quarter)
    sent = gestor_pack.sent_on(year, quarter)
    if sent:
        text += f"\n\n✅ Ya se le mandó al gestor ({sent[:10]})."
    await update.message.reply_text(
        text, reply_markup=_keyboard(gestor_pack.buttons(year, quarter)))


async def _on_tax_button(update: Update, action: str, period: str) -> None:
    from src import gestor_pack

    query = update.callback_query
    if await _gate(update, "taxes.view") is None:
        return
    try:
        year, quarter = (int(p) for p in period.split("-"))
    except ValueError:
        return

    if action == "pack":
        status = await query.message.reply_text("Preparando el paquete...")
        path = await asyncio.to_thread(gestor_pack.build_pack, year, quarter)
        with open(path, "rb") as handle:
            await query.message.reply_document(
                document=handle, filename=path.name,
                caption=("Libros de facturas en Excel, el PDF de cada factura y los "
                         "tickets del trimestre. Reenvíaselo a tu gestor tal cual."))
        await status.delete()
    elif action == "mail":
        status = await query.message.reply_text("Enviándoselo al gestor...")
        sent, message = await asyncio.to_thread(gestor_pack.email_to_gestor, year, quarter)
        await status.edit_text(("📧 " if sent else "⚠️ ") + message)


async def cmd_cobrada(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/cobrada <número>: a client paid. Alone: what is unpaid, each with a button."""
    if await _gate(update, "receivables.manage") is None:
        return
    config = get_config()
    if context.args:
        number = context.args[0].strip()
        try:
            store.mark_paid(number)
        except KeyError:
            await update.message.reply_text(f"No encuentro la factura {number}.")
            return
        await update.message.reply_text(f"✅ Factura {number} marcada como cobrada.")
        return

    unpaid = store.list_unpaid()
    if not unpaid:
        await update.message.reply_text("No tienes nada pendiente de cobro. 🎉")
        return
    await update.message.reply_text(
        f"Tienes {len(unpaid)} factura(s) sin cobrar. Toca la que ya te hayan pagado:")
    for record in unpaid[:10]:
        inv = record["invoice"]
        late = (f" · {record['days_overdue']} día(s) de retraso"
                if record["days_overdue"] > 0 else "")
        await update.message.reply_text(
            f"{inv.invoice_number} — {inv.client_name}: "
            f"{format_money(compute_totals(inv, config)[2], config)}{late}",
            reply_markup=_keyboard([("✅ Cobrada", f"dun:paid:{inv.invoice_number}")]))


async def cmd_recordar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/recordar <número>: email the client a payment reminder now."""
    from src import payment_reminders

    if await _gate(update, "receivables.manage") is None:
        return
    if not context.args:
        await update.message.reply_text(
            "Uso: `/recordar <número de factura>`\nEjemplo: `/recordar 2026-0007`",
            parse_mode="Markdown")
        return
    sent, message = await asyncio.to_thread(payment_reminders.send,
                                            context.args[0].strip())
    await update.message.reply_text(("📧 " if sent else "⚠️ ") + message)


async def _on_dunning_button(update: Update, action: str, number: str) -> None:
    from src import payment_reminders

    query = update.callback_query
    if await _gate(update, "receivables.manage") is None:
        return
    await query.edit_message_reply_markup(reply_markup=None)
    if action == "send":
        sent, message = await asyncio.to_thread(payment_reminders.send, number)
        await query.message.reply_text(("📧 " if sent else "⚠️ ") + message)
    elif action == "paid":
        try:
            store.mark_paid(number)
            await query.message.reply_text(f"✅ Factura {number} marcada como cobrada.")
        except KeyError:
            await query.message.reply_text(f"No encuentro la factura {number}.")
    elif action == "stop":
        payment_reminders.pause(number)
        await query.message.reply_text(
            f"⏸ Vale, no le recordaré más la factura {number}. "
            "Puedes volver a activarlo desde el panel (Cobros).")


async def cmd_factura(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/factura <número>: the PDF of an issued invoice. Without a number: the latest ones."""
    if await _gate(update, "invoices.view") is None:
        return
    config = get_config()
    if not context.args:
        issued = store.list_issued()[:10]
        if not issued:
            await update.message.reply_text("Todavía no has emitido ninguna factura.")
            return
        lines = ["*Últimas facturas*", ""]
        for record in issued:
            inv = record["invoice"]
            _, _, total = compute_totals(inv, config)
            lines.append(f"• `{inv.invoice_number}` {_md(inv.client_name)} — "
                         f"{format_money(total, config)}")
        lines.append("\nPara el PDF: `/factura <número>`")
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
        return

    number = context.args[0].strip()
    path = await asyncio.to_thread(finalize.pdf_for, number)
    if path is None:
        await update.message.reply_text(f"No encuentro la factura {number}.")
        return
    with open(path, "rb") as pdf_file:
        await update.message.reply_document(document=pdf_file,
                                            filename=f"Factura_{number}.pdf")


def _keyboard(buttons):
    """[(label, data), ...] one per row, or a list of pairs for a row of several."""
    if not buttons:
        return None
    rows = []
    for entry in buttons:
        if isinstance(entry, list):
            rows.append([InlineKeyboardButton(label, callback_data=data)
                         for label, data in entry])
        else:
            label, data = entry
            rows.append([InlineKeyboardButton(label, callback_data=data)])
    return InlineKeyboardMarkup(rows)


async def _send(target, replies) -> None:
    """Deliver the conversation's replies to Telegram."""
    for reply in replies:
        await target.reply_text(
            reply.text,
            parse_mode="Markdown" if reply.markdown else None,
            reply_markup=_keyboard(reply.buttons),
        )


# ── Dictating an invoice ─────────────────────────────────────────────────────

async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await _gate(update, "invoices.create")
    if user is None:
        return

    status_msg = await update.message.reply_text("Recibido. Descargando audio...")
    tmp_path: str | None = None
    try:
        voice = update.message.voice or update.message.audio
        tg_file = await context.bot.get_file(voice.file_id)
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
            tmp_path = tmp.name
        await tg_file.download_to_drive(tmp_path)

        await status_msg.edit_text("Transcribiendo audio...")
        # A slow network call: on the event loop it would freeze every other chat.
        transcript = await asyncio.to_thread(transcribe_audio, tmp_path)
        logger.info("Transcript: %s", transcript)

        await status_msg.edit_text(
            f"Transcripción:\n_{_md(transcript)}_\n\nExtrayendo datos...",
            parse_mode="Markdown",
        )
        await _begin_invoice(update, status_msg, transcript, user)

    except ParseError as exc:
        # Already a sentence for the user ("la IA está saturada, prueba en un minuto").
        await status_msg.edit_text(f"⚠️ {exc}")
    except Exception as exc:
        logger.error("Error processing the audio", exc_info=True)
        await status_msg.edit_text(
            f"Error al procesar el audio:\n`{exc}`", parse_mode="Markdown"
        )
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """A photographed supplier bill or till receipt: read it and offer to file it.

    Telegram sends several sizes of a photo; the last is the largest, and receipts need
    every pixel they can get to stay legible. An image sent as a file (Document) arrives
    uncompressed, which is better still.
    """
    if await _gate(update, "bills.manage") is None:
        return

    message = update.message
    status_msg = await message.reply_text("Recibido. Leyendo el documento...")
    tmp_path: str | None = None
    try:
        if message.photo:
            source = message.photo[-1]
            suffix = ".jpg"
        else:
            source = message.document
            suffix = os.path.splitext(source.file_name or "")[1].lower() or ".jpg"

        tg_file = await context.bot.get_file(source.file_id)
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp_path = tmp.name
        await tg_file.download_to_drive(tmp_path)

        # Reading an image is a slow blocking call, and slower still when the free tier
        # is busy and it has to back off and retry. On the event loop that would freeze
        # every other chat until it finished, so it goes to a worker thread.
        receipt = await asyncio.to_thread(
            receipts.extract_receipt, tmp_path, message.caption
        )
        logger.info(
            "Receipt read: %s %s (confidence %.2f)",
            receipt.supplier_name, receipt.total, receipt.confidence,
        )

        # Filing copies the image out of the temp file, so it must happen before the
        # finally block deletes it -- hence the archive here rather than on confirm.
        # A photo that holds no document at all is not worth keeping.
        if receipt.looks_like_a_document:
            receipt.image_path = receipts.archive_image(tmp_path, receipt)

        conversation.clear(update.effective_chat.id)
        session = receipts.session_for(update.effective_chat.id)
        replies = session.start(receipt)
        await status_msg.delete()
        await _send(message, replies)

    except receipts.ReceiptError as exc:
        # Already written for the user: show it as-is, with no stack trace or backticks.
        await status_msg.edit_text(f"⚠️ {exc}")
    except Exception as exc:
        logger.error("Error reading the receipt", exc_info=True)
        await status_msg.edit_text(
            f"Error al leer el documento:\n`{exc}`", parse_mode="Markdown"
        )
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Text works exactly like voice: continue the dialogue, or start a new invoice."""
    text = (update.message.text or "").strip()

    # A photographed expense waiting for a yes, an amount or a supplier name owns the
    # next message; only then does a typed sentence mean "start an invoice".
    expense = receipts.session_for(update.effective_chat.id)
    if expense.active:
        if await _gate(update, "bills.manage") is None:
            return
        await _send(update.message, expense.handle_text(text))
        return

    user = await _gate(update, "invoices.create")
    if user is None:
        return

    session = conversation.session_for(update.effective_chat.id)

    if session.active:
        if session.awaiting == conversation.AWAIT_CONFIRM and conversation.says_yes(text):
            await _approve(update, session, user)
            return
        await _send(update.message, session.handle_text(text))
        return

    status_msg = await update.message.reply_text("Leyendo los datos de la factura...")
    await _begin_invoice(update, status_msg, text, user)


async def _begin_invoice(update, status_msg, text: str, user: dict) -> None:
    """Parse a dictation or a typed request and start the review dialogue."""
    session = conversation.session_for(update.effective_chat.id)
    try:
        invoice = await asyncio.to_thread(parse_invoice_from_transcript, text)
    except ParseError as exc:
        await status_msg.edit_text(f"⚠️ {exc}")
        return
    except Exception as exc:
        logger.error("Could not parse the invoice", exc_info=True)
        await status_msg.edit_text(
            f"No he podido leer los datos:\n`{exc}`", parse_mode="Markdown"
        )
        return

    replies = session.start(invoice, can_send=accounts.can(user, "invoices.approve"))
    await status_msg.delete()
    await _send(update.message, replies)


async def _approve(update, session, user: dict) -> None:
    """The user confirmed the invoice: issue it, or queue it if it is not theirs to send."""
    if not accounts.can(user, "invoices.approve"):
        await _queue(update, session, user, ask_for_approval=True)
        return

    message = _target(update)
    invoice = session.invoice
    config = get_config()

    status = await message.reply_text("Generando y enviando la factura...")
    try:
        # Stored first, so the invoice is linked to the client record it belongs to.
        saved_note = ""
        if session.contact_is_new() and session.save_contact():
            saved_note = f"\n\n💾 He guardado a {invoice.client_name} en tus clientes."
        session.remember_terms()

        # Numbering, PDF, Sheets and Gmail: slow and blocking, so off the event loop.
        result = await asyncio.to_thread(finalize.issue, invoice)
        _, _, total = compute_totals(invoice, config)

        # Stock moving without a word was the old behaviour: it only ever reached the
        # log file, so a level could drift for weeks before anyone noticed.
        stock_note = ""
        for movement in result.stock_movements:
            stock_note += (
                f"\n📦 {movement['name']}: −{movement['quantity']:g} → "
                f"quedan {movement['balance']:g} {movement['unit']}"
            )
            if movement["low"]:
                stock_note += " ⚠️ por reponer"

        with open(result.pdf_path, "rb") as pdf_file:
            await message.reply_document(
                document=pdf_file,
                filename=f"Factura_{result.number}.pdf",
                caption=(
                    f"✅ Factura {result.number}\n"
                    f"Cliente: {invoice.client_name}\n"
                    f"Total: {format_money(total, config)}\n"
                    f"{result.email_status}{stock_note}{saved_note}"
                ),
                reply_markup=_keyboard(_resend_button(result)),
            )
        await status.delete()
    except Exception as exc:
        logger.error("Could not issue the invoice", exc_info=True)
        await status.edit_text(
            f"Error al emitir la factura:\n`{exc}`", parse_mode="Markdown"
        )
    finally:
        conversation.clear(update.effective_chat.id)


async def _queue(update, session, user: dict, ask_for_approval: bool) -> None:
    """Put the confirmed draft in the pending queue instead of sending it.

    Two ways here: someone without the approval permission ("Mandar a revisión" -- the
    approvers are sent the draft with an approve button), and an approver who wants
    to look again later ("Guardar sin enviar" -- nobody else is bothered).

    No invoice number is consumed until approval, so a draft that is thrown away
    leaves no gap in the series.
    """
    message = _target(update)
    chat_id = update.effective_chat.id
    invoice = session.invoice
    config = get_config()
    try:
        # Remember a new client now: whoever approves it later should not lose them.
        if session.contact_is_new():
            session.save_contact()
        session.remember_terms()

        token = store.new_token()
        draft_path = finalize.draft_path(token)
        await asyncio.to_thread(generate_invoice_pdf, invoice, draft_path)
        store.add_pending(
            invoice, draft_path, token=token,
            created_by=user.get("id"), created_by_name=_display_name(user),
            created_chat_id=chat_id,
        )
        _, _, total = compute_totals(invoice, config)
        summary = f"{invoice.client_name} — {format_money(total, config)}"

        if ask_for_approval:
            told = await asyncio.to_thread(notify.request_approval, token, chat_id)
            if told:
                text = (f"📤 Mandada a revisión: {summary}.\n"
                        "Te aviso aquí en cuanto la aprueben o la descarten.")
            else:
                text = (f"📤 Guardada para revisión: {summary}.\n"
                        "Ahora mismo nadie que pueda aprobarla tiene Telegram conectado, "
                        "así que la verán en el panel web (Pendientes).")
        else:
            text = (f"💾 Guardada sin enviar: {summary}.\n"
                    "La tienes en /pendientes y en el panel para enviarla cuando quieras.")
        await message.reply_text(text)
    except Exception as exc:
        logger.error("Could not queue the invoice", exc_info=True)
        await message.reply_text(f"No he podido guardarla: {exc}")
    finally:
        conversation.clear(chat_id)


# ── The pending queue from Telegram ──────────────────────────────────────────

def _pending_buttons(token: str, can_approve: bool):
    if can_approve:
        return [[("✅ Aprobar y enviar", f"pend:ok:{token}"),
                 ("❌ Descartar", f"pend:no:{token}")],
                [("📄 Ver PDF", f"pend:pdf:{token}")]]
    return [[("📄 Ver PDF", f"pend:pdf:{token}")]]


async def cmd_pendientes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Invoices waiting for approval, each with its buttons."""
    user = await _gate(update, "invoices.view")
    if user is None:
        return
    pending = store.list_pending()
    if not pending:
        await update.message.reply_text("No hay ninguna factura pendiente de aprobar. 👌")
        return

    config = get_config()
    can_approve = accounts.can(user, "invoices.approve")
    await update.message.reply_text(
        f"Hay {len(pending)} factura(s) esperando aprobación:")
    for p in pending[:10]:
        inv = p["invoice"]
        _, _, total = compute_totals(inv, config)
        who = p.get("created_by_name")
        text = (f"🧾 {inv.client_name} — {format_money(total, config)}\n"
                f"Para: {inv.client_email or '(sin email)'}\n"
                f"Preparada el {(p.get('created') or '')[:10]}"
                + (f" por {who}" if who else ""))
        await update.message.reply_text(
            text, reply_markup=_keyboard(_pending_buttons(p["token"], can_approve)))
    if len(pending) > 10:
        await update.message.reply_text(
            f"… y {len(pending) - 10} más en el panel web (Pendientes).")


async def _on_pending_button(update: Update, action: str, token: str) -> None:
    query = update.callback_query
    user = await _gate(update, "invoices.view" if action == "pdf" else "invoices.approve")
    if user is None:
        return

    pending = store.get_pending(token)
    if pending is None:
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text(
            "Esa factura ya no está pendiente: la ha revisado otra persona.")
        return

    invoice = pending["invoice"]
    config = get_config()

    if action == "pdf":
        draft = pending.get("draft_path") or finalize.draft_path(token)
        if not os.path.exists(draft):
            await asyncio.to_thread(generate_invoice_pdf, invoice, draft)
        with open(draft, "rb") as pdf_file:
            await query.message.reply_document(document=pdf_file,
                                               filename="Borrador_factura.pdf")
        return

    await query.edit_message_reply_markup(reply_markup=None)
    approver = _display_name(user)

    if action == "no":
        store.remove_pending(token)
        await query.message.reply_text(f"❌ Descartada: {invoice.client_name}.")
        await asyncio.to_thread(
            notify.tell_creator, pending,
            f"❌ {approver} ha descartado tu factura para {invoice.client_name}. "
            "No se ha enviado nada.")
        return

    # action == "ok": approve and send.
    status = await query.message.reply_text("Aprobando y enviando...")
    invoice.invoice_number = None  # a fresh gap-free number, assigned now
    try:
        result = await asyncio.to_thread(finalize.issue, invoice, token)
    except KeyError:
        await status.edit_text("Esa factura ya no está pendiente: la ha aprobado otra persona.")
        return
    except Exception as exc:
        logger.error("Could not approve %s", token, exc_info=True)
        await status.edit_text(f"Error al emitir la factura:\n{exc}")
        return

    _, _, total = compute_totals(invoice, config)
    with open(result.pdf_path, "rb") as pdf_file:
        await query.message.reply_document(
            document=pdf_file,
            filename=f"Factura_{result.number}.pdf",
            caption=(f"✅ Factura {result.number} aprobada\n"
                     f"Cliente: {invoice.client_name}\n"
                     f"Total: {format_money(total, config)}\n{result.email_status}"),
            reply_markup=_keyboard(_resend_button(result)),
        )
    await status.delete()
    await asyncio.to_thread(
        notify.tell_creator, pending,
        f"✅ {approver} ha aprobado tu factura para {invoice.client_name}: "
        f"{result.number}, {format_money(total, config)}.\n{result.email_status}")


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data or ""

    if data.startswith("pend:"):
        _, action, token = (data.split(":", 2) + ["", ""])[:3]
        await _on_pending_button(update, action, token)
        return

    if data.startswith("dun:"):
        _, action, number = (data.split(":", 2) + ["", ""])[:3]
        await _on_dunning_button(update, action, number)
        return

    if data.startswith("tax:"):
        _, action, period = (data.split(":", 2) + ["", ""])[:3]
        await _on_tax_button(update, action, period)
        return

    if data.startswith("resend:"):
        if await _gate(update, "invoices.approve") is None:
            return
        await query.edit_message_reply_markup(reply_markup=None)
        await _resend(query.message, data.split(":", 1)[1])
        return

    if data.startswith("exp:"):
        if await _gate(update, "bills.manage") is None:
            return
        expense = receipts.session_for(update.effective_chat.id)
        await query.edit_message_reply_markup(reply_markup=None)
        if not expense.active:
            await query.message.reply_text("Ese gasto ya no está activo.")
            return
        action = data.split(":", 1)[1]
        if action == "save":
            await _send(query.message, expense.confirm())
        elif action == "toggle_paid":
            await _send(query.message, expense.toggle_paid())
        elif action == "cancel":
            await _send(query.message, expense.cancel())
        return

    user = await _gate(update, "invoices.create")
    if user is None:
        return

    session = conversation.session_for(update.effective_chat.id)

    if not session.active:
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text("Esa factura ya no está activa.")
        return

    if data == "approve":
        await query.edit_message_reply_markup(reply_markup=None)
        await _approve(update, session, user)
        return

    if data == "hold":
        await query.edit_message_reply_markup(reply_markup=None)
        await _queue(update, session, user, ask_for_approval=False)
        return

    if data == "cancel":
        await query.edit_message_reply_markup(reply_markup=None)
        await _send(query.message, session.cancel())
        return

    if data == "toggle_tax":
        await query.edit_message_reply_markup(reply_markup=None)
        await _send(query.message, session.toggle_tax())
        return

    if data == "toggle_irpf":
        await query.edit_message_reply_markup(reply_markup=None)
        await _send(query.message, session.toggle_irpf())
        return

    if data.startswith("contact:"):
        await query.edit_message_reply_markup(reply_markup=None)
        await _send(query.message, session.pick_contact(int(data.split(":", 1)[1])))
        return


async def cmd_cancelar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _gate(update) is None:
        return
    expense = receipts.session_for(update.effective_chat.id)
    if expense.active:
        await _send(update.message, expense.cancel())
        return

    session = conversation.session_for(update.effective_chat.id)
    if not session.active:
        await update.message.reply_text("No hay ninguna factura ni ningún gasto en marcha.")
        return
    await _send(update.message, session.cancel())


async def cmd_clientes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """List the stored clients, so it is obvious what the bot already knows."""
    from src import contacts

    if await _gate(update, "contacts.view") is None:
        return
    clients = contacts.list_all(contacts.CLIENT)
    if not clients:
        await update.message.reply_text(
            "Todavía no tienes clientes guardados. "
            "Se guardan solos cuando emites la primera factura a cada uno."
        )
        return
    lines = [f"*Clientes guardados ({len(clients)})*", ""]
    for c in clients:
        lines.append(f"• {_md(contacts.describe(c))}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Keep the bot alive through transient failures.

    A dropped connection while polling is normal on a laptop that sleeps or changes
    network; without a handler python-telegram-bot logs a full traceback for each one and
    the user is told nothing. Network errors are noted quietly; anything else gets a
    short apology in the chat so a silent failure is never mistaken for success.
    """
    from telegram.error import NetworkError, TimedOut

    error = context.error
    if isinstance(error, (NetworkError, TimedOut)):
        logger.warning("Network problem talking to Telegram: %s", error)
        return

    logger.error("Unhandled error", exc_info=error)
    message = getattr(update, "message", None) or getattr(
        getattr(update, "callback_query", None), "message", None
    )
    if message is not None:
        try:
            await message.reply_text(
                "Ha fallado algo por mi parte y no he podido continuar. "
                "Vuelve a intentarlo, o escribe /cancelar para empezar de cero."
            )
        except Exception:
            logger.exception("Could not even report the error to the user")


def _bot_token() -> str | None:
    """Which Telegram bot to run as -- see telegram_api.bot_token().

    Only the active company's bot runs here: one process serves one company, which is
    how the software is installed.
    """
    from src import telegram_api

    return telegram_api.bot_token()


# The menu Telegram shows when "/" is typed, so nobody has to remember a command.
_MENU = [
    ("ayuda", "Cómo hablarme y ejemplos"),
    ("pendientes", "Facturas esperando aprobación"),
    ("factura", "El PDF de una factura emitida"),
    ("cobrada", "Marcar una factura como cobrada"),
    ("trimestre", "IVA e IRPF del trimestre, y el paquete para el gestor"),
    ("pagos", "Qué debes y qué te deben"),
    ("clientes", "Clientes guardados"),
    ("stock", "Qué tienes en stock"),
    ("producto", "Dar de alta un producto o ver el catálogo"),
    ("entrada", "Te ha llegado material"),
    ("gasto", "Anotar una factura de proveedor"),
    ("anular", "Emitir una rectificativa"),
    ("cancelar", "Descartar lo que está en marcha"),
]


async def _post_init(app: Application) -> None:
    try:
        await app.bot.set_my_commands([BotCommand(c, d) for c, d in _MENU])
    except Exception:
        logger.warning("Could not publish the command menu", exc_info=True)


def run_bot() -> None:
    token = _bot_token()
    if not token:
        raise RuntimeError(
            "No hay ningún bot configurado. Pon el token en el panel de administración "
            "(Empresas → Configurar empresa y su bot) o en TELEGRAM_BOT_TOKEN en .env."
        )

    app = Application.builder().token(token).post_init(_post_init).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("ayuda", cmd_help))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("chatid", cmd_chatid))
    app.add_handler(CommandHandler("pendientes", cmd_pendientes))
    app.add_handler(CommandHandler("factura", cmd_factura))
    app.add_handler(CommandHandler("facturas", cmd_factura))
    app.add_handler(CommandHandler("reenviar", cmd_reenviar))
    app.add_handler(CommandHandler("trimestre", cmd_trimestre))
    app.add_handler(CommandHandler("cobrada", cmd_cobrada))
    app.add_handler(CommandHandler("cobradas", cmd_cobrada))
    app.add_handler(CommandHandler("recordar", cmd_recordar))
    app.add_handler(CommandHandler("impuestos", cmd_trimestre))
    app.add_handler(CommandHandler("anular", cmd_anular))
    app.add_handler(CommandHandler("gasto", cmd_gasto))
    app.add_handler(CommandHandler("pagos", cmd_pagos))
    app.add_handler(CommandHandler("stock", cmd_stock))
    app.add_handler(CommandHandler("producto", cmd_producto))
    app.add_handler(CommandHandler("productos", cmd_producto))
    app.add_handler(CommandHandler("entrada", cmd_entrada))
    app.add_handler(CommandHandler("salida", cmd_salida))
    app.add_handler(CommandHandler("inventario", cmd_inventario))
    app.add_handler(CommandHandler("cancelar", cmd_cancelar))
    app.add_handler(CommandHandler("clientes", cmd_clientes))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice))
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_error_handler(on_error)

    # Loud on purpose: this is installed once per client company, and the failure mode
    # is realising only after the first invoice went out that nobody filled this in.
    if get_config().is_placeholder:
        logger.warning(
            "config/company.yaml still has the example company details, so every "
            "invoice will be stamped DOCUMENTO DE PRUEBA. Fill in the client's name, "
            "CIF, address and IBAN before going live."
        )
    if not telegram_access.owner_chat_ids() and not accounts.telegram_recipients(
            "invoices.create"):
        logger.warning(
            "Nobody can use the bot yet: set the owner's chat (TELEGRAM_CHAT_ID, or "
            "'Chat de avisos' in the admin panel) or link an account from the panel "
            "(Mi cuenta -> Conectar Telegram). Strangers are refused."
        )

    logger.info("Bot started, waiting for messages...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)
