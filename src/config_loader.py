import logging
import os

import yaml
from pathlib import Path


def _int(value) -> int:
    """A chat id that is blank, commented out or mistyped means "not configured"."""
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return 0


def _series(value) -> str:
    """A numbering series, or "" when it is blank or unusable (never a crash)."""
    from src.invoice_number import clean_series

    try:
        return clean_series(value)
    except ValueError:
        logging.getLogger(__name__).warning("Ignoring unusable invoice series %r", value)
        return ""


def days_from_terms(terms: str) -> int:
    """Days to pay, read from the payment terms printed on the invoice.

    "30 días" -> 30, "60 dias fecha factura" -> 60, "Al contado" -> 0. Anything else
    falls back to 30, the legal default between businesses in Spain.
    """
    import re

    text = (terms or "").lower()
    if any(word in text for word in ("contado", "inmediato", "a la vista")):
        return 0
    match = re.search(r"\d+", text)
    return int(match.group()) if match else 30


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
        self.company_id = None
        self.is_placeholder = self._looks_like_placeholder()

        invoice = data.get("invoice", {})
        self.tax_rate = invoice.get("tax_rate", 21)
        # IRPF withheld by the client on every invoice, unless said otherwise: 15 for
        # a professional (7 in their first years), 0 for everyone else.
        self.irpf_rate = float(invoice.get("irpf_rate", 0) or 0)
        # A business whose activity is VAT-exempt (tax_rate 0): why, printed on every
        # invoice. A key of src/exemptions.REASONS, e.g. "exempt".
        self.vat_reason = invoice.get("vat_reason") or None
        self.currency = invoice.get("currency", "EUR")
        self.currency_symbol = invoice.get("currency_symbol", "€")
        self.payment_terms = invoice.get("payment_terms", "30 días")
        self.bank_account = invoice.get("bank_account", "")
        # Numbering series: "" -> 2026-0001, "A" -> A-2026-0001.
        self.invoice_series = _series(invoice.get("series", ""))
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
        # The hour of the morning the reminders go out (never after 21:00).
        self.alert_hour = min(max(int(alerts.get("hour", 9) or 9), 0), 20)

        # ── Chasing unpaid invoices ──────────────────────────────────────────
        collections = data.get("collections", {}) or {}
        # "ask": the owner is asked on Telegram before each reminder goes out;
        # "auto": they go out on their own; "off": never.
        mode = str(collections.get("mode", "ask") or "ask").strip().lower()
        self.collections_mode = mode if mode in ("ask", "auto", "off") else "ask"
        self.collections_first_after = int(collections.get("first_after_days", 3))
        self.collections_every = max(int(collections.get("repeat_every_days", 7)), 1)
        self.collections_max = max(int(collections.get("max_reminders", 3)), 1)
        self.collections_subject = collections.get(
            "subject_template", "Recordatorio de pago — factura {invoice_number}")
        self.collections_body = collections.get("body_template", "") or ""

        # ── Backups ──────────────────────────────────────────────────────────
        backup = data.get("backup", {}) or {}
        self.backup_keep = max(int(backup.get("keep", 30) or 30), 1)
        # A folder on THIS machine (OneDrive, a USB disk) belongs in .env, which is
        # per-machine, rather than in this file, which is the shared template.
        self.backup_copy_to = (os.getenv("BACKUP_COPY_TO")
                               or str(backup.get("copy_to", "") or ""))
        email = os.getenv("BACKUP_EMAIL", backup.get("email", "auto"))
        self.backup_email = "" if email is None else str(email)

        # ── Quotes ───────────────────────────────────────────────────────────
        self.quote_validity_days = int((data.get("quotes", {}) or {}).get(
            "validity_days", 30) or 30)

        # ── The gestor ───────────────────────────────────────────────────────
        # Who receives the quarter's pack (invoice books, PDFs, receipts).
        self.gestor_email = str((data.get("gestor", {}) or {}).get("email", "") or "")

        # ── Verifactu ────────────────────────────────────────────────────────
        verifactu = data.get("verifactu", {}) or {}
        # The QR the AEAT requires at the top of every invoice.
        self.verifactu_qr = bool(verifactu.get("qr", True))
        # "test" / "production"; blank decides from whether the company is configured.
        self.verifactu_environment = str(verifactu.get("environment", "") or "")

        web = review.get("web", {}) or {}
        self.web_base_url = str(web.get("base_url", "http://localhost:8000")).rstrip("/")
        self.web_host = web.get("host", "127.0.0.1")
        self.web_port = int(web.get("port", 8000))


    # ── Settings entered in the admin panel ──────────────────────────────────

    def apply_company(self, company: dict) -> "CompanyConfig":
        """Overlay a client's settings from the database onto the file defaults.

        The YAML stays as the shipped default; anything filled in for the company in
        the admin panel wins. That is what makes the panel real rather than a form that
        writes to a table nobody reads -- and it means preparing a client never requires
        editing a file on their machine.

        Only non-empty values override, so a half-filled company still produces a
        working invoice using the defaults for whatever is missing.
        """
        def take(key, attr=None, cast=None):
            value = company.get(key)
            if value is None or (isinstance(value, str) and not value.strip()):
                return
            setattr(self, attr or key, cast(value) if cast else value)

        take("name")
        take("tax_id", "cif")
        take("address")
        take("phone")
        take("invoice_email", "email")
        take("gestor_email")
        if company.get("collections_mode") in ("ask", "auto", "off"):
            self.collections_mode = company["collections_mode"]
        take("iban", "bank_account")
        take("tax_rate", cast=float)
        take("irpf_rate", cast=float)
        take("vat_reason")
        take("payment_terms")
        take("invoice_series", cast=_series)
        take("logo_path")
        take("review_mode")
        take("telegram_chat_id", "notify_telegram_chat_id", cast=_int)
        if company.get("prices_include_tax") is not None:
            self.prices_include_tax = bool(company["prices_include_tax"])

        self.company_id = company.get("id")
        self.is_placeholder = self._looks_like_placeholder()
        return self

    @property
    def payment_days(self) -> int:
        """Days until an invoice falls due, from the payment terms printed on it."""
        return days_from_terms(self.payment_terms)

    def _looks_like_placeholder(self) -> bool:
        return (
            self.cif.replace(" ", "").upper() in ("B00000000", "B87654321", "")
            or "EMPRESA DE PRUEBA" in self.name.upper()
            or "EJEMPLO" in self.name.upper()
            or not self.name.strip()
        )


_config: CompanyConfig | None = None


def get_config() -> CompanyConfig:
    """The settings in force right now.

    Built from config/company.yaml, then overlaid with the active client's settings from
    the admin panel when there is exactly one active company. The database lookup is
    deliberately forgiving: this is called from the PDF builder and the bot, and a
    missing or half-migrated database must not stop an invoice being produced.
    """
    global _config
    if _config is None:
        config = CompanyConfig()
        try:
            from src import accounts

            company = accounts.active_company()
            if company:
                config.apply_company(company)
        except Exception:  # pragma: no cover - defensive
            logging.getLogger(__name__).debug(
                "No company settings available; using config/company.yaml", exc_info=True
            )
        _config = config
    return _config


def reload_config() -> CompanyConfig:
    """Forget the cached settings, so a change in the panel takes effect."""
    global _config
    _config = None
    return get_config()
