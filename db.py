"""The data layer: schema, statement importers, and one database copy per session.

Only facts and the user's plan are stored here. Everything derived from them
(balances, who owes whom, budget remaining) is computed by finance.py on demand,
so it can never go stale.

Money is stored as integer cents. Negative amounts are money leaving an account.
"""

import csv
import re
import sqlite3
from datetime import date, datetime
from pathlib import Path

STATEMENTS = Path(__file__).parent / "statements"
ME = "Maya"  # the Splitwise member who is the user; every other member is a "person"

SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE accounts (
    account_id      TEXT PRIMARY KEY,      -- short name the agent uses: 'chase', 'boa'
    name            TEXT NOT NULL,
    role            TEXT NOT NULL,         -- 'spending' | 'salary'
    opening_cents   INTEGER NOT NULL,
    opening_date    TEXT NOT NULL
);

CREATE TABLE transactions (
    txn_id          INTEGER PRIMARY KEY,
    account_id      TEXT NOT NULL REFERENCES accounts,
    date            TEXT NOT NULL,         -- ISO date; hidden from tools until the simulated today
    amount_cents    INTEGER NOT NULL,
    description     TEXT NOT NULL,
    merchant        TEXT NOT NULL,
    category        TEXT NOT NULL,
    kind            TEXT NOT NULL,         -- purchase | income | transfer | p2p_in | p2p_out
    person          TEXT,                  -- counterparty for p2p payments
    transfer_id     INTEGER,               -- both legs of a transfer between own accounts share it
    source          TEXT NOT NULL          -- 'statement' | 'user'
);

CREATE TABLE shared_expenses (
    expense_id      INTEGER PRIMARY KEY,
    date            TEXT NOT NULL,
    description     TEXT NOT NULL,
    category        TEXT NOT NULL,
    cost_cents      INTEGER NOT NULL,
    paid_by         TEXT NOT NULL,         -- 'me' or a person's name
    txn_id          INTEGER REFERENCES transactions  -- the bank charge, when the user paid
);

CREATE TABLE shared_shares (
    expense_id      INTEGER NOT NULL REFERENCES shared_expenses,
    person          TEXT NOT NULL,         -- 'me' or a person's name
    owed_cents      INTEGER NOT NULL,
    PRIMARY KEY (expense_id, person)
);

CREATE TABLE settlements (
    settlement_id   INTEGER PRIMARY KEY,
    date            TEXT NOT NULL,
    from_person     TEXT NOT NULL,         -- 'me' or a person's name
    to_person       TEXT NOT NULL,
    amount_cents    INTEGER NOT NULL,
    txn_id          INTEGER REFERENCES transactions  -- the matching Zelle, if one exists
);

CREATE TABLE budget (key TEXT PRIMARY KEY, value INTEGER NOT NULL);   -- monthly_cap_cents

CREATE TABLE budget_categories (
    category        TEXT PRIMARY KEY,
    target_cents    INTEGER NOT NULL
);

CREATE TABLE obligations (
    name            TEXT PRIMARY KEY,
    category        TEXT NOT NULL,
    amount_cents    INTEGER NOT NULL,      -- expected cost to the user; an estimate if variable
    variable        INTEGER NOT NULL,      -- 1 if the amount changes month to month
    source          TEXT NOT NULL,         -- 'bank' (matched in transactions) | 'shared' (matched in shared expenses)
    match           TEXT NOT NULL,         -- case-insensitive substring to look for
    due_day         INTEGER NOT NULL
);

CREATE TABLE recommendations (
    rec_id          INTEGER PRIMARY KEY,
    sim_date        TEXT NOT NULL,
    category        TEXT NOT NULL,
    horizon         TEXT NOT NULL,
    amount_cents    INTEGER NOT NULL,
    inputs          TEXT NOT NULL          -- JSON snapshot used to explain later changes
);
"""

# --- Categorization ------------------------------------------------------------
# Ordered rules: first match wins. Spending categories are the ones a budget can
# target; Income, Transfer and Payments never count as spending.

RULES = [
    (r"PAYROLL", "Income", "income"),
    (r"ONLINE TRANSFER", "Transfer", "transfer"),
    (r"ZELLE PAYMENT FROM", "Payments", "p2p_in"),
    (r"ZELLE PAYMENT TO", "Payments", "p2p_out"),
    (r"MGMT.*RENT|RENT\b", "Rent", "purchase"),
    (r"CON ?ED|SPECTRUM", "Utilities", "purchase"),
    (r"SPOTIFY|MINT MOBILE|NETFLIX", "Subscriptions", "purchase"),
    (r"MTA|UBER \*TRIP|LYFT", "Transport", "purchase"),
    (r"UBER EATS|DOORDASH|GRUBHUB", "Food Delivery", "purchase"),
    (r"TRADER JOE|KEY FOOD|WHOLE FOODS", "Groceries", "purchase"),
    (r"COFFEE|DUNKIN|PASTRY", "Coffee", "purchase"),
    (r"BAR\b|TAVERN|PUB\b", "Nightlife", "purchase"),
    (r"SWEETGREEN|CHIPOTLE|JUNZI|GRAND TIER|TOMO|PATIALA|JING FONG|PICKLES|RESTAURANT", "Dining", "purchase"),
    (r"AMAZON|UNIQLO|H&M|TARGET|SEPHORA|ZARA", "Shopping", "purchase"),
    (r"AMC|CINEMA|TICKETS", "Entertainment", "purchase"),
    (r"THREADING|SALON|CVS", "Personal Care", "purchase"),
]

SPENDING_CATEGORIES = [
    "Rent", "Utilities", "Subscriptions", "Groceries", "Dining", "Coffee", "Food Delivery",
    "Nightlife", "Transport", "Shopping", "Entertainment", "Personal Care", "Household", "Other",
]
FIXED_CATEGORIES = ["Rent", "Utilities", "Subscriptions"]  # covered by obligations, not flexible targets

# Splitwise categories -> our categories
SPLITWISE_CATEGORIES = {"Dining out": "Dining", "Utilities": "Utilities", "Household": "Household",
                        "Entertainment": "Entertainment", "Groceries": "Groceries"}


def categorize(description: str) -> tuple[str, str]:
    """Return (category, kind) for a bank description."""
    for pattern, category, kind in RULES:
        if re.search(pattern, description, re.IGNORECASE):
            return category, kind
    return "Other", "purchase"


def merchant_name(description: str) -> str:
    """A readable merchant from a raw bank description: 'SQ *TOMO SUSHI NEW YORK NY' -> 'Tomo Sushi'."""
    d = re.sub(r"^(SQ|TST)\s?\*\s?", "", description, flags=re.IGNORECASE)
    d = re.split(r"\s(NEW YORK|SAN FRANCISCO|HELP\.|AMZN|DES:|\d{3}-|#|\d{4,}|NY$)", d)[0]
    d = " ".join(w.capitalize() for w in d.strip(" *").split())  # "5TH AVE" -> "5th Ave", not "5Th Ave"
    return "MTA Subway" if d.startswith("Mta") else d or description


def person_from_zelle(description: str) -> str | None:
    m = re.search(r"ZELLE PAYMENT (?:FROM|TO) (\w+)", description, re.IGNORECASE)
    return m.group(1).title() if m else None


def cents(value: str) -> int:
    return round(float(value) * 100)


def iso(us_date: str) -> str:
    return datetime.strptime(us_date, "%m/%d/%Y").date().isoformat()


# --- Importers -----------------------------------------------------------------


def add_bank_txn(conn, account_id, day, amount_cents, description, source="statement"):
    category, kind = categorize(description)
    person = person_from_zelle(description) if kind.startswith("p2p") else None
    cur = conn.execute(
        "INSERT INTO transactions (account_id, date, amount_cents, description, merchant, category, kind, person, source)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (account_id, day, amount_cents, description, merchant_name(description), category, kind, person, source),
    )
    return cur.lastrowid


def import_chase(conn, path: Path, account_id="chase"):
    rows = list(csv.DictReader(open(path)))
    first = rows[0]
    opening = cents(first["Balance"]) - cents(first["Amount"])
    conn.execute("INSERT INTO accounts VALUES (?, ?, ?, ?, ?)",
                 (account_id, "Chase Total Checking", "spending", opening, iso(first["Posting Date"])))
    for r in rows:
        add_bank_txn(conn, account_id, iso(r["Posting Date"]), cents(r["Amount"]), r["Description"])


def import_boa(conn, path: Path, account_id="boa"):
    lines = list(csv.reader(open(path)))
    header = next(i for i, row in enumerate(lines) if row[:2] == ["Date", "Description"])
    rows = [r for r in lines[header + 1:] if r and r[2]]
    opening_row = lines[header + 1]
    conn.execute("INSERT INTO accounts VALUES (?, ?, ?, ?, ?)",
                 (account_id, "Bank of America Advantage", "salary", cents(opening_row[3]), iso(opening_row[0])))
    for d, desc, amount, _ in rows:
        add_bank_txn(conn, account_id, iso(d), cents(amount), desc)


def import_splitwise(conn, path: Path):
    """Each row holds every member's net change. Positive = they paid more than their share."""
    reader = csv.DictReader(open(path))
    people = reader.fieldnames[5:]
    name = lambda p: "me" if p == ME else p
    for r in reader:
        net = {p: cents(r[p]) for p in people if cents(r[p]) != 0}
        cost = cents(r["Cost"])
        if r["Category"] == "Payment":
            payer = next(p for p, v in net.items() if v > 0)
            payee = next(p for p, v in net.items() if v < 0)
            conn.execute("INSERT INTO settlements (date, from_person, to_person, amount_cents) VALUES (?, ?, ?, ?)",
                         (r["Date"], name(payer), name(payee), cost))
            continue
        payer = next(p for p, v in net.items() if v > 0)
        cur = conn.execute(
            "INSERT INTO shared_expenses (date, description, category, cost_cents, paid_by) VALUES (?, ?, ?, ?, ?)",
            (r["Date"], r["Description"], SPLITWISE_CATEGORIES.get(r["Category"], "Other"), cost, name(payer)))
        for p in people:
            # Payer's share is what they paid minus what others owe them back.
            owed = cost - net[p] if p == payer else -net.get(p, 0)
            if owed:
                conn.execute("INSERT INTO shared_shares VALUES (?, ?, ?)", (cur.lastrowid, name(p), owed))


# --- Linking: the cross-source reasoning starts here -----------------------------


def link_transfers(conn):
    """Pair an outflow in one account with an equal inflow in another within 3 days."""
    outs = conn.execute("SELECT txn_id, account_id, date, amount_cents FROM transactions "
                        "WHERE kind = 'transfer' AND amount_cents < 0 AND transfer_id IS NULL").fetchall()
    for txn_id, account, day, amount in outs:
        match = conn.execute(
            "SELECT txn_id FROM transactions WHERE kind = 'transfer' AND transfer_id IS NULL AND account_id != ? "
            "AND amount_cents = ? AND abs(julianday(date) - julianday(?)) <= 3 ORDER BY date LIMIT 1",
            (account, -amount, day)).fetchone()
        if match:
            conn.execute("UPDATE transactions SET transfer_id = ? WHERE txn_id IN (?, ?)", (txn_id, txn_id, match[0]))


def link_shared_expenses(conn):
    """Find the bank charge behind each shared expense the user paid for."""
    rows = conn.execute("SELECT expense_id, date, cost_cents FROM shared_expenses "
                        "WHERE paid_by = 'me' AND txn_id IS NULL").fetchall()
    for expense_id, day, cost in rows:
        match = conn.execute(
            "SELECT txn_id FROM transactions WHERE kind = 'purchase' AND amount_cents = ? "
            "AND abs(julianday(date) - julianday(?)) <= 2 "
            "AND txn_id NOT IN (SELECT txn_id FROM shared_expenses WHERE txn_id IS NOT NULL) LIMIT 1",
            (-cost, day)).fetchone()
        if match:
            conn.execute("UPDATE shared_expenses SET txn_id = ? WHERE expense_id = ?", (match[0], expense_id))


def link_settlements(conn):
    """Match each settle-up to a Zelle with the same person and amount within 3 days."""
    rows = conn.execute("SELECT settlement_id, date, from_person, to_person, amount_cents FROM settlements "
                        "WHERE txn_id IS NULL").fetchall()
    for sid, day, src, dst, amount in rows:
        person, kind, signed = (src, "p2p_in", amount) if dst == "me" else (dst, "p2p_out", -amount)
        match = conn.execute(
            "SELECT txn_id FROM transactions WHERE kind = ? AND person = ? AND amount_cents = ? "
            "AND abs(julianday(date) - julianday(?)) <= 3 "
            "AND txn_id NOT IN (SELECT txn_id FROM settlements WHERE txn_id IS NOT NULL) LIMIT 1",
            (kind, person, signed, day)).fetchone()
        if match:
            conn.execute("UPDATE settlements SET txn_id = ? WHERE settlement_id = ?", (match[0], sid))


# --- The starting plan -----------------------------------------------------------

MONTHLY_CAP_CENTS = 180_000  # $1,800 a month, rent included

OBLIGATIONS = [
    # name, category, amount_cents, variable, source, match, due_day
    ("Rent", "Rent", 125_000, 0, "bank", "RENT", 1),
    ("Spotify", "Subscriptions", 1_199, 0, "bank", "SPOTIFY", 7),
    ("Phone (Mint Mobile)", "Subscriptions", 3_000, 0, "bank", "MINT MOBILE", 21),
    ("Electricity share (ConEd)", "Utilities", 0, 1, "shared", "ConEd", 14),
    ("Internet share (Spectrum)", "Utilities", 2_167, 0, "shared", "Spectrum", 16),
]


def seed_plan(conn, as_of: date):
    """Write the obligations and a starting budget learned from past spending.

    Variable obligations are estimated from the last three months. Flexible
    category targets are each category's average monthly spend before the demo
    month, scaled so that obligations + targets = the monthly cap exactly.
    """
    import finance  # local import: finance depends on this module's schema

    conn.execute("INSERT INTO budget VALUES ('monthly_cap_cents', ?)", (MONTHLY_CAP_CENTS,))
    conn.executemany("INSERT INTO obligations VALUES (?, ?, ?, ?, ?, ?, ?)", OBLIGATIONS)
    for name, _, _, variable, source, match, _ in OBLIGATIONS:
        if variable:
            est = finance.estimate_shared_obligation(conn, match, as_of, months=3)
            conn.execute("UPDATE obligations SET amount_cents = ? WHERE name = ?", (est, name))

    averages = finance.historical_category_averages(conn, as_of, months=6)
    obligations_total = conn.execute("SELECT SUM(amount_cents) FROM obligations").fetchone()[0]
    pool = MONTHLY_CAP_CENTS - obligations_total
    flexible = {c: v for c, v in averages.items() if c not in FIXED_CATEGORIES and v > 0}
    scale = pool / sum(flexible.values())
    targets = {c: round(v * scale / 500) * 500 for c, v in flexible.items()}  # round to $5
    # Put any rounding remainder on the largest category so targets sum to the pool.
    largest = max(targets, key=targets.get)
    targets[largest] += pool - sum(targets.values())
    conn.executemany("INSERT INTO budget_categories VALUES (?, ?)", targets.items())


# --- Building the seed database and per-session copies --------------------------

DEMO_START = "2026-09-18"  # the simulated "today" every new session starts on (a Friday)


def build_seed() -> sqlite3.Connection:
    """Read the statement files into an in-memory database. Runs once at startup."""
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.executescript(SCHEMA)
    import_chase(conn, STATEMENTS / "chase_spending.csv")
    import_boa(conn, STATEMENTS / "boa_salary.csv")
    import_splitwise(conn, STATEMENTS / "splitwise.csv")
    link_transfers(conn)
    link_shared_expenses(conn)
    link_settlements(conn)
    last = conn.execute("SELECT MAX(date) FROM transactions").fetchone()[0]
    conn.executemany("INSERT INTO meta VALUES (?, ?)",
                     [("today", DEMO_START), ("start", DEMO_START), ("last_day", last)])
    seed_plan(conn, date.fromisoformat(DEMO_START))
    conn.commit()
    return conn


def session_copy(seed: sqlite3.Connection) -> sqlite3.Connection:
    """A private, writable copy of the seed for one chat session."""
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    seed.backup(conn)
    return conn


def today(conn) -> date:
    return date.fromisoformat(conn.execute("SELECT value FROM meta WHERE key = 'today'").fetchone()[0])
