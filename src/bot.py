import logging
import os
import tempfile
from pathlib import Path

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

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
    "• Generar la factura en PDF\n"
    "• Registrarla en Google Sheets\n"
    "• Enviarla por email al cliente\n\n"
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
    "El bot generará la factura, la registrará en Google Sheets y la enviará al cliente."
)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(_WELCOME, parse_mode="Markdown")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(_HELP, parse_mode="Markdown")


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    status_msg = await update.message.reply_text("Recibido. Descargando audio...")

    tmp_path: str | None = None
    pdf_path: str | None = None

    try:
        voice = update.message.voice or update.message.audio
        tg_file = await context.bot.get_file(voice.file_id)

        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
            tmp_path = tmp.name
        await tg_file.download_to_drive(tmp_path)

        await status_msg.edit_text("Transcribiendo audio con Whisper...")
        transcript = transcribe_audio(tmp_path)
        logger.info("Transcript: %s", transcript)

        await status_msg.edit_text(
            f"Transcripción:\n_{transcript}_\n\nExtrayendo datos de la factura...",
            parse_mode="Markdown",
        )

        invoice = parse_invoice_from_transcript(transcript)
        invoice.invoice_number = get_next_invoice_number()

        await status_msg.edit_text(
            f"Datos extraídos. Generando PDF para la factura *{invoice.invoice_number}*...",
            parse_mode="Markdown",
        )

        pdf_path = f"data/invoices/Factura_{invoice.invoice_number}.pdf"
        generate_invoice_pdf(invoice, pdf_path)

        await status_msg.edit_text("Registrando en Google Sheets...")
        add_invoice_to_sheet(invoice)

        await status_msg.edit_text(f"Enviando factura a *{invoice.client_email}*...", parse_mode="Markdown")
        send_invoice_email(invoice, pdf_path)

        subtotal = invoice.subtotal
        from src.config_loader import get_config
        config = get_config()
        total = round(subtotal * (1 + config.tax_rate / 100), 2)

        with open(pdf_path, "rb") as pdf_file:
            await update.message.reply_document(
                document=pdf_file,
                filename=f"Factura_{invoice.invoice_number}.pdf",
                caption=(
                    f"Factura *{invoice.invoice_number}* generada\n"
                    f"Cliente: {invoice.client_name}\n"
                    f"Total: {total:,.2f} {config.currency_symbol}\n"
                    f"Enviada a: {invoice.client_email}"
                ),
                parse_mode="Markdown",
            )

        await status_msg.delete()

    except Exception as exc:
        logger.error("Error processing invoice", exc_info=True)
        await status_msg.edit_text(
            f"Error al procesar la factura:\n`{exc}`\n\nRevisa los logs para más detalles.",
            parse_mode="Markdown",
        )
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)


def run_bot() -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set in .env")

    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("ayuda", cmd_help))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice))

    logger.info("Bot started, waiting for messages...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)
