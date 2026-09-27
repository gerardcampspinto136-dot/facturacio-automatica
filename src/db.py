"""SQLite storage for the whole system.

One file, data/facturacio.db, holds invoices, contacts, products and stock movements.
There is no server to install and no migration tool: connect() applies the schema below
on every start (all statements are IF NOT EXISTS).

Everything that writes goes through a transaction, so a crash halfway through issuing an
invoice cannot leave a number consumed without an invoice attached to it.
"""

import os
import sqlite3
import threading
from pathlib import Path

DB_PATH = Path(os.getenv("DB_PATH", "data/facturacio.db"))

_local = threading.local()

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- The businesses this system serves: one row per client company the software is sold
-- to. Kept as a table rather than only config/company.yaml because accounts hang off
-- it, and because the vendor needs to see and suspend them from the admin panel.
CREATE TABLE IF NOT EXISTS companies (
    id         INTEGER PRIMARY KEY,
    name       TEXT NOT NULL,
    tax_id     TEXT,
    contact_email TEXT,
    status     TEXT NOT NULL DEFAULT 'active'
               CHECK (status IN ('active', 'suspended')),
    notes      TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Everyone who can sign in.
--
-- role says what kind of account it is:
--   superadmin  the vendor. Belongs to no company (company_id IS NULL) and may create
--               companies and their first owner.
--   admin       the client business owner. Everything inside their own company,
--               including creating and revoking their employees' accounts.
--   employee    only what `permissions` grants, one key per line.
--
-- Emails are stored lowercased so the unique index is a real constraint: an account is
-- the identity Google hands back at login, and two rows differing only in case would
-- let the same person hold two different permission sets.
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY,
    company_id    INTEGER REFERENCES companies (id) ON DELETE CASCADE,
    email         TEXT NOT NULL UNIQUE,
    name          TEXT,
    role          TEXT NOT NULL DEFAULT 'employee'
                  CHECK (role IN ('superadmin', 'admin', 'employee')),
    permissions   TEXT NOT NULL DEFAULT '',
    active        INTEGER NOT NULL DEFAULT 1,
    created_by    INTEGER REFERENCES users (id) ON DELETE SET NULL,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    last_login_at TEXT,
    CHECK ((role = 'superadmin') = (company_id IS NULL))
);
CREATE INDEX IF NOT EXISTS idx_users_company ON users (company_id, active);

-- Clients and suppliers share one table; `kind` separates them. A contact can be both,
-- in which case it is stored twice: the tax details are the same but payment terms,
-- history and balances are not.
CREATE TABLE IF NOT EXISTS contacts (
    id                 INTEGER PRIMARY KEY,
    kind               TEXT NOT NULL CHECK (kind IN ('client', 'supplier')),
    name               TEXT NOT NULL,
    tax_id             TEXT,
    email              TEXT,
    phone              TEXT,
    address            TEXT,
    payment_terms_days INTEGER NOT NULL DEFAULT 30,
    iban               TEXT,
    notes              TEXT,
    active             INTEGER NOT NULL DEFAULT 1,
    created_at         TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_contacts_kind_name ON contacts (kind, name);
CREATE UNIQUE INDEX IF NOT EXISTS idx_contacts_kind_taxid
    ON contacts (kind, tax_id) WHERE tax_id IS NOT NULL AND tax_id <> '';

-- Products and services. Services set track_stock = 0 and ignore the stock columns.
CREATE TABLE IF NOT EXISTS products (
    id             INTEGER PRIMARY KEY,
    sku            TEXT UNIQUE,
    name           TEXT NOT NULL,
    description    TEXT,
    unit           TEXT NOT NULL DEFAULT 'ud',
    unit_price     REAL NOT NULL DEFAULT 0,
    cost_price     REAL NOT NULL DEFAULT 0,
    tax_rate       REAL,
    track_stock    INTEGER NOT NULL DEFAULT 1,
    stock_qty      REAL NOT NULL DEFAULT 0,
    reorder_point  REAL NOT NULL DEFAULT 0,
    supplier_id    INTEGER REFERENCES contacts (id) ON DELETE SET NULL,
    active         INTEGER NOT NULL DEFAULT 1,
    created_at     TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_products_name ON products (name);

-- Every change to stock_qty is written here too, so the level is always explainable.
CREATE TABLE IF NOT EXISTS stock_moves (
    id          INTEGER PRIMARY KEY,
    product_id  INTEGER NOT NULL REFERENCES products (id) ON DELETE CASCADE,
    delta       REAL NOT NULL,
    balance     REAL NOT NULL,
    reason      TEXT NOT NULL,
    ref         TEXT,
    note        TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_moves_product ON stock_moves (product_id, created_at);

-- Sales invoices, in both states: 'pending' (awaiting review, no number yet) and
-- 'issued'. Client details are snapshotted onto the invoice so that editing a contact
-- later never rewrites history on an invoice already sent.
CREATE TABLE IF NOT EXISTS invoices (
    id             INTEGER PRIMARY KEY,
    status         TEXT NOT NULL CHECK (status IN ('pending', 'issued')),
    token          TEXT UNIQUE,
    number         TEXT UNIQUE,
    contact_id     INTEGER REFERENCES contacts (id) ON DELETE SET NULL,
    client_name    TEXT NOT NULL DEFAULT '',
    client_email   TEXT NOT NULL DEFAULT '',
    client_address TEXT,
    client_id      TEXT,
    date           TEXT NOT NULL,
    due_date       TEXT,
    notes          TEXT,
    prices_include_tax INTEGER,
    rectifies      TEXT,
    rectified_by   TEXT,
    draft_path     TEXT,
    paid_at        TEXT,
    paid_amount    REAL,
    created_at     TEXT NOT NULL DEFAULT (datetime('now')),
    issued_at      TEXT
);
CREATE INDEX IF NOT EXISTS idx_invoices_status ON invoices (status, created_at);
CREATE INDEX IF NOT EXISTS idx_invoices_unpaid ON invoices (paid_at, due_date)
    WHERE status = 'issued';

CREATE TABLE IF NOT EXISTS invoice_items (
    id          INTEGER PRIMARY KEY,
    invoice_id  INTEGER NOT NULL REFERENCES invoices (id) ON DELETE CASCADE,
    position    INTEGER NOT NULL DEFAULT 0,
    product_id  INTEGER REFERENCES products (id) ON DELETE SET NULL,
    description TEXT NOT NULL,
    quantity    REAL NOT NULL DEFAULT 1,
    unit_price  REAL NOT NULL DEFAULT 0,
    total       REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_items_invoice ON invoice_items (invoice_id, position);

-- Bills received from suppliers. The mirror image of invoices: money going out.
CREATE TABLE IF NOT EXISTS bills (
    id            INTEGER PRIMARY KEY,
    supplier_id   INTEGER REFERENCES contacts (id) ON DELETE SET NULL,
    supplier_name TEXT NOT NULL DEFAULT '',
    reference     TEXT,
    date          TEXT NOT NULL,
    due_date      TEXT,
    subtotal      REAL NOT NULL DEFAULT 0,
    tax_amount    REAL NOT NULL DEFAULT 0,
    total         REAL NOT NULL DEFAULT 0,
    category      TEXT,
    notes         TEXT,
    file_path     TEXT,
    paid_at       TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_bills_unpaid ON bills (paid_at, due_date);

-- Gap-free per-series invoice numbering. Replaces data/invoice_counter.json.
CREATE TABLE IF NOT EXISTS counters (
    series  TEXT NOT NULL,
    year    INTEGER NOT NULL,
    counter INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (series, year)
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- Invoices that repeat: a maintenance fee, a rent, a monthly retainer. Each row is a
-- template; on its date it becomes a real invoice -- prepared for approval, or sent
-- on its own if the company chose that. Line prices are stored net.
CREATE TABLE IF NOT EXISTS recurring_invoices (
    id             INTEGER PRIMARY KEY,
    contact_id     INTEGER REFERENCES contacts (id) ON DELETE SET NULL,
    client_name    TEXT NOT NULL,
    client_email   TEXT,
    client_address TEXT,
    client_id      TEXT,
    items_json     TEXT NOT NULL,
    notes          TEXT,
    tax_rate       REAL,
    irpf_rate      REAL,
    frequency      TEXT NOT NULL CHECK (frequency IN ('monthly', 'quarterly', 'yearly')),
    day_of_month   INTEGER NOT NULL,
    next_date      TEXT NOT NULL,
    auto_send      INTEGER NOT NULL DEFAULT 0,
    active         INTEGER NOT NULL DEFAULT 1,
    created_by     INTEGER,
    created_chat_id INTEGER,
    last_run_at    TEXT,
    last_number    TEXT,
    source_number  TEXT,
    created_at     TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_recurring_due ON recurring_invoices (active, next_date);

-- The working-time record (registro de jornada, art. 34.9 of the Estatuto de los
-- Trabajadores): each clock event as it happened, chained by fingerprint like the
-- Verifactu register, never edited. A mistake -- a forgotten clock-out -- is fixed by
-- adding a correction that says who made it and why; the original stays.
CREATE TABLE IF NOT EXISTS time_entries (
    id            INTEGER PRIMARY KEY,
    user_id       INTEGER NOT NULL REFERENCES users (id),
    kind          TEXT NOT NULL CHECK (kind IN ('in', 'out', 'break_start', 'break_end')),
    at            TEXT NOT NULL,
    source        TEXT NOT NULL DEFAULT 'telegram',
    note          TEXT,
    corrected_by  INTEGER REFERENCES users (id),
    reason        TEXT,
    previous_hash TEXT NOT NULL DEFAULT '',
    hash          TEXT NOT NULL UNIQUE,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_time_user ON time_entries (user_id, at);

CREATE TRIGGER IF NOT EXISTS trg_time_frozen
BEFORE UPDATE ON time_entries
BEGIN
    SELECT RAISE(ABORT, 'Un fichaje no se puede modificar: añade una corrección.');
END;

CREATE TRIGGER IF NOT EXISTS trg_time_kept
BEFORE DELETE ON time_entries
BEGIN
    SELECT RAISE(ABORT, 'Un fichaje no se puede borrar: añade una corrección.');
END;

-- Movements imported from bank statements, kept so that importing the same statement
-- twice adds nothing, and so each can be matched to what it paid: an invoice (money
-- in), a supplier bill (money out), or a new expense recorded from it.
CREATE TABLE IF NOT EXISTS bank_movements (
    id             INTEGER PRIMARY KEY,
    fingerprint    TEXT NOT NULL UNIQUE,
    date           TEXT NOT NULL,
    value_date     TEXT,
    amount         REAL NOT NULL,
    description    TEXT NOT NULL DEFAULT '',
    balance        REAL,
    status         TEXT NOT NULL DEFAULT 'new'
                   CHECK (status IN ('new', 'matched', 'ignored')),
    invoice_number TEXT,
    bill_id        INTEGER REFERENCES bills (id) ON DELETE SET NULL,
    source         TEXT,
    imported_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_bank_status ON bank_movements (status, date);

-- Quotes (presupuestos). Not invoices: no fiscal value, no Verifactu record, and they
-- can be accepted, rejected or left to expire. An accepted one becomes an invoice,
-- and remembers which.
CREATE TABLE IF NOT EXISTS quotes (
    id             INTEGER PRIMARY KEY,
    number         TEXT NOT NULL UNIQUE,
    status         TEXT NOT NULL DEFAULT 'sent'
                   CHECK (status IN ('sent', 'accepted', 'rejected', 'invoiced')),
    contact_id     INTEGER REFERENCES contacts (id) ON DELETE SET NULL,
    client_name    TEXT NOT NULL DEFAULT '',
    client_email   TEXT,
    client_address TEXT,
    client_id      TEXT,
    date           TEXT NOT NULL,
    valid_until    TEXT NOT NULL,
    notes          TEXT,
    tax_rate       REAL,
    irpf_rate      REAL,
    invoice_number TEXT,
    email_sent_at  TEXT,
    email_error    TEXT,
    created_by     INTEGER,
    created_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS quote_items (
    id          INTEGER PRIMARY KEY,
    quote_id    INTEGER NOT NULL REFERENCES quotes (id) ON DELETE CASCADE,
    position    INTEGER NOT NULL DEFAULT 0,
    description TEXT NOT NULL,
    quantity    REAL NOT NULL DEFAULT 1,
    unit_price  REAL NOT NULL DEFAULT 0,
    total       REAL NOT NULL DEFAULT 0
);

-- The Verifactu register (see src/verifactu.py): one record per invoice issued, each
-- fingerprinted together with the one before it. The values are stored exactly as they
-- were hashed -- as text -- so the chain can be re-verified at any time.
CREATE TABLE IF NOT EXISTS verifactu_records (
    id              INTEGER PRIMARY KEY,
    kind            TEXT NOT NULL CHECK (kind IN ('alta', 'anulacion')),
    invoice_number  TEXT NOT NULL,
    issuer_nif      TEXT NOT NULL,
    issue_date      TEXT NOT NULL,
    invoice_type    TEXT,
    tax_total       TEXT,
    amount_total    TEXT,
    previous_hash   TEXT NOT NULL DEFAULT '',
    generated_at    TEXT NOT NULL,
    hash            TEXT NOT NULL UNIQUE,
    -- For sending the record to the AEAT, once that is built.
    sent_status     TEXT NOT NULL DEFAULT 'not_sent',
    sent_at         TEXT,
    aeat_response   TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_verifactu_number ON verifactu_records (invoice_number);

-- A record, once written, is part of a chain that proves nothing was rewritten: it can
-- be marked as sent to the AEAT, and nothing else.
CREATE TRIGGER IF NOT EXISTS trg_verifactu_frozen
BEFORE UPDATE OF kind, invoice_number, issuer_nif, issue_date, invoice_type, tax_total,
                 amount_total, previous_hash, generated_at, hash
ON verifactu_records
BEGIN
    SELECT RAISE(ABORT, 'Un registro Verifactu no se puede modificar.');
END;

CREATE TRIGGER IF NOT EXISTS trg_verifactu_kept
BEFORE DELETE ON verifactu_records
BEGIN
    SELECT RAISE(ABORT, 'Un registro Verifactu no se puede borrar.');
END;
"""


def connect() -> sqlite3.Connection:
    """Return this thread's connection, creating and initialising the database if needed.

    Connections are per-thread because the bot, the web app and the scheduler all run in
    the same process but on different threads, and SQLite objects are not shareable.
    """
    conn = getattr(_local, "conn", None)
    if conn is not None:
        return conn

    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=15, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    _upgrade(conn)
    _local.conn = conn
    return conn


# Columns added after the first release. CREATE TABLE IF NOT EXISTS silently does nothing
# to a table that already exists, so new columns have to be added explicitly.
_ADDED_COLUMNS = {
    "invoices": [
        ("prices_include_tax", "INTEGER"),
        # Who prepared a draft that is waiting for approval, and where to tell them.
        ("created_by", "INTEGER"),
        ("created_by_name", "TEXT"),
        ("created_chat_id", "INTEGER"),
        # The rates this invoice was issued at. NULL on invoices from before this
        # column existed, which then fall back to the company default.
        ("tax_rate", "REAL"),
        ("irpf_rate", "REAL"),
        # Whether the email to the client actually went out, and why not if it did not.
        ("email_sent_at", "TEXT"),
        ("email_error", "TEXT"),
        # Chasing an unpaid invoice: how many reminders went out, when the owner was
        # last asked about sending one, and whether they said to stop.
        ("reminder_count", "INTEGER NOT NULL DEFAULT 0"),
        ("last_reminder_at", "TEXT"),
        ("reminder_prompted_at", "TEXT"),
        ("reminders_paused", "INTEGER NOT NULL DEFAULT 0"),
        # The quote this invoice came from, if any.
        ("quote_number", "TEXT"),
    ],
    # Everything the admin panel needs to set up a client without editing YAML. Added
    # here rather than in CREATE TABLE so an installation that already has companies
    # gains the columns on its next start.
    "companies": [
        ("address", "TEXT"),
        ("phone", "TEXT"),
        ("invoice_email", "TEXT"),
        ("iban", "TEXT"),
        ("tax_rate", "REAL"),
        ("payment_terms", "TEXT"),
        ("prices_include_tax", "INTEGER"),
        ("invoice_series", "TEXT"),
        ("review_mode", "TEXT"),
        ("telegram_bot_token", "TEXT"),
        ("telegram_chat_id", "TEXT"),
        ("logo_path", "TEXT"),
        ("configured_at", "TEXT"),
        ("irpf_rate", "REAL"),
        # Where the quarter's pack goes: the company's gestor or accountant.
        ("gestor_email", "TEXT"),
        # ask / auto / off: how overdue invoices are chased.
        ("collections_mode", "TEXT"),
    ],
    # What was agreed with each client and should not have to be said twice: whether
    # their invoices carry an IRPF withholding (NULL = the company default).
    "contacts": [
        ("irpf_rate", "REAL"),
    ],
    # A Telegram account linked to a panel account, so the bot applies the same
    # permissions the web does. The code is a one-time pairing secret with an expiry.
    "users": [
        ("telegram_id", "INTEGER"),
        ("telegram_username", "TEXT"),
        ("telegram_code", "TEXT"),
        ("telegram_code_expires", "TEXT"),
    ],
}

# Statements that depend on the columns above, so they can only run once those exist.
# An index on a column an old database does not have yet would fail in SCHEMA.
_AFTER_UPGRADE = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_users_telegram
    ON users (telegram_id) WHERE telegram_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_users_telegram_code
    ON users (telegram_code) WHERE telegram_code IS NOT NULL;

-- An issued invoice is a legal document: it is corrected by issuing a rectifying
-- invoice, never by editing or deleting it (and Verifactu requires exactly that).
-- Enforced here rather than trusted to the code, so no future bug and no hand-run
-- UPDATE can quietly rewrite one. Payment tracking, the rectified-by link and the
-- email delivery status are bookkeeping about the invoice, not part of it, so they
-- stay writable.
CREATE TRIGGER IF NOT EXISTS trg_issued_invoice_frozen
BEFORE UPDATE OF number, status, client_name, client_email, client_address, client_id,
                 date, notes, rectifies, prices_include_tax, tax_rate, irpf_rate
ON invoices WHEN OLD.status = 'issued'
BEGIN
    SELECT RAISE(ABORT, 'Una factura emitida no se puede modificar: emite una rectificativa.');
END;

CREATE TRIGGER IF NOT EXISTS trg_issued_invoice_kept
BEFORE DELETE ON invoices WHEN OLD.status = 'issued'
BEGIN
    SELECT RAISE(ABORT, 'Una factura emitida no se puede borrar: emite una rectificativa.');
END;

CREATE TRIGGER IF NOT EXISTS trg_issued_items_no_insert
BEFORE INSERT ON invoice_items
WHEN (SELECT status FROM invoices WHERE id = NEW.invoice_id) = 'issued'
BEGIN
    SELECT RAISE(ABORT, 'Una factura emitida no se puede modificar: emite una rectificativa.');
END;

CREATE TRIGGER IF NOT EXISTS trg_issued_items_no_update
BEFORE UPDATE ON invoice_items
WHEN (SELECT status FROM invoices WHERE id = OLD.invoice_id) = 'issued'
BEGIN
    SELECT RAISE(ABORT, 'Una factura emitida no se puede modificar: emite una rectificativa.');
END;

CREATE TRIGGER IF NOT EXISTS trg_issued_items_no_delete
BEFORE DELETE ON invoice_items
WHEN (SELECT status FROM invoices WHERE id = OLD.invoice_id) = 'issued'
BEGIN
    SELECT RAISE(ABORT, 'Una factura emitida no se puede modificar: emite una rectificativa.');
END;
"""


def _upgrade(conn: sqlite3.Connection) -> None:
    for table, columns in _ADDED_COLUMNS.items():
        existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        for name, decl in columns:
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
    conn.executescript(_AFTER_UPGRADE)


def close() -> None:
    """Close this thread's connection. Mainly for tests."""
    conn = getattr(_local, "conn", None)
    if conn is not None:
        conn.close()
        _local.conn = None


def set_db_path(path) -> None:
    """Point the store at a different database file (used by the tests)."""
    global DB_PATH
    close()
    DB_PATH = Path(path)


class transaction:
    """Context manager wrapping a write in BEGIN IMMEDIATE / COMMIT.

    IMMEDIATE takes the write lock up front, so two threads assigning an invoice number
    at the same moment serialise instead of one failing at commit time.
    """

    def __enter__(self) -> sqlite3.Connection:
        self.conn = connect()
        self.conn.execute("BEGIN IMMEDIATE")
        return self.conn

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc_type is None:
            self.conn.execute("COMMIT")
        else:
            self.conn.execute("ROLLBACK")
        return False
