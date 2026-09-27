# Facturación Automática — Invoice Bot

A Telegram bot that turns a voice message into a complete invoice:

1. Receives a voice message describing the invoice (client, services, hours, materials…)
2. Transcribes the audio with **Groq Whisper** (free) or **OpenAI Whisper**
3. Extracts structured data with **Groq** (free) or **Claude (Anthropic)**
4. Generates a professional **PDF invoice** with your company branding
5. Either sends it immediately or holds it for review (see **Review modes** below)
6. Logs it to **Google Sheets** and emails the PDF to the client via **Gmail**

It also tracks the money going the other way: photograph a supplier invoice or a till
receipt and it is read and filed as an expense (see **Expenses** below).

## The bot is private

The bot only answers people it knows:

- **The owner's chat** — `TELEGRAM_CHAT_ID` in `.env`, or *Chat de avisos* in the admin
  panel. Full access. Send `/chatid` to the bot to find the number.
- **Anyone who connected their Telegram** from the web panel (*Mi cuenta → Conectar
  Telegram*, or the owner does it for them from *Equipo*). They get a one-time link — or a QR
  to scan with the phone — and from then on the bot applies **exactly their account's
  permissions**: an employee who may only see stock cannot list clients or read `/pagos`.

Everyone else gets *"Este asistente es privado"* and the owner is told, once, who tried.

## Who may send an invoice

Decided **per person**, by the permission *Aprobar y enviar facturas* (Equipo):

- **With it** — after checking the summary in the chat, *✅ Enviar* issues and emails it.
  *💾 Guardar sin enviar* keeps it in `/pendientes` for later.
- **Without it** — the button is *📤 Mandar a revisión*. The draft goes to everyone who
  can approve, **as a PDF on their phone with ✅ Aprobar / ❌ Descartar buttons**; whoever
  prepared it is told the outcome. It can also be approved from the web (*Pendientes*).

A draft consumes **no number** until it is approved, so discarded drafts never leave gaps.

## Everything the bot understands

| Say or send | What happens |
|---|---|
| an audio or a text: *«factura para…»* | an invoice: it asks for anything missing, shows it, and sends it on *Enviar* |
| *«presupuesto para…»* | a quote; *Aceptado → facturar* turns it into the invoice |
| *«con retención del 15%»*, *«IVA del 10%»* | withholding / VAT rate for that invoice |
| a photo or PDF of a ticket or supplier invoice | an expense, with its deductible VAT |
| *«¿cuánto he facturado este mes?»*, *«¿quién me debe?»* | the answer, added up from the books (never guessed by the model) |
| a bank statement (Norma 43, CSV, Excel) | payments matched to invoices and bills; see *Bank statements* |
| `/fichar` · `/jornada` | clock in, break, clock out · today's and this month's hours |
| `/pendientes` | drafts waiting for approval, with *Aprobar / Descartar* |
| `/factura [n]` · `/reenviar n` · `/anular n motivo` | an invoice's PDF · email it again · cancel it |
| `/xml n` · `/xml n ubl` | the invoice as Facturae (for FACe) · as UBL (EN 16931) |
| `/cobrada [n]` · `/recordar n` | mark paid (alone: list with buttons) · send a payment reminder |
| `/recurrentes` · `/presupuestos` | invoices that repeat · quotes waiting for an answer |
| `/trimestre [T año]` | the quarter's IVA (303) and IRPF (130), and the pack for the gestor |
| `/pagos` · `/clientes` · `/stock` | who owes what · stored clients · what is in stock |
| `/producto` `/entrada` `/salida` `/inventario` | catalog and stock movements |

Under every invoice issued there is also *🔁 Repetir cada mes*.

## Quarterly taxes and the gestor

Everything needed for the quarter's returns is already recorded, so the software adds
it up:

- **Modelo 303 (IVA)** — VAT charged by rate, minus the deductible VAT on the expenses
  recorded, and the result. Expenses entered without their VAT are pointed out: that is
  deductible VAT being lost.
- **Modelo 130 (IRPF)** — for a person (NIF, not CIF): the running totals from January,
  20 %, minus earlier instalments and the IRPF clients withheld. A company is told it
  files the 202 instead.
- **The pack for the gestor** — one ZIP: the two VAT record books (issued and received
  invoices) in Excel with totals as formulas, the PDF of every invoice, and the photo of
  every receipt. Download it from *Impuestos* in the panel, get it in the chat with
  `/trimestre`, or send it straight to the gestor's email (set in the company settings).
- **The calendar** — when a filing window opens (1–20 April/July/October, 1–30
  January) the owner gets the figures and the pack button; five days before the
  deadline, if nothing has gone to the gestor yet, a last reminder.

The figures are an estimate from what was recorded: the gestor reviews and files.

## Verifactu

Mandatory for this software's clients from **1 January 2027** (companies) and **1 July
2027** (autónomos). What is in place:

- **A chained register.** Every invoice issued gets a billing record with its SHA-256
  fingerprint (*huella*), computed together with the previous record's — exactly as the
  AEAT's technical specification describes, and tested against the AEAT's own worked
  examples. The record is written in the same transaction as the invoice.
- **Nothing can be rewritten.** Issued invoices and their records cannot be edited or
  deleted (database triggers); a mistake is corrected with `/anular` (a rectifying
  invoice, registered as `R1`).
- **The QR code** at the top of every invoice, 35 mm, headed *QR tributario:*.
- **A check**: the panel's *Verifactu* page and `verificar.py` recompute every
  fingerprint and link, and say whether anything was altered.

**Not yet built: sending the records to the AEAT** (the *VERI\*FACTU* mode). It needs
each client's digital certificate to test with. Until then the system is *no
VERI\*FACTU*: the QR points at `ValidarQRNoVerifactu`, and the "VERI\*FACTU" legend —
which asserts the record reached the AEAT — is not printed.

### Contra / rectifying invoices (facturas rectificativas)

To cancel an already-sent invoice, send `/anular <número>` in Telegram (or use the **Anular** button
on the web *Emitidas* page). This issues a rectifying invoice in its own `R-` series with negated
amounts, logs it, and emails the client.

## Electronic invoices: FACe and the B2B obligation

- **Public bodies (FACe).** A town hall, a regional ministry or a university only pays
  invoices that come in through FACe as Facturae XML, addressed with the body's three
  DIR3 codes. Enter them once on the client's record (*Clientes → Administración
  pública (FACe)*). From then on, every invoice to that client arrives in the chat with
  its Facturae file right after the PDF, and the steps: sign it with **AutoFirma** (the
  government's free signing app) and upload the `.xsig` at face.gob.es. Any invoice's
  file: `/xml <número>`, or the *XML ▾* menu on *Emitidas*.
- **Between businesses (RD 238/2026).** Electronic invoicing becomes compulsory for
  B2B in October 2027 (turnover above 8 M€) and October 2028 (everyone else), in a
  syntax of the EN 16931 model; the AEAT's free public solution takes UBL. `/xml <n>
  ubl` produces it today. IRPF withholding, which EN 16931 has no place for, travels
  as an amount already settled plus UBL's `WithholdingTaxTotal`; the ministerial order
  with the technical details may settle it differently (`src/einvoice.py`).
- **Checked against the real schemas.** The tests validate every variant (plain, with
  IRPF, rectifying, public body, self-employed client, VAT-exempt) against the official
  Facturae 3.2.2 XSD and OASIS's UBL 2.1 XSDs, and check the EN 16931 sums.
- **Not built: the XAdES signature.** It needs each client's certificate; AutoFirma
  does it in one drag and drop meanwhile.

## Bank statements

Upload the statement on the panel's *Banco* page, or send the file to the bot. Norma 43
(*Cuaderno 43*) and the CSV/Excel export of any bank are read, whatever the columns are
called. Money in with the same amount and the invoice number or client's name in the
text marks that invoice paid by itself; the same amount alone is proposed for a human
to confirm. Payments out are matched to supplier bills the same way, and a charge with
nothing behind it (the phone, the insurance, bank fees) is one click from being filed
as an expense. Importing a statement twice adds nothing.

## Working-time record (registro de jornada)

Required for every company with employees (art. 34.9 Estatuto de los Trabajadores).
Each employee links their Telegram account and presses `/fichar`: entry, breaks and
exit, stamped with the server's time, chained by SHA-256 and impossible to edit or
delete. A forgotten exit is fixed by an added, signed correction with its reason, and
at 20:00 anyone still clocked in gets a reminder. The panel's *Registro de jornada*
page shows each person's days (employees see only their own) and prints the monthly
PDF to sign.

---

## Checking it all works

```
py verificar.py          # everything, including Telegram / Groq / Google
py verificar.py --rapido # skips anything that needs internet
```

Or double-click **`Comprobar que todo funciona.bat`**. It exercises the real thing —
VAT both ways, gap-free numbering, stock deducted from a dictated invoice line, supplier
bills, receipt arithmetic, a generated PDF, and that every web page demands a login —
on a throwaway database, so it never touches real data. Each failure says what to do.

For development, the full test suite:

```
py -m pip install -r requirements-dev.txt
py -m pytest -q
```

## Running it

| | |
|---|---|
| **`Iniciar bot.bat`** | the Telegram bot + the web app + reminders |
| **`Panel de administracion.bat`** | just the web panel, and opens it in the browser |
| **`Comprobar que todo funciona.bat`** | the checks above |

---

## Accounts, roles and permissions

Three kinds of account, because the people using this are not all the same person:

| Role | Who | Can |
|---|---|---|
| **Proveedor** (superadmin) | you, the vendor | Create client companies and their first owner; suspend a company's access |
| **Responsable** (admin) | the client's owner | Everything inside their own company, including creating and revoking their staff's accounts |
| **Empleado** | the client's staff | Only what they have been granted, one permission at a time |

Nobody hands out passwords: everyone signs in with their **Google account**, and an
account is the email Google hands back. When someone leaves, their account is
deactivated and they stop getting in on their next click — nothing they did is lost.

**The vendor panel** (`/admin`) lists every client company with its accounts and last
login, creates a company together with its first owner, and suspends a client without
deleting anything — a customer who stops paying keeps their records and can be switched
back on.

**The team panel** (`/team`) is the client's own: their owner adds staff and ticks what
each one may do, grouped as Facturas / Cobros / Proveedores / Stock / Clientes /
Administración. New staff start read-mostly — approving and sending invoices is never
granted by default. The navigation only shows tabs an account can actually open, and a
refusal names the missing permission rather than showing a bare 403.

Two things are deliberately impossible: **editing your own account** (which is how people
remove their own admin rights and lock everyone out), and **removing the last active
owner** of a company.

### First run

Put your own address in `SUPERADMIN_EMAILS` in `.env`. The first time you sign in with
it, your vendor account is created automatically — otherwise the panel would be
unreachable, since creating an account needs the panel.

```
SUPERADMIN_EMAILS=tu-email@gmail.com
```

### One company per installation, for now

Accounts and permissions are per-company, but invoices, supplier bills and stock are
**not yet** — there is one set of records in the database. That matches how the software
is sold (one installation per client), and while a single company is active everything
works normally.

If a second company is ever made active on the same installation, the books close with
an explanation instead of showing one client another client's data. Separating the
records by company is the work that would turn this into true shared hosting.

### Setting a client up from the panel

`/admin` → **⚙️ Configurar empresa y su bot** holds everything needed to put a client
live, so preparing one never means editing a file on their machine:

- **Datos fiscales** — name, CIF, address, phone, invoicing email, IBAN. These print on
  every invoice, and until name + CIF + address are all present the company is listed as
  *sin configurar* and its invoices carry the `DOCUMENTO DE PRUEBA` banner.
- **Facturación** — default VAT rate, payment terms, numbering series, whether dictated
  prices include VAT, and whether invoices wait for approval or send immediately.
- **Su bot de Telegram** — each client has their **own** bot with their own name. Create
  it in Telegram with `@BotFather` (`/newbot`), paste the token here, and that company is
  connected to that bot. The company list shows `bot ✓` once it is.
- **Marca** — logo path, internal contact email and notes.

Saving takes effect immediately; nothing needs restarting. Settings entered here override
`config/company.yaml`, which stays as the shipped default for anything left blank.

### Where things are

| | URL |
|---|---|
| What the client's staff see | `http://localhost:8000/` |
| The client owner's team panel | `http://localhost:8000/team` |
| Your vendor panel | `http://localhost:8000/admin` |

These run on the machine the bot runs on. To let a client reach it from their own office
or phone, it has to be deployed somewhere with a real domain (or exposed through a tunnel
for testing), and `review.web.base_url` updated to match.

---

## Preparing it for a client company

The bot is installed once per company. Everything that differs between clients lives in
**`config/company.yaml`** — their fiscal details, VAT rate, IBAN, who may approve
invoices — plus their own keys in `.env`.

Until those details replace the ones shipped in the repo, **every invoice is stamped
`DOCUMENTO DE PRUEBA — SIN VALOR FISCAL`** across the top and the bot says so on
startup. That is deliberate: an installation nobody finished configuring cannot quietly
send a real customer an invoice carrying a made-up CIF.

Checklist per client:

1. `py setup_wizard.py` — writes their `.env` (Telegram token, Groq key, Google, sheet id)
2. Fill in `config/company.yaml`: name, CIF, address, phone, email, IBAN, VAT rate
3. `config/logo.png` — drop in their artwork, or run `py generate_logo.py` to get a
   placeholder built from the details in step 2
4. Accounts for their staff — the owner adds them in `/team`, and each one connects their Telegram from *Mi cuenta*
5. Issue one invoice and confirm the red PRUEBA banner is gone

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
| `TELEGRAM_CHAT_ID` | Send `/chatid` to your own bot. Kept here, not in `company.yaml`, because that file is committed |
| `GROQ_API_KEY` | [console.groq.com](https://console.groq.com/keys) — free, preferred for speech-to-text |
| `OPENAI_API_KEY` | [platform.openai.com](https://platform.openai.com/api-keys) — optional fallback if no Groq key |
| `ANTHROPIC_API_KEY` | [console.anthropic.com](https://console.anthropic.com/) — optional; Groq alone runs the whole bot for free |
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

### 5 — Set up the web panel's Google sign-in

The review page authenticates reviewers with **Google Sign-In**:

1. In [Google Cloud Console](https://console.cloud.google.com/) → **Credentials → Create Credentials
   → OAuth 2.0 Client ID**, choose **Web application** (this is separate from the Desktop client in
   step 4).
2. Under **Authorized redirect URIs** add `{base_url}/auth/callback` (e.g.
   `http://localhost:8000/auth/callback`).
3. Put the client id/secret in `.env` as `WEB_OAUTH_CLIENT_ID` / `WEB_OAUTH_CLIENT_SECRET`, and set a
   long random `SESSION_SECRET`.
4. Who may sign in is decided by the accounts in the panel (`/admin`, `/team`), not by a list in a file.

**Reviewing from your phone:** the local server must be reachable at a public URL. Run a tunnel, e.g.
`cloudflared tunnel --url http://localhost:8000`, then set `review.web.base_url` (and the OAuth
redirect URI) to that public URL.

> For quick local testing you can set `WEB_DEV_NO_AUTH=1` in `.env` to skip Google login. **Never use
> this in production.**

### 6 — Run the bot

```bash
py main.py
```

This starts the Telegram bot, the web panel (`review.web.host:port`) and the scheduler that
sends the reminders, the quarterly tax calendar, recurring invoices and payment reminders.

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

## Expenses — photograph the receipt

Send the bot a **photo** of a supplier invoice or a till receipt. It reads the supplier,
their CIF, the document number, the date, the base, the VAT and the total, shows you
what it read, and files it as a bill only once you confirm.

- Add a caption to give it context: *"comida con cliente"* sets the category and the note.
- Card and cash tickets are filed as already paid. An invoice with a due date stays
  pending and shows up in `/pagos` and in the weekly money digest.
- Anything it cannot read it **asks for** rather than guessing — a wrong total is worse
  than a question. A figure that contradicts the total is flagged, not silently fixed.
- The photo itself is kept in `data/receipts/<year>/`, because an expense without the
  document behind it is not deductible.

`/gasto Ferretería Puig 242,50 F-2026/88` still works for typing one in without a photo.

The vision model is Groq's `qwen/qwen3.8-27b` (free). Override with `GROQ_VISION_MODEL`,
or set `ANTHROPIC_API_KEY` to use Claude instead.

---

## Stock — entered once, discounted automatically

Add what you sell, and the bot takes it off the shelf every time you invoice it.

```
/producto Tornillos M8 0,25 100     name, sale price, how many you have now
/producto Mano de obra 45           no quantity → a service, no stock kept
/entrada Tornillos M8 50            material arrived
/salida Tornillos M8 3              broken, or used on your own job
/inventario Tornillos M8 87         match the shelf after a count
/stock                              what you have, short items first
/producto                           the whole catalog
```

When an invoice line matches a catalog product the stock is moved automatically and the
bot tells you what is left — *"📦 Tornillos M8: −20 → quedan 80 ud"* — and flags anything
that has reached its reorder point. Cancelling an invoice with `/anular` puts the stock
back.

**You do not have to say the catalogue name exactly.** *"20 tornillos M8"*, *"3 brocas
widia de 10mm"* and *"un tornillo M8"* all find their product: matching ignores accents,
plurals, filler words and the order of the words, and glues *"10 mm"* back into *"10mm"*.
Where two products are equally good candidates it deducts nothing rather than guess —
*"brocas widia"* with both an 8mm and a 10mm on file is left alone.

Every change is written to `stock_moves` with the invoice number behind it, so a level
that looks wrong can always be traced back.

---

## Project structure

```
.
├── config/
│   ├── company.yaml          # Edit this with your company details
│   ├── logo.png              # Your company logo (add manually)
│   └── credentials/          # Google OAuth files (gitignored)
├── data/                     # all gitignored: this is the client's data
│   ├── facturacio.db         # SQLite: invoices, contacts, products, bills, Verifactu
│   ├── invoices/             # Invoice PDFs (and borradores/ for drafts)
│   ├── presupuestos/         # Quote PDFs
│   ├── gestor/               # The quarterly packs for the gestor
│   └── receipts/             # Photographed supplier documents
├── src/
│   ├── models.py             # InvoiceData and InvoiceItem dataclasses
│   ├── config_loader.py      # Loads company.yaml
│   ├── db.py                 # SQLite schema and per-thread connections
│   ├── invoice_number.py     # Gap-free per-series invoice numbering
│   ├── transcription.py      # Groq / OpenAI Whisper STT
│   ├── parser.py             # Voice → invoice data extractor
│   ├── receipts.py           # Photo → supplier bill (vision model)
│   ├── totals.py             # The single source of truth for VAT maths
│   ├── checklist.py          # What an invoice needs before it can be issued
│   ├── conversation.py       # The ask-for-what-is-missing dialogue
│   ├── contacts.py           # Clients and suppliers
│   ├── catalog.py            # Products, stock, and matching spoken lines to them
│   ├── bills.py              # Supplier bills and due dates
│   ├── invoice_generator.py  # ReportLab PDF builder
│   ├── google_auth.py        # Shared Google OAuth2 flow
│   ├── sheets.py             # Google Sheets logger
│   ├── email_sender.py       # Gmail sender (send_email + send_invoice_email)
│   ├── store.py              # Invoice records, pending and issued
│   ├── finalize.py           # Shared: assign number → PDF → Sheets → email → record
│   ├── rectify.py            # Contra / rectifying invoices
│   ├── notify.py             # Money digest and reviewer reminders
│   ├── scheduler.py          # Batched reminders (reviews, money, stock)
│   ├── accounts.py           # Companies, accounts, roles, permissions, Telegram linking
│   ├── telegram_access.py    # Who may use the bot, and as whom
│   ├── telegram_api.py       # Messages and files to Telegram from the web and scheduler
│   ├── verifactu.py          # The chained Verifactu register and the invoice QR
│   ├── taxes.py              # Modelo 303 and 130 from the recorded invoices and bills
│   ├── gestor_pack.py        # The quarter's ZIP for the gestor, and its calendar
│   ├── payment_reminders.py  # Chasing overdue invoices, asking the owner first
│   ├── recurring.py          # Invoices that repeat every month/quarter/year
│   ├── quotes.py             # Presupuestos, and turning an accepted one into an invoice
│   ├── einvoice.py           # Facturae (FACe) and UBL (EN 16931) electronic invoices
│   ├── bank.py               # Bank statements matched to invoices and bills
│   ├── timeclock.py          # The working-time record (registro de jornada)
│   ├── assistant.py          # What a message is asking for (invoice, expense, question)
│   ├── answers.py            # Questions about the business, answered from the books
│   ├── backup.py             # Daily copies of the data, and restoring one
│   ├── web/app.py            # FastAPI review page (Google login)
│   ├── web/admin.py          # Team panel and vendor panel
│   └── bot.py                # Telegram bot handlers
├── main.py                   # Entry point (bot + web + scheduler)
├── requirements.txt
└── .env.example
```
