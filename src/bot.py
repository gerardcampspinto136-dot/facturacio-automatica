import logging
import os
import tempfile
import uuid
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from src.config_loader import get_config
from src.email_sender import send_invoice_email
from src.invoice_generator import generate_invoice_pdf
from src.invoice_number import get_next_invoice_number
from src.parser import parse_invoice_from_transcript
from src.sheets import add_invoice_to_sheet
from src.transcription import transcribe_audio

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

_WELCOME = (
    "Hola! Soy tu asistente de facturación automatica.\n\n"
    "Envíame un *mensaje de voz* con los datos de la factura y me encargaré de todo:\n\n"
    "• Transcribir el audio\n"
    "• Extraer los datos del cliente\n"
    "• Generar un *borrador* de la factura en PDF\n"
    "• Enviarla al cliente y registrarla en Google Sheets *cuando tú lo confirmes*\n\n"
    "Puedes mencionar: nombre del cliente, email, dirección, NIF/CIF, "
    "horas trabajadas, tarifa por hora, materiales, etc.\n\n"
    "Usa /ayuda para ver un ejemplo de lo que puedes decir."
)

_HELP = (
    "Ejemplo de mensaje de voz:\n\n"
    "_\"Factura para Juan García, email juan@ejemplo.com, dirección Calle Mayor 5 Madrid, "
    "DNI doce tres cuatro cinco seis siete ocho A. "
    "Le he hecho tres horas de trabajo a cincuenta euros la hora "
    "y materiales por cien euros.\"_\n\n"
    "Te mostraré un borrador para que lo revises. Solo se enviará al cliente "
    "cuando pulses *Confirmar y enviar*."
)


def _compute_totals(invoice, config) -> tuple[float, float, float]:
    subtotal = invoice.subtotal
    tax_amount = round(subtotal * config.tax_rate / 100, 2)
    total = round(subtotal + tax_amount, 2)
    return subtotal, tax_amount, total


def _format_summary(invoice, config) -> str:
    sym = config.currency_symbol
    subtotal, tax_amount, total = _compute_totals(invoice, config)

    lines = [
        "*Revisa la factura antes de enviarla:*",
        "",
        f"*Cliente:* {invoice.client_name or '⚠️ no detectado'}",
        f"*Email:* {invoice.client_email or '⚠️ no detectado'}",
    ]
    if invoice.client_id:
        lines.append(f"*NIF/CIF:* {invoice.client_id}")
    if invoice.client_address:
        lines.append(f"*Dirección:* {invoice.client_address}")

    lines.append("")
    lines.append("*Conceptos:*")
    for item in invoice.items:
        lines.append(
            f"• {item.description} — {item.quantity:g} × {item.unit_price:,.2f} "
            f"= {item.total:,.2f} {sym}"
        )

    lines.append("")
    lines.append(f"Base imponible: {subtotal:,.2f} {sym}")
    lines.append(f"IVA ({config.tax_rate}%): {tax_amount:,.2f} {sym}")
    lines.append(f"*TOTAL: {total:,.2f} {sym}*")

    if not invoice.client_email:
        lines.append("")
        lines.append("⚠️ _No he detectado el email del cliente. "
                     "Si confirmas, generaré la factura pero no podré enviarla por correo._")
    return "\n".join(lines)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(_WELCOME, parse_mode="Markdown")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(_HELP, parse_mode="Markdown")


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    status_msg = await update.message.reply_text("Recibido. Descargando audio...")

    tmp_path: str | None = None
    draft_path: str | None = None

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
            f"Transcripción:\n_{transcript}_\n\nExtrayendo datos de la factura...",
            parse_mode="Markdown",
        )

        invoice = parse_invoice_from_transcript(transcript)

        if not invoice.items:
            await status_msg.edit_text(
                "No he podido detectar ningún concepto a facturar. "
                "Prueba de nuevo indicando el trabajo, las horas o el importe."
            )
            return

        await status_msg.edit_text("Generando borrador de la factura...")

        # Draft PDF — the real invoice number is only consumed on confirmation,
        # so cancelled drafts never create gaps in the sequential numbering.
        token = uuid.uuid4().hex[:12]
        invoice.invoice_number = "BORRADOR"
        draft_path = f"data/invoices/_borrador_{token}.pdf"
        generate_invoice_pdf(invoice, draft_path)

        context.chat_data[token] = {"invoice": invoice, "draft_path": draft_path}

        config = get_config()
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Confirmar y enviar", callback_data=f"send:{token}"),
            InlineKeyboardButton("❌ Cancelar", callback_data=f"cancel:{token}"),
        ]])

        with open(draft_path, "rb") as pdf_file:
            await update.message.reply_document(
                document=pdf_file,
                filename="Borrador_factura.pdf",
                caption="📄 Borrador de la factura (aún no enviado)",
            )

        await status_msg.delete()
        await update.message.reply_text(
            _format_summary(invoice, config),
            reply_markup=keyboard,
            parse_mode="Markdown",
        )

    except Exception as exc:
        logger.error("Error processing invoice", exc_info=True)
        await status_msg.edit_text(
            f"Error al procesar la factura:\n`{exc}`\n\nRevisa los logs para más detalles.",
            parse_mode="Markdown",
        )
        if draft_path and os.path.exists(draft_path):
            os.unlink(draft_path)
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)


async def handle_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    action, _, token = query.data.partition(":")
    pending = context.chat_data.get(token)

    if not pending:
        await query.edit_message_text(
            "⏱️ Este borrador ya no está disponible. Envíame el mensaje de voz de nuevo."
        )
        return

    invoice = pending["invoice"]
    draft_path = pending["draft_path"]
    config = get_config()

    if action == "cancel":
        context.chat_data.pop(token, None)
        if draft_path and os.path.exists(draft_path):
            os.unlink(draft_path)
        await query.edit_message_text("❌ Factura cancelada. No se ha enviado nada.")
        return

    # action == "send"
    context.chat_data.pop(token, None)
    final_path: str | None = None
    try:
        await query.edit_message_text("Asignando número de factura y generando el PDF final...")

        invoice.invoice_number = get_next_invoice_number()
        final_path = f"data/invoices/Factura_{invoice.invoice_number}.pdf"
        generate_invoice_pdf(invoice, final_path)

        await query.edit_message_text(
            f"Registrando la factura *{invoice.invoice_number}* en Google Sheets...",
            parse_mode="Markdown",
        )
        add_invoice_to_sheet(invoice)

        _, _, total = _compute_totals(invoice, config)
        sym = config.currency_symbol

        if invoice.client_email:
            await query.edit_message_text(
                f"Enviando factura a *{invoice.client_email}*...", parse_mode="Markdown"
            )
            send_invoice_email(invoice, final_path)
            sent_line = f"Enviada a: {invoice.client_email}"
            final_note = f"✅ Factura *{invoice.invoice_number}* enviada y registrada."
        else:
            sent_line = "No enviada por email (falta el email del cliente)"
            final_note = (
                f"✅ Factura *{invoice.invoice_number}* generada y registrada.\n"
                "⚠️ No se envió por email porque falta la dirección del cliente."
            )

        with open(final_path, "rb") as pdf_file:
            await query.message.reply_document(
                document=pdf_file,
                filename=f"Factura_{invoice.invoice_number}.pdf",
                caption=(
                    f"Factura *{invoice.invoice_number}*\n"
                    f"Cliente: {invoice.client_name}\n"
                    f"Total: {total:,.2f} {sym}\n"
                    f"{sent_line}"
                ),
                parse_mode="Markdown",
            )

        await query.edit_message_text(final_note, parse_mode="Markdown")

    except Exception as exc:
        logger.error("Error finalising invoice", exc_info=True)
        await query.edit_message_text(
            f"Error al finalizar la factura:\n`{exc}`\n\nRevisa los logs para más detalles.",
            parse_mode="Markdown",
        )
    finally:
        if draft_path and os.path.exists(draft_path):
            os.unlink(draft_path)


def run_bot() -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set in .env")

    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("ayuda", cmd_help))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice))
    app.add_handler(CallbackQueryHandler(handle_confirmation, pattern=r"^(send|cancel):"))

    logger.info("Bot started, waiting for messages...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)
