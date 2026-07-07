import base64
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional

from googleapiclient.discovery import build

from src.config_loader import get_config
from src.google_auth import get_credentials
from src.models import InvoiceData


def send_email(to: str, subject: str, body: str, pdf_path: Optional[str] = None,
               attachment_name: Optional[str] = None) -> None:
    """Send a plain-text email (optionally with a PDF attachment) via Gmail."""
    config = get_config()
    creds = get_credentials()
    service = build("gmail", "v1", credentials=creds)

    msg = MIMEMultipart()
    msg["To"] = to
    msg["From"] = config.email
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain", "utf-8"))

    if pdf_path:
        with open(pdf_path, "rb") as f:
            attachment = MIMEBase("application", "octet-stream")
            attachment.set_payload(f.read())
        encoders.encode_base64(attachment)
        attachment.add_header(
            "Content-Disposition",
            f'attachment; filename="{attachment_name or "adjunto.pdf"}"',
        )
        msg.attach(attachment)

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    service.users().messages().send(userId="me", body={"raw": raw}).execute()


def send_invoice_email(invoice: InvoiceData, pdf_path: str) -> None:
    config = get_config()

    subtotal = invoice.subtotal
    tax_amount = round(subtotal * config.tax_rate / 100, 2)
    total = round(subtotal + tax_amount, 2)

    subject = config.email_subject_template.format(
        invoice_number=invoice.invoice_number,
        company_name=config.name,
    )
    body = config.email_body_template.format(
        client_name=invoice.client_name,
        invoice_number=invoice.invoice_number,
        total=f"{total:,.2f} {config.currency_symbol}",
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
