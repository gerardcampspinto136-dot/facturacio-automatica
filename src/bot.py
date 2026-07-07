import logging
import os
import tempfile

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from src import store
from src.config_loader import get_config
from src.finalize import finalize_invoice
from src.invoice_generator import generate_invoice_pdf
from src.parser import parse_invoice_from_transcript
from src.rectify import create_rectifying_invoice
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
    "• Generar la factura en PDF\n"
    "• Registrarla en Google Sheets\n"
    "• Enviarla por email al cliente\n\n"
    "Según tu configuración, la factura se envía al momento (modo automático) "
    "o queda pendiente de revisión en la web (modo manual).\n\n"
    "Comandos:\n"
    "• /ayuda — ejemplo de mensaje de voz\n"
    "• /anular <número> — emitir una factura rectificativa que anula una factura ya emitida"
)

_HELP = (
    "Ejemplo de mensaje de voz:\n\n"
    "_\"Factura para Juan García, email juan@ejemplo.com, dirección Calle Mayor 5 Madrid, "
    "DNI doce tres cuatro cinco seis siete ocho A. "
    "Le he hecho tres horas de trabajo a cincuenta euros la hora "
    "y materiales por cien euros.\"_\n\n"
    "Para anular una factura ya emitida:\n"
    "`/anular 2026-0007`"
)


def _totals(invoice, config) -> tuple[float, float, float]:
    subtotal = invoice.subtotal
    tax_amount = round(subtotal * config.tax_rate / 100, 2)
    total = round(subtotal + tax_amount, 2)
    return subtotal, tax_amount, total


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
        _, _, total = _totals(rectifying, config)
        with open(pdf_path, "rb") as pdf_file:
            await update.message.reply_document(
                document=pdf_file,
                filename=f"Factura_{rectifying.invoice_number}.pdf",
                caption=(
                    f"✅ Factura rectificativa *{rectifying.invoice_number}*\n"
                    f"Anula la factura {number}\n"
                    f"Importe: {total:,.2f} {config.currency_symbol}"
                ),
                parse_mode="Markdown",
            )
        await status.delete()
    except ValueError as exc:
        await status.edit_text(f"⚠️ {exc}")
    except Exception as exc:
        logger.error("Error creating rectifying invoice", exc_info=True)
        await status.edit_text(f"Error al anular la factura:\n`{exc}`", parse_mode="Markdown")


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

        config = get_config()
        if config.review_mode == "auto":
            await _handle_auto(update, status_msg, invoice, config)
        else:
            await _handle_manual(update, status_msg, invoice, config)

    except Exception as exc:
        logger.error("Error processing invoice", exc_info=True)
        await status_msg.edit_text(
            f"Error al procesar la factura:\n`{exc}`\n\nRevisa los logs para más detalles.",
            parse_mode="Markdown",
        )
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)


async def _handle_auto(update, status_msg, invoice, config) -> None:
    await status_msg.edit_text("Modo automático: generando y enviando la factura...")
    pdf_path = finalize_invoice(invoice)  # assigns number, logs, emails
    _, _, total = _totals(invoice, config)

    if invoice.client_email:
        sent_line = f"Enviada a: {invoice.client_email}"
    else:
        sent_line = "No enviada por email (falta el email del cliente)"

    with open(pdf_path, "rb") as pdf_file:
        await update.message.reply_document(
            document=pdf_file,
            filename=f"Factura_{invoice.invoice_number}.pdf",
            caption=(
                f"✅ Factura *{invoice.invoice_number}* generada\n"
                f"Cliente: {invoice.client_name}\n"
                f"Total: {total:,.2f} {config.currency_symbol}\n"
                f"{sent_line}"
            ),
            parse_mode="Markdown",
        )
    await status_msg.delete()


async def _handle_manual(update, status_msg, invoice, config) -> None:
    await status_msg.edit_text("Generando borrador de la factura...")

    # No invoice number is consumed yet — it is assigned on approval, keeping the
    # sequential numbering gap-free.
    invoice.invoice_number = "BORRADOR"
    import uuid

    draft_path = f"data/invoices/_borrador_{uuid.uuid4().hex[:8]}.pdf"
    generate_invoice_pdf(invoice, draft_path)
    store.add_pending(invoice, draft_path)

    pending = store.count_pending()
    _, _, total = _totals(invoice, config)

    with open(draft_path, "rb") as pdf_file:
        await update.message.reply_document(
            document=pdf_file,
            filename="Borrador_factura.pdf",
            caption=(
                f"📥 Guardada para revisión ({pending} pendiente"
                f"{'s' if pending != 1 else ''})\n"
                f"Cliente: {invoice.client_name}\n"
                f"Total: {total:,.2f} {config.currency_symbol}\n\n"
                f"Revísala y envíala desde:\n{config.web_base_url}"
            ),
        )
    await status_msg.delete()


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
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice))

    logger.info("Bot started, waiting for messages...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)
