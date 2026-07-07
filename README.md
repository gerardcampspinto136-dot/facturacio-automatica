# Facturación Automática — Invoice Bot

A Telegram bot that turns a voice message into a complete invoice:

1. Receives a voice message describing the invoice (client, services, hours, materials…)
2. Transcribes the audio with **Groq Whisper** (free) or **OpenAI Whisper**
3. Extracts structured data with **Claude (Anthropic)**
4. Generates a professional **PDF invoice draft** with your company branding
5. Shows you the draft for review — nothing is sent until you press **✅ Confirmar y enviar**
6. On confirmation: assigns the invoice number, logs it to **Google Sheets**, and emails the PDF to the client via **Gmail**

> The sequential invoice number is only consumed when you confirm, so cancelled drafts never leave gaps in your numbering.

---

## Quick start

### 1 — Install Python dependencies

```bash
py -m pip install -r requirements.txt
```

### 2 — Configure your company

Edit `config/company.yaml` with your company name, CIF, address, phone, email, and optionally place your logo at `config/logo.png`.

### 3 — Create `.env`

Copy `.env.example` to `.env` and fill in all values:

```bash
copy .env.example .env
```

| Variable | Where to get it |
|---|---|
| `TELEGRAM_BOT_TOKEN` | [@BotFather](https://t.me/BotFather) on Telegram |
| `GROQ_API_KEY` | [console.groq.com](https://console.groq.com/keys) — free, preferred for speech-to-text |
| `OPENAI_API_KEY` | [platform.openai.com](https://platform.openai.com/api-keys) — optional fallback if no Groq key |
| `ANTHROPIC_API_KEY` | [console.anthropic.com](https://console.anthropic.com/) |
| `GOOGLE_CREDENTIALS_PATH` | See step 4 below |
| `SPREADSHEET_ID` | From the Google Sheets URL |

### 4 — Set up Google OAuth2

1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Create a new project (or select an existing one)
3. Enable **Google Sheets API** and **Gmail API**
4. Go to **APIs & Services → Credentials → Create Credentials → OAuth 2.0 Client ID**
5. Choose **Desktop application**, download the JSON file
6. Save it as `config/credentials/google_credentials.json`
7. On first run the browser will open for authorisation — follow the prompts

### 5 — Run the bot

```bash
py main.py
```

---

## Customisation

### Company branding (`config/company.yaml`)

| Field | Description |
|---|---|
| `company.name` | Your company name (shown on invoice) |
| `company.cif` | CIF/NIF tax ID |
| `company.address` | Full address |
| `company.phone` | Phone number |
| `company.email` | Sender email (must match the authorised Gmail account) |
| `company.logo_path` | Path to your logo (PNG/JPG, ~300×100 px) |
| `invoice.tax_rate` | IVA percentage (default 21) |
| `invoice.bank_account` | IBAN shown at the bottom of the invoice |
| `email.subject_template` | Email subject (supports `{invoice_number}`, `{company_name}`) |
| `email.body_template` | Email body (supports `{client_name}`, `{invoice_number}`, `{total}`, `{company_name}`, `{company_phone}`, `{company_email}`) |

### What to say in the voice message

The bot understands Spanish, Catalan and English. Example:

> "Factura para María López, email maria@empresa.com, dirección Avenida Diagonal 10, Barcelona, NIF 12345678A.
> Le he hecho 5 horas de consultoría a 80 euros la hora y materiales por 150 euros."

---

## Project structure

```
.
├── config/
│   ├── company.yaml          # Edit this with your company details
│   ├── logo.png              # Your company logo (add manually)
│   └── credentials/          # Google OAuth files (gitignored)
├── data/
│   └── invoices/             # Generated PDFs (gitignored)
├── src/
│   ├── models.py             # InvoiceData and InvoiceItem dataclasses
│   ├── config_loader.py      # Loads company.yaml
│   ├── invoice_number.py     # Auto-incrementing invoice number
│   ├── transcription.py      # Groq / OpenAI Whisper STT
│   ├── parser.py             # Claude invoice data extractor
│   ├── invoice_generator.py  # ReportLab PDF builder
│   ├── google_auth.py        # Shared Google OAuth2 flow
│   ├── sheets.py             # Google Sheets logger
│   ├── email_sender.py       # Gmail sender
│   └── bot.py                # Telegram bot handlers
├── main.py                   # Entry point
├── requirements.txt
└── .env.example
```
