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

from src import bills, conversation, store
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
    "Comandos:\n"
    "• /ayuda — cómo hablarme y ejemplos\n"
    "• /clientes — clientes que ya tengo guardados\n"
    "• /cancelar — descartar la factura en curso\n"
    "• /anular <número> — emitir una rectificativa\n"
    "• /gasto <proveedor> <importe> — anotar una factura de proveedor\n"
    "• /pagos — qué debes y qué te deben\n"
    "• /stock — productos por reponer"
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
    "*Otras cosas*\n"
    "`/anular 2026-0007` — factura rectificativa\n"
    "`/gasto Ferretería Puig 242,50 F-2026/88` — anotar una factura de proveedor\n"
    "`/pagos` — cobros y pagos pendientes · `/stock` — qué reponer"
)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(_WELCOME, parse_mode="Markdown")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(_HELP, parse_mode="Markdown")


async def cmd_chatid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Report this chat's id — used to configure review.notify.telegram_chat_id."""
    await update.message.reply_text(
        f"El ID de este chat es: `{update.effective_chat.id}`\n"
        "Cópialo en `review.notify.telegram_chat_id` de config/company.yaml "
        "para recibir aquí los avisos de facturas pendientes.",
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


async def cmd_stock(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """List products that have reached their reorder point."""
    from src import catalog

    low = catalog.low_stock()
    if not low:
        await update.message.reply_text("Todo el stock está por encima del punto de pedido. 👍")
        return
    lines = ["📦 Stock bajo:"]
    for p in low[:20]:
        lines.append(
            f"  • {p['name']}: quedan {p['stock_qty']:g} {p['unit']} "
            f"(punto de pedido {p['reorder_point']:g})"
        )
    await update.message.reply_text("\n".join(lines))


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


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Text works exactly like voice: continue the dialogue, or start a new invoice."""
    text = (update.message.text or "").strip()
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

        with open(pdf_path, "rb") as pdf_file:
            await message.reply_document(
                document=pdf_file,
                filename=f"Factura_{invoice.invoice_number}.pdf",
                caption=(
                    f"✅ Factura *{invoice.invoice_number}*\n"
                    f"Cliente: {invoice.client_name}\n"
                    f"Total: {format_money(total, config)}\n"
                    f"{sent}{saved_note}"
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
    session = conversation.session_for(update.effective_chat.id)
    data = query.data or ""

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
    session = conversation.session_for(update.effective_chat.id)
    if not session.active:
        await update.message.reply_text("No hay ninguna factura en marcha.")
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
    app.add_handler(CommandHandler("cancelar", cmd_cancelar))
    app.add_handler(CommandHandler("clientes", cmd_clientes))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    logger.info("Bot started, waiting for messages...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)
