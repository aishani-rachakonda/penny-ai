"""Penny's database: one schema for every source, and one copy per chat session.

Every row records where it came from, so Penny can always say how it knows something:

    synced    from a provider (Plaid for banks, Splitwise for shared expenses)
    detected  inferred by Penny's algorithms (recurring payments, links between sources)
    user      told to Penny by the user (onboarding answers, plans, rules, notes)
    manual    a transaction the user reported before the bank has it

Derived numbers (budget left, who owes whom, recommendations) are never stored:
finance.py computes them from these rows on demand.

Money is stored as integer cents. Negative amounts are money leaving an account.
"""

import sqlite3
from datetime import date
from pathlib import Path

SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

-- Connected sources. One row per Plaid Item (a bank login) and one for Splitwise.
CREATE TABLE connections (
    connection_id   INTEGER PRIMARY KEY,
    provider        TEXT NOT NULL,             -- 'plaid' | 'splitwise'
    institution     TEXT NOT NULL,             -- 'Chase', 'Bank of America', 'Splitwise'
    access_token    TEXT,                      -- Plaid access token (production: encrypted at rest)
    external_id     TEXT,                      -- Plaid item_id / Splitwise user id
    cursor          TEXT,                      -- Plaid sync cursor / Splitwise updated_after
    last_synced     TEXT
);

CREATE TABLE accounts (
    account_id      TEXT PRIMARY KEY,          -- short name the agent uses: 'chase', 'boa'
    connection_id   INTEGER NOT NULL REFERENCES connections,
    external_id     TEXT UNIQUE NOT NULL,      -- Plaid account_id
    name            TEXT NOT NULL,
    mask            TEXT,
    subtype         TEXT,
    balance_current_cents   INTEGER,           -- as reported by the bank at last sync
    balance_available_cents INTEGER,           -- current minus pending
    balance_as_of   TEXT
);

CREATE TABLE transactions (
    txn_id          INTEGER PRIMARY KEY,
    account_id      TEXT NOT NULL REFERENCES accounts,
    external_id     TEXT UNIQUE,               -- Plaid transaction_id; NULL for manual entries
    date            TEXT NOT NULL,             -- when it happened (Plaid authorized_date)
    posted_date     TEXT,                      -- when the bank posted it; NULL while pending
    pending         INTEGER NOT NULL DEFAULT 0,
    amount_cents    INTEGER NOT NULL,
    description     TEXT NOT NULL,             -- raw bank description
    merchant        TEXT NOT NULL,
    category        TEXT NOT NULL,
    category_source TEXT NOT NULL,             -- 'plaid' | 'penny_rule' | 'user_rule'
    provider_category TEXT,                    -- Plaid personal_finance_category.detailed
    kind            TEXT NOT NULL,             -- purchase | income | transfer | p2p_in | p2p_out
    person          TEXT,                      -- counterparty for person-to-person payments
    transfer_id     INTEGER,                   -- both legs of a transfer between own accounts share it
    source          TEXT NOT NULL              -- 'plaid' | 'manual'
);

CREATE TABLE people (
    name            TEXT PRIMARY KEY,          -- first name, as the user refers to them
    full_name       TEXT NOT NULL,             -- matches bank counterparties ("Sam Okafor")
    splitwise_user_id INTEGER UNIQUE
);

CREATE TABLE shared_expenses (
    expense_id      INTEGER PRIMARY KEY,
    external_id     TEXT UNIQUE NOT NULL,      -- Splitwise expense id
    date            TEXT NOT NULL,
    description     TEXT NOT NULL,
    category        TEXT NOT NULL,
    cost_cents      INTEGER NOT NULL,
    paid_by         TEXT NOT NULL,             -- 'me' or a person's name
    txn_id          INTEGER REFERENCES transactions ON DELETE SET NULL,  -- the bank charge behind it
    link_source     TEXT                       -- 'detected' (matched by amount/date) | 'user'
);

CREATE TABLE shared_shares (
    expense_id      INTEGER NOT NULL REFERENCES shared_expenses ON DELETE CASCADE,
    person          TEXT NOT NULL,             -- 'me' or a person's name
    owed_cents      INTEGER NOT NULL,
    PRIMARY KEY (expense_id, person)
);

CREATE TABLE settlements (
    settlement_id   INTEGER PRIMARY KEY,
    external_id     TEXT UNIQUE NOT NULL,      -- Splitwise payment id
    date            TEXT NOT NULL,
    from_person     TEXT NOT NULL,
    to_person       TEXT NOT NULL,
    amount_cents    INTEGER NOT NULL,
    txn_id          INTEGER REFERENCES transactions ON DELETE SET NULL,  -- the Zelle that moved the money
    link_source     TEXT
);

-- Categories and how merchants map to them.
CREATE TABLE categories (
    name            TEXT PRIMARY KEY,
    kind            TEXT NOT NULL,             -- 'bill' (fixed, covered by bills) | 'flexible'
    source          TEXT NOT NULL              -- 'penny' (default set) | 'user'
);
CREATE TABLE merchant_rules (
    merchant        TEXT PRIMARY KEY,          -- lowercase merchant name
    category        TEXT NOT NULL,
    source          TEXT NOT NULL              -- 'penny' (built in) | 'user' (a correction)
);

-- The user's plan, one per month. Known facts come from the user; Penny fills gaps.
CREATE TABLE plans (
    month           TEXT PRIMARY KEY,          -- 'YYYY-MM'
    monthly_cap_cents INTEGER NOT NULL,
    source          TEXT NOT NULL,             -- 'user' | 'carried_forward'
    updated_on      TEXT NOT NULL
);
CREATE TABLE plan_bills (
    month           TEXT NOT NULL,
    name            TEXT NOT NULL,
    category        TEXT NOT NULL,
    amount_cents    INTEGER,                   -- NULL = varies; Penny estimates it from detection
    due_day_start   INTEGER NOT NULL,
    due_day_end     INTEGER NOT NULL,
    how_paid        TEXT NOT NULL,             -- 'bank' | 'splitwise'
    match           TEXT NOT NULL,             -- text that identifies the payment
    source          TEXT NOT NULL,             -- 'user' | 'detected' (a detected payment the user confirmed)
    PRIMARY KEY (month, name)
);
CREATE TABLE plan_categories (
    month           TEXT NOT NULL,
    category        TEXT NOT NULL,
    target_cents    INTEGER NOT NULL,
    source          TEXT NOT NULL,             -- 'penny' (suggested from history) | 'user'
    PRIMARY KEY (month, category)
);

-- Recurring payments Penny detected. Rebuilt after every sync; the user's verdicts are kept.
CREATE TABLE recurring_series (
    series_key      TEXT PRIMARY KEY,          -- 'bank:spotify', 'splitwise:coned electricity'
    label           TEXT NOT NULL,
    origin          TEXT NOT NULL,             -- 'bank' | 'splitwise'
    direction       TEXT NOT NULL,             -- 'out' | 'in'
    kind            TEXT NOT NULL,             -- subscription | bill | income | other
    category        TEXT,
    cadence         TEXT NOT NULL,             -- weekly | biweekly | monthly
    typical_cents   INTEGER NOT NULL,
    last_cents      INTEGER NOT NULL,
    low_cents       INTEGER NOT NULL,
    high_cents      INTEGER NOT NULL,
    variable        INTEGER NOT NULL,
    occurrences     INTEGER NOT NULL,
    first_date      TEXT NOT NULL,
    last_date       TEXT NOT NULL,
    next_due        TEXT,
    due_day_low     INTEGER,
    due_day_high    INTEGER,
    status          TEXT NOT NULL,             -- active | stopped
    price_change    TEXT,                      -- JSON {from, to, on} if the amount changed
    confidence      REAL NOT NULL
);
CREATE TABLE recurring_verdicts (
    series_key      TEXT PRIMARY KEY,
    verdict         TEXT NOT NULL              -- 'dismissed' (the user says it isn't a bill)
);

CREATE TABLE memory_notes (
    note_id         INTEGER PRIMARY KEY,
    text            TEXT NOT NULL,
    created_on      TEXT NOT NULL
);

CREATE TABLE recommendations (
    rec_id          INTEGER PRIMARY KEY,
    sim_date        TEXT NOT NULL,
    category        TEXT NOT NULL,
    horizon         TEXT NOT NULL,
    amount_cents    INTEGER NOT NULL,
    inputs          TEXT NOT NULL              -- JSON snapshot used to explain later changes
);

-- Ingestion audit trail: the untouched provider payloads, and what each sync changed.
CREATE TABLE raw_events (
    event_id        INTEGER PRIMARY KEY,
    connection_id   INTEGER NOT NULL,
    received_on     TEXT NOT NULL,
    endpoint        TEXT NOT NULL,
    payload         TEXT NOT NULL
);
CREATE TABLE sync_log (
    sync_id         INTEGER PRIMARY KEY,
    sim_date        TEXT NOT NULL,
    connection_id   INTEGER NOT NULL,
    added           INTEGER NOT NULL,
    modified        INTEGER NOT NULL,
    removed         INTEGER NOT NULL,
    detail          TEXT NOT NULL
);

-- Simulation only: Splitwise writes made in this session (stands in for Splitwise's servers).
CREATE TABLE sandbox_splitwise_writes (id INTEGER PRIMARY KEY, payload TEXT NOT NULL);
"""

# Categories aren't preset: each one is created the first time the user's own data uses it
# (or when the user makes one in chat). These are the ones that hold fixed bills.
BILL_CATEGORIES = {"Rent", "Utilities", "Subscriptions"}


def ensure_category(conn, name: str, source: str = "penny") -> str:
    conn.execute("INSERT OR IGNORE INTO categories VALUES (?, ?, ?)",
                 (name, "bill" if name in BILL_CATEGORIES else "flexible", source))
    return name


# Built-in merchant rules, for where Plaid's category is too coarse for budgeting.
DEFAULT_MERCHANT_RULES = {
    "uber eats": "Food Delivery", "doordash": "Food Delivery", "grubhub": "Food Delivery",
    "hulu": "Subscriptions", "netflix": "Subscriptions", "spotify": "Subscriptions", "icloud": "Subscriptions",
    "blink fitness": "Subscriptions",
}

DEMO_START = "2026-09-18"  # the simulated "today" every new session starts on (a Friday)
DEMO_LAST_DAY = "2026-10-06"


def today(conn) -> date:
    return date.fromisoformat(meta(conn, "today"))


def meta(conn, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def user_name(conn) -> str:
    return meta(conn, "user_name") or "you"


def categories(conn, kind: str | None = None) -> list[str]:
    if kind:
        return [r[0] for r in conn.execute("SELECT name FROM categories WHERE kind = ? ORDER BY name", (kind,))]
    return [r[0] for r in conn.execute("SELECT name FROM categories ORDER BY name")]


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def build_seed(snapshot_path: Path | None = None) -> sqlite3.Connection:
    """Onboard the demo user exactly as a real user would be onboarded.

    1. Record what the user told Penny at sign-up (sandbox/data/user_profile.json).
    2. Connect their sources (two Plaid Items, one Splitwise account).
    3. Run the ingestion pipeline: initial sync, linking, recurring detection.
    4. Draft the first monthly plan from what they told Penny plus what was detected.
    """
    import planning
    from ingest import pipeline
    from sandbox.providers import PROFILE

    conn = connect()
    conn.executescript(SCHEMA)
    conn.executemany("INSERT INTO merchant_rules VALUES (?, ?, 'penny')", DEFAULT_MERCHANT_RULES.items())
    conn.executemany("INSERT INTO meta VALUES (?, ?)", [
        ("today", DEMO_START), ("start", DEMO_START), ("last_day", DEMO_LAST_DAY), ("user_name", PROFILE["name"])])

    pipeline.connect_sources(conn)
    pipeline.sync_all(conn)
    planning.create_plan_from_profile(conn, PROFILE, date.fromisoformat(DEMO_START))
    conn.commit()

    if snapshot_path:  # a file copy anyone can open with a SQLite browser
        snapshot_path.unlink(missing_ok=True)
        disk = sqlite3.connect(snapshot_path)
        conn.backup(disk)
        disk.close()
    return conn


def session_copy(seed: sqlite3.Connection) -> sqlite3.Connection:
    """A private, writable copy of the seed for one chat session."""
    conn = connect()
    seed.backup(conn)
    return conn
