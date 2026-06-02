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


_config: CompanyConfig | None = None


def get_config() -> CompanyConfig:
    global _config
    if _config is None:
        _config = CompanyConfig()
    return _config
