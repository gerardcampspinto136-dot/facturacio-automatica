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
    _local.conn = conn
    return conn


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
