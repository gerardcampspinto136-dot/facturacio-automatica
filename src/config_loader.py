import yaml
import os
from pathlib import Path


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

        invoice = data.get("invoice", {})
        self.tax_rate = invoice.get("tax_rate", 21)
        self.currency = invoice.get("currency", "EUR")
        self.currency_symbol = invoice.get("currency_symbol", "€")
        self.payment_terms = invoice.get("payment_terms", "30 días")
        self.bank_account = invoice.get("bank_account", "")

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
        self.notify_telegram_chat_id = notify.get("telegram_chat_id", 0)

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
