import yaml
import os
from pathlib import Path


def _int(value) -> int:
    """A chat id that is blank, commented out or mistyped means "not configured"."""
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return 0


class CompanyConfig:
    def __init__(self, config_path: str = "config/company.yaml"):
        with open(config_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)

        company = data.get("company", {})
        self.name = company.get("name", "")
        self.cif = company.get("cif", "")
        self.address = company.get("address", "")
        self.phone = company.get("phone", "")
        self.email = company.get("email", "")
        self.logo_path = company.get("logo_path", "config/logo.png")

        # Shipped placeholders. The bot is installed once per client company, so the
        # dangerous state is an installation that was never filled in: invoices would go
        # out to real customers carrying a made-up CIF. Detected rather than trusted, so
        # it can be marked on the invoice itself.
        self.is_placeholder = (
            self.cif.replace(" ", "").upper() in ("B00000000", "B87654321", "")
            or "EMPRESA DE PRUEBA" in self.name.upper()
            or "EJEMPLO" in self.name.upper()
            or not self.name.strip()
        )

        invoice = data.get("invoice", {})
        self.tax_rate = invoice.get("tax_rate", 21)
        self.currency = invoice.get("currency", "EUR")
        self.currency_symbol = invoice.get("currency_symbol", "€")
        self.payment_terms = invoice.get("payment_terms", "30 días")
        self.bank_account = invoice.get("bank_account", "")
        # True  -> a dictated price is assumed to already contain VAT
        # False -> VAT is added on top (the usual B2B convention)
        # Saying "IVA incluido" or "más IVA" out loud overrides this per invoice.
        self.prices_include_tax = bool(invoice.get("prices_include_tax", False))
        # Fields an invoice must have before it can be issued.
        required = data.get("required_fields", {}) or {}
        self.require_email = bool(required.get("client_email", True))
        self.require_tax_id = bool(required.get("client_id", True))
        self.require_address = bool(required.get("client_address", False))

        email_cfg = data.get("email", {})
        self.email_subject_template = email_cfg.get(
            "subject_template", "Factura {invoice_number} - {company_name}"
        )
        self.email_body_template = email_cfg.get("body_template", "")

        # ── Review workflow ──────────────────────────────────────────────────
        review = data.get("review", {}) or {}
        # "auto"   → send to the client immediately.
        # "manual" → queue for review on the web page before sending.
        self.review_mode = review.get("mode", "manual")
        self.reviewers = review.get("reviewers", []) or []

        notify = review.get("notify", {}) or {}
        self.notify_channels = notify.get("channels", ["telegram"]) or []
        self.notify_schedule = str(notify.get("schedule", "1d"))
        self.notify_email = notify.get("email", "")
        # The chat id identifies a personal Telegram account, and company.yaml is
        # committed, so .env wins over the file and the file can be left empty.
        self.notify_telegram_chat_id = _int(
            os.getenv("TELEGRAM_CHAT_ID") or notify.get("telegram_chat_id", 0)
        )

        # ── Money and stock alerts ───────────────────────────────────────────
        alerts = data.get("alerts", {}) or {}
        # Empty string / null turns a job off entirely.
        self.money_schedule = alerts.get("money_schedule", "1w") or ""
        self.stock_schedule = alerts.get("stock_schedule", "1w") or ""
        self.bills_due_within_days = int(alerts.get("bills_due_within_days", 7))

        web = review.get("web", {}) or {}
        self.web_base_url = str(web.get("base_url", "http://localhost:8000")).rstrip("/")
        self.web_host = web.get("host", "127.0.0.1")
        self.web_port = int(web.get("port", 8000))


_config: CompanyConfig | None = None


def get_config() -> CompanyConfig:
    global _config
    if _config is None:
        _config = CompanyConfig()
    return _config
