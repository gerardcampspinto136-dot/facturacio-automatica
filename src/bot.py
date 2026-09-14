import asyncio
import logging
import os
import tempfile

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from src import bills, conversation, receipts, store
from src.config_loader import get_config
from src.finalize import finalize_invoice
from src.invoice_generator import generate_invoice_pdf
from src.parser import parse_invoice_from_transcript
from src.rectify import create_rectifying_invoice
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
    "• /clientes — clientes que ya tengo guardados\n"
    "• /cancelar — descartar la factura en curso\n"
    "• /anular <número> — emitir una rectificativa\n"
    "• /gasto <proveedor> <importe> — anotar una factura de proveedor\n"
    "• /pagos — qué debes y qué te deben\n"
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
    "También puedes cambiarlo con el botón *Cambiar IVA* antes de enviar.\n\n"
    "*Datos obligatorios*\n"
    "Nombre, concepto e importe, email y NIF/CIF. Si falta algo te lo pregunto "
    "uno a uno. Lo que exijo se configura en `required_fields`.\n\n"
    "*Clientes*\n"
    "Guardo cada cliente la primera vez. Después basta con «factura para Talleres Puig» "
    "y ya sé su email y su NIF. Si hay dos con el mismo nombre, te pregunto cuál.\n\n"
    "*Para enviarla*\n"
    "Pulsa *Enviar* o contéstame «sí, envíala». Para descartarla, «no» o /cancelar.\n\n"
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
    "`/anular 2026-0007` — factura rectificativa (devuelve el stock)\n"
    "`/gasto Ferretería Puig 242,50 F-2026/88` — anotar un gasto sin foto\n"
    "`/pagos` — cobros y pagos pendientes"
)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(_WELCOME, parse_mode="Markdown")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(_HELP, parse_mode="Markdown")


async def cmd_chatid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Report this chat's id — used to configure TELEGRAM_CHAT_ID."""
    await update.message.reply_text(
        f"El ID de este chat es: `{update.effective_chat.id}`\n"
        "Ponlo en el archivo `.env` como `TELEGRAM_CHAT_ID` para recibir aquí los "
        "avisos. Va en `.env` y no en `company.yaml` porque ese se sube a GitHub.",
        parse_mode="Markdown",
    )


async def cmd_gasto(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log a supplier bill: /gasto <proveedor> <importe> [referencia]

    The amount is taken as the last numeric argument so the supplier name can contain
    spaces without needing quotes -- dictating "Ferreteria Puig 242" on a phone is the
    whole point of having this as a command.
    """
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
        f"✅ Anotado: *{bill['supplier_name']}* — {amount:.2f} {config.currency_symbol}\n"
        f"Vence el *{bill['due_date']}*.\n"
        f"Total pendiente de pagar: {format_money(bills.total_owed(), config)}",
        parse_mode="Markdown",
    )


async def cmd_pagos(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show what is owed out and what is owed in, on demand."""
    from src.notify import build_money_digest

    config = get_config()
    digest = build_money_digest(
        bills.due_soon(within_days=config.bills_due_within_days), store.list_unpaid()
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
    line = f"  {mark} {product['name']}: {product['stock_qty']:g} {product['unit']}"
    if product["reorder_point"] > 0:
        line += f" (pedir a {product['reorder_point']:g})"
    return line


async def cmd_stock(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show what is in stock, with anything at its reorder point flagged first."""
    from src import catalog

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
            lines.append(f"• {p['name']}: {format_money(p['unit_price'], config)}{stock}")
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
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
            f"Ya tienes un producto llamado *{name}*. "
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
        body = (f"✅ Producto creado: *{product['name']}*\n"
                f"Precio: {format_money(product['unit_price'], config)}\n"
                f"Stock inicial: {product['stock_qty']:g} {product['unit']}\n\n"
                f"Lo descontaré solo cuando lo factures.")
    else:
        body = (f"✅ Servicio creado: *{product['name']}*\n"
                f"Precio: {format_money(product['unit_price'], config)}\n\n"
                f"Sin control de stock. Si querías llevar stock, dime también "
                f"cuántos tienes: `/producto {product['name']} "
                f"{product['unit_price']:g} 100`")
    await update.message.reply_text(body, parse_mode="Markdown")


async def _adjust_stock(update, context, sign: int, verb: str) -> None:
    """Shared body of /entrada and /salida: <producto> <cantidad>."""
    from src import catalog

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
            f"No encuentro ningún producto que se llame «{name}». "
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
    text = (f"{arrow} *{product['name']}*: {'+' if sign > 0 else '−'}{quantity:g}\n"
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
    text = (f"📋 *{product['name']}*: {before:g} → *{balance:g} {product['unit']}*\n"
            f"Diferencia: {difference:+g}")
    await update.message.reply_text(text, parse_mode="Markdown")


async def cmd_anular(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text(
            "Uso: `/anular <número de factura>`\nEjemplo: `/anular 2026-0007`",
            parse_mode="Markdown",
        )
        return

    number = context.args[0].strip()
    status = await update.message.reply_text(
        f"Emitiendo factura rectificativa que anula *{number}*...", parse_mode="Markdown"
    )
    try:
        rectifying, pdf_path = create_rectifying_invoice(number)
        config = get_config()
        _, _, total = compute_totals(rectifying, config)
        with open(pdf_path, "rb") as pdf_file:
            await update.message.reply_document(
                document=pdf_file,
                filename=f"Factura_{rectifying.invoice_number}.pdf",
                caption=(
                    f"✅ Factura rectificativa *{rectifying.invoice_number}*\n"
                    f"Anula la factura {number}\n"
                    f"Importe: {format_money(total, config)}"
                ),
                parse_mode="Markdown",
            )
        await status.delete()
    except ValueError as exc:
        await status.edit_text(f"⚠️ {exc}")
    except Exception as exc:
        logger.error("Error creating rectifying invoice", exc_info=True)
        await status.edit_text(f"Error al anular la factura:\n`{exc}`", parse_mode="Markdown")


def _keyboard(buttons):
    if not buttons:
        return None
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(label, callback_data=data)] for label, data in buttons]
    )


async def _send(target, replies) -> None:
    """Deliver the conversation's replies to Telegram."""
    for reply in replies:
        await target.reply_text(
            reply.text,
            parse_mode="Markdown" if reply.markdown else None,
            reply_markup=_keyboard(reply.buttons),
        )


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    status_msg = await update.message.reply_text("Recibido. Descargando audio...")
    tmp_path: str | None = None
    try:
        voice = update.message.voice or update.message.audio
        tg_file = await context.bot.get_file(voice.file_id)
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
            tmp_path = tmp.name
        await tg_file.download_to_drive(tmp_path)

        await status_msg.edit_text("Transcribiendo audio...")
        transcript = transcribe_audio(tmp_path)
        logger.info("Transcript: %s", transcript)

        await status_msg.edit_text(
            f"Transcripción:\n_{transcript}_\n\nExtrayendo datos...",
            parse_mode="Markdown",
        )
        await _begin_invoice(update, status_msg, transcript)

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
        await _send(update.message, expense.handle_text(text))
        return

    session = conversation.session_for(update.effective_chat.id)

    if session.active:
        if session.awaiting == conversation.AWAIT_CONFIRM and conversation.says_yes(text):
            await _approve(update, session)
            return
        await _send(update.message, session.handle_text(text))
        return

    status_msg = await update.message.reply_text("Leyendo los datos de la factura...")
    await _begin_invoice(update, status_msg, text)


async def _begin_invoice(update, status_msg, text: str) -> None:
    """Parse a dictation or a typed request and start the review dialogue."""
    session = conversation.session_for(update.effective_chat.id)
    try:
        invoice = parse_invoice_from_transcript(text)
    except Exception as exc:
        logger.error("Could not parse the invoice", exc_info=True)
        await status_msg.edit_text(
            f"No he podido leer los datos:\n`{exc}`", parse_mode="Markdown"
        )
        return

    replies = session.start(invoice)
    await status_msg.delete()
    await _send(update.message, replies)


async def _approve(update, session) -> None:
    """Issue the invoice the user has just confirmed."""
    message = update.message or update.callback_query.message
    invoice = session.invoice
    config = get_config()

    status = await message.reply_text("Generando y enviando la factura...")
    try:
        new_contact = session.contact_is_new()
        pdf_path = finalize_invoice(invoice)
        _, _, total = compute_totals(invoice, config)

        saved_note = ""
        if new_contact and session.save_contact():
            saved_note = f"\n\n💾 He guardado a *{invoice.client_name}* en tus clientes."

        if invoice.client_email:
            sent = f"📧 Enviada a {invoice.client_email}"
        else:
            sent = "⚠️ No enviada: falta el email del cliente"

        # Stock moving without a word was the old behaviour: it only ever reached the
        # log file, so a level could drift for weeks before anyone noticed.
        stock_note = ""
        for movement in getattr(invoice, "stock_movements", []):
            stock_note += (
                f"\n📦 {movement['name']}: −{movement['quantity']:g} → "
                f"quedan {movement['balance']:g} {movement['unit']}"
            )
            if movement["low"]:
                stock_note += " ⚠️ por reponer"

        with open(pdf_path, "rb") as pdf_file:
            await message.reply_document(
                document=pdf_file,
                filename=f"Factura_{invoice.invoice_number}.pdf",
                caption=(
                    f"✅ Factura *{invoice.invoice_number}*\n"
                    f"Cliente: {invoice.client_name}\n"
                    f"Total: {format_money(total, config)}\n"
                    f"{sent}{stock_note}{saved_note}"
                ),
                parse_mode="Markdown",
            )
        await status.delete()
    except Exception as exc:
        logger.error("Could not issue the invoice", exc_info=True)
        await status.edit_text(
            f"Error al emitir la factura:\n`{exc}`", parse_mode="Markdown"
        )
    finally:
        conversation.clear(update.effective_chat.id)


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data or ""

    if data.startswith("exp:"):
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

    session = conversation.session_for(update.effective_chat.id)

    if not session.active:
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text("Esa factura ya no está activa.")
        return

    if data == "approve":
        await query.edit_message_reply_markup(reply_markup=None)
        await _approve(update, session)
        return

    if data == "cancel":
        await query.edit_message_reply_markup(reply_markup=None)
        await _send(query.message, session.cancel())
        return

    if data == "toggle_tax":
        await query.edit_message_reply_markup(reply_markup=None)
        await _send(query.message, session.toggle_tax())
        return

    if data.startswith("contact:"):
        await query.edit_message_reply_markup(reply_markup=None)
        await _send(query.message, session.pick_contact(int(data.split(":", 1)[1])))
        return


async def cmd_cancelar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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

    clients = contacts.list_all(contacts.CLIENT)
    if not clients:
        await update.message.reply_text(
            "Todavía no tienes clientes guardados. "
            "Se guardan solos cuando emites la primera factura a cada uno."
        )
        return
    lines = [f"*Clientes guardados ({len(clients)})*", ""]
    for c in clients:
        lines.append(f"• {contacts.describe(c)}")
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


def run_bot() -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set in .env")

    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("ayuda", cmd_help))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("chatid", cmd_chatid))
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

    logger.info("Bot started, waiting for messages...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)
