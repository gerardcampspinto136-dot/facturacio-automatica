"""Sending email: the invoices to clients, reminders, the gestor's pack.

Two ways, chosen by what is configured:

  smtp   Any mail account: SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS (and SMTP_FROM).
         For Gmail that is smtp.gmail.com with an *app password*. It never expires,
         so this is the way to set up a client.
  gmail  The Gmail API with the OAuth token from authorize_google.py. Works, but while
         the Google Cloud app is in "Testing" Google revokes the token every 7 days --
         which silently stopped every invoice email once already.

EMAIL_BACKEND=smtp|gmail forces one; otherwise SMTP is used whenever SMTP_HOST is set.
"""

import base64
import mimetypes
import os
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from pathlib import Path
from typing import Optional

from src.config_loader import get_config
from src.models import InvoiceData
from src.totals import compute_totals, format_money


_TYPES = {
    ".pdf": "application/pdf",
    ".zip": "application/zip",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xml": "application/xml",
    ".xsig": "application/xml",
    ".csv": "text/csv",
}


def backend() -> str:
    forced = (os.getenv("EMAIL_BACKEND") or "").strip().lower()
    if forced in ("smtp", "gmail"):
        return forced
    return "smtp" if (os.getenv("SMTP_HOST") or "").strip() else "gmail"


def _smtp_password() -> str:
    return os.getenv("SMTP_PASS") or os.getenv("SMTP_PASSWORD") or ""


def _sender_address() -> str:
    if backend() == "smtp":
        return (os.getenv("SMTP_FROM") or os.getenv("SMTP_USER") or "").strip()
    return get_config().email


def build_message(to: str, subject: str, body: str,
                  attachments: Optional[list[tuple[str, str]]] = None) -> EmailMessage:
    """The email itself. `attachments` is [(path, filename), ...]."""
    config = get_config()
    msg = EmailMessage()
    sender = _sender_address()
    # The company's name as the sender, so the client sees who it is from; replies go
    # to the company's invoicing address even when a different account sends.
    msg["From"] = formataddr((config.name, sender)) if sender else config.name
    msg["To"] = to
    msg["Subject"] = subject
    msg["Message-ID"] = make_msgid(domain=(sender.split("@")[-1] if "@" in sender
                                           else None))
    if config.email and config.email.lower() != (sender or "").lower():
        msg["Reply-To"] = config.email
    msg.set_content(body)

    for path, filename in attachments or []:
        name = filename or os.path.basename(path)
        # The files this software sends, by their standard types: Windows' registry
        # otherwise decides (it calls a ZIP "application/x-zip-compressed").
        mime = _TYPES.get(Path(name).suffix.lower()) or mimetypes.guess_type(name)[0]
        maintype, subtype = (mime or "application/octet-stream").split("/", 1)
        msg.add_attachment(Path(path).read_bytes(), maintype=maintype, subtype=subtype,
                           filename=filename or os.path.basename(path))
    return msg


def _send_smtp(msg: EmailMessage) -> None:
    host = os.getenv("SMTP_HOST", "").strip()
    port = int(os.getenv("SMTP_PORT") or 587)
    user = os.getenv("SMTP_USER", "").strip()
    context = ssl.create_default_context()
    if port == 465:
        server = smtplib.SMTP_SSL(host, port, timeout=30, context=context)
    else:
        server = smtplib.SMTP(host, port, timeout=30)
        server.starttls(context=context)
    try:
        if user:
            server.login(user, _smtp_password())
        server.send_message(msg)
    finally:
        try:
            server.quit()
        except Exception:
            pass


def _send_gmail(msg: EmailMessage) -> None:
    from googleapiclient.discovery import build

    from src.google_auth import get_credentials

    service = build("gmail", "v1", credentials=get_credentials())
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    service.users().messages().send(userId="me", body={"raw": raw}).execute()


def send_email(to: str, subject: str, body: str, pdf_path: Optional[str] = None,
               attachment_name: Optional[str] = None) -> None:
    """Send a plain-text email, optionally with one attachment (PDF, ZIP, Excel...)."""
    attachments = [(pdf_path, attachment_name or os.path.basename(pdf_path))] \
        if pdf_path else []
    msg = build_message(to, subject, body, attachments)
    if backend() == "smtp":
        _send_smtp(msg)
    else:
        _send_gmail(msg)


def check_connection() -> tuple[bool, str]:
    """Can email be sent right now? Logs in (SMTP) or refreshes the token (Gmail),
    without sending anything. Returns (ok, detail)."""
    if backend() == "smtp":
        host = os.getenv("SMTP_HOST", "").strip()
        port = int(os.getenv("SMTP_PORT") or 587)
        try:
            context = ssl.create_default_context()
            if port == 465:
                server = smtplib.SMTP_SSL(host, port, timeout=20, context=context)
            else:
                server = smtplib.SMTP(host, port, timeout=20)
                server.starttls(context=context)
            server.login(os.getenv("SMTP_USER", "").strip(), _smtp_password())
            server.quit()
        except Exception as exc:
            return False, f"SMTP {host}: {exc}"
        return True, f"SMTP {host} como {_sender_address()}"

    try:
        from google.auth.transport.requests import Request

        from src.google_auth import load_credentials

        creds = load_credentials()
        if creds is None:
            return False, "No hay autorización de Google (ejecuta authorize_google.py)."
        if not creds.valid:
            creds.refresh(Request())
    except Exception as exc:
        return False, (f"La autorización de Gmail ha caducado o se ha revocado ({exc}). "
                       "Configura SMTP en .env o vuelve a ejecutar authorize_google.py.")
    return True, "Gmail API"


def send_invoice_email(invoice: InvoiceData, pdf_path: str) -> None:
    config = get_config()

    _, _, total = compute_totals(invoice, config)

    subject = config.email_subject_template.format(
        invoice_number=invoice.invoice_number,
        company_name=config.name,
    )
    body = config.email_body_template.format(
        client_name=invoice.client_name,
        invoice_number=invoice.invoice_number,
        total=format_money(total, config),
        company_name=config.name,
        company_phone=config.phone,
        company_email=config.email,
    )

    send_email(
        to=invoice.client_email,
        subject=subject,
        body=body,
        pdf_path=pdf_path,
        attachment_name=f"Factura_{invoice.invoice_number}.pdf",
    )
