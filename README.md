# Facturación Automática — Invoice Bot

A Telegram bot that turns a voice message into a complete invoice:

1. Receives a voice message describing the invoice (client, services, hours, materials…)
2. Transcribes the audio with **Groq Whisper** (free) or **OpenAI Whisper**
3. Extracts structured data with **Claude (Anthropic)**
4. Generates a professional **PDF invoice** with your company branding
5. Either sends it immediately or holds it for review (see **Review modes** below)
6. Logs it to **Google Sheets** and emails the PDF to the client via **Gmail**

## Review modes

Set `review.mode` in `config/company.yaml`:

- **`auto`** — the invoice is generated and **sent to the client immediately**, at your own risk.
  If something is wrong, cancel it afterwards with a contra invoice (`/anular <número>`).
- **`manual`** (default) — each invoice is held in a **pending queue** with a draft PDF; the
  sequential number is **not** consumed yet (so cancelled drafts never leave gaps). On a schedule you
  choose (`review.notify.schedule`: `1d`, `3d`, `1w`, `2w…`) the reviewer gets a **Telegram and/or
  email** notification with a link to a **web page** where they sign in with their Google account and
  **approve, edit, or reject** each invoice. Only on approval is the number assigned, the invoice
  logged, and the email sent.

### Contra / rectifying invoices (facturas rectificativas)

To cancel an already-sent invoice, send `/anular <número>` in Telegram (or use the **Anular** button
on the web *Emitidas* page). This issues a rectifying invoice in its own `R-` series with negated
amounts, logs it, and emails the client.

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

### 5 — (Manual mode only) Set up the web review page

The review page authenticates reviewers with **Google Sign-In**:

1. In [Google Cloud Console](https://console.cloud.google.com/) → **Credentials → Create Credentials
   → OAuth 2.0 Client ID**, choose **Web application** (this is separate from the Desktop client in
   step 4).
2. Under **Authorized redirect URIs** add `{base_url}/auth/callback` (e.g.
   `http://localhost:8000/auth/callback`).
3. Put the client id/secret in `.env` as `WEB_OAUTH_CLIENT_ID` / `WEB_OAUTH_CLIENT_SECRET`, and set a
   long random `SESSION_SECRET`.
4. List the allowed reviewer emails under `review.reviewers` in `config/company.yaml`.

**Reviewing from your phone:** the local server must be reachable at a public URL. Run a tunnel, e.g.
`cloudflared tunnel --url http://localhost:8000`, then set `review.web.base_url` (and the OAuth
redirect URI) to that public URL.

> For quick local testing you can set `WEB_DEV_NO_AUTH=1` in `.env` to skip Google login. **Never use
> this in production.**

### 6 — Run the bot

```bash
py main.py
```

In `manual` mode this also starts the web review app (`review.web.host:port`) and the reminder
scheduler. In `auto` mode only the Telegram bot runs.

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
│   ├── email_sender.py       # Gmail sender (send_email + send_invoice_email)
│   ├── store.py              # Pending queue + issued-invoice records (JSON)
│   ├── finalize.py           # Shared: assign number → PDF → Sheets → email → record
│   ├── rectify.py            # Contra / rectifying invoices
│   ├── notify.py             # Reviewer reminders (Telegram/email)
│   ├── scheduler.py          # Batched pending-invoice reminders
│   ├── web/app.py            # FastAPI review page (Google login)
│   └── bot.py                # Telegram bot handlers
├── main.py                   # Entry point (bot + web + scheduler)
├── requirements.txt
└── .env.example
```
