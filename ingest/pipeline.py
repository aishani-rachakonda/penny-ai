"""The ingestion pipeline: connect sources, sync them, link them, detect patterns.

    connect_sources   once, at onboarding: record each Plaid Item and the Splitwise account
    sync_all          every time new data may exist (production: on Plaid's SYNC_UPDATES_AVAILABLE
                      webhook and on a schedule; simulation: whenever the simulated day advances
                      or the user writes to Splitwise):
                        1. pull changes from every connection (ingest/plaid.py, ingest/splitwise.py)
                        2. reconcile manual entries with the bank transactions that confirm them
                        3. link across sources: transfers between own accounts, Splitwise expenses
                           to the bank charges that paid for them, settle-ups to Zelle payments
                        4. re-run recurring payment detection (recurring.py)
"""

import json

import db
import recurring
from ingest import clients, plaid, splitwise


def connect_sources(conn):
    """Store the user's connections. Production: after Plaid Link and Splitwise OAuth complete."""
    p = clients.plaid(conn)
    for item in p.link_tokens():
        conn.execute("INSERT INTO connections (provider, institution, access_token, external_id) VALUES (?, ?, ?, ?)",
                     ("plaid", item["institution_name"], item["access_token"], item["item_id"]))
    s = clients.splitwise(conn)
    me = s.get_current_user()["user"]
    conn.execute("INSERT INTO connections (provider, institution, external_id) VALUES ('splitwise', 'Splitwise', ?)",
                 (str(me["id"]),))
    for f in s.get_friends()["friends"]:
        conn.execute("INSERT OR IGNORE INTO people VALUES (?, ?, ?)",
                     (f["first_name"], f"{f['first_name']} {f.get('last_name') or ''}".strip(), f["id"]))


def sync_all(conn, only: str | None = None) -> dict:
    today = db.today(conn).isoformat()
    reports = []
    p, s = clients.plaid(conn), clients.splitwise(conn)
    for cid, provider, institution, token, ext, cursor in conn.execute(
            "SELECT connection_id, provider, institution, access_token, external_id, cursor FROM connections").fetchall():
        if only and provider != only:
            continue
        if provider == "plaid":
            r = plaid.sync(conn, p, (cid, institution, token, cursor))
        else:
            r = splitwise.sync(conn, s, (cid, ext, cursor))
        conn.execute("INSERT INTO sync_log (sim_date, connection_id, added, modified, removed, detail) VALUES (?, ?, ?, ?, ?, ?)",
                     (today, cid, r["added"], r["modified"], r["removed"], json.dumps(r["new"][-50:])))
        reports.append(r)
    matched = reconcile_manual(conn)
    link_transfers(conn)
    link_shared_expenses(conn)
    link_settlements(conn)
    recurring.detect(conn)
    return {"date": today, "sources": reports, "manual_entries_confirmed": matched}


def reconcile_manual(conn) -> int:
    """A transaction the user reported in chat is replaced by the bank's version once it arrives.

    A match needs the same account, amount and kind within 3 days, plus evidence it's the
    same thing: the same person (for payments) or a shared merchant word or category
    (for purchases). Amount alone isn't enough: a $30 Zara purchase must not be
    "confirmed" by a $30 phone bill.
    """
    matched = 0
    for txn_id, account, day, amount, kind, merchant, category, person in conn.execute(
            "SELECT txn_id, account_id, date, amount_cents, kind, merchant, category, person FROM transactions "
            "WHERE source = 'manual'").fetchall():
        words = {w for w in merchant.lower().split() if len(w) > 2}
        for bank_id, bank_merchant, bank_category, bank_person in conn.execute(
                """SELECT txn_id, merchant, category, person FROM transactions WHERE source = 'plaid' AND account_id = ?
                   AND amount_cents = ? AND kind = ? AND abs(julianday(date) - julianday(?)) <= 3
                   ORDER BY abs(julianday(date) - julianday(?))""", (account, amount, kind, day, day)).fetchall():
            same = (person and person == bank_person) if kind.startswith("p2p") else \
                bool(words & set(bank_merchant.lower().split())) or category == bank_category
            if same:
                conn.execute("UPDATE shared_expenses SET txn_id = ? WHERE txn_id = ?", (bank_id, txn_id))
                conn.execute("UPDATE settlements SET txn_id = ? WHERE txn_id = ?", (bank_id, txn_id))
                conn.execute("DELETE FROM transactions WHERE txn_id = ?", (txn_id,))
                matched += 1
                break
    return matched


def link_transfers(conn):
    """Pair an outflow in one account with an equal inflow in another within 3 days."""
    for txn_id, account, day, amount in conn.execute(
            "SELECT txn_id, account_id, date, amount_cents FROM transactions "
            "WHERE kind = 'transfer' AND amount_cents < 0 AND transfer_id IS NULL").fetchall():
        match = conn.execute(
            "SELECT txn_id FROM transactions WHERE kind = 'transfer' AND transfer_id IS NULL AND account_id != ? "
            "AND amount_cents = ? AND abs(julianday(date) - julianday(?)) <= 3 ORDER BY date LIMIT 1",
            (account, -amount, day)).fetchone()
        if match:
            conn.execute("UPDATE transactions SET transfer_id = ? WHERE txn_id IN (?, ?)", (txn_id, txn_id, match[0]))


def link_shared_expenses(conn):
    """Find the bank charge behind each shared expense the user paid for (same amount, within 3 days)."""
    conn.execute("UPDATE shared_expenses SET txn_id = NULL, link_source = NULL WHERE link_source = 'detected'")
    for expense_id, day, cost in conn.execute(
            "SELECT expense_id, date, cost_cents FROM shared_expenses WHERE paid_by = 'me' AND txn_id IS NULL").fetchall():
        match = conn.execute(
            """SELECT txn_id FROM transactions WHERE kind = 'purchase' AND amount_cents = ?
               AND abs(julianday(date) - julianday(?)) <= 3
               AND txn_id NOT IN (SELECT txn_id FROM shared_expenses WHERE txn_id IS NOT NULL)
               ORDER BY abs(julianday(date) - julianday(?)) LIMIT 1""", (-cost, day, day)).fetchone()
        if match:
            conn.execute("UPDATE shared_expenses SET txn_id = ?, link_source = 'detected' WHERE expense_id = ?",
                         (match[0], expense_id))


def link_settlements(conn):
    """Match each Splitwise settle-up to a Zelle with the same person and amount within 3 days."""
    conn.execute("UPDATE settlements SET txn_id = NULL, link_source = NULL WHERE link_source = 'detected'")
    for sid, day, src, dst, amount in conn.execute(
            "SELECT settlement_id, date, from_person, to_person, amount_cents FROM settlements WHERE txn_id IS NULL").fetchall():
        person, kind, signed = (src, "p2p_in", amount) if dst == "me" else (dst, "p2p_out", -amount)
        match = conn.execute(
            """SELECT txn_id FROM transactions WHERE kind = ? AND person = ? AND amount_cents = ?
               AND abs(julianday(date) - julianday(?)) <= 3
               AND txn_id NOT IN (SELECT txn_id FROM settlements WHERE txn_id IS NOT NULL) LIMIT 1""",
            (kind, person, signed, day)).fetchone()
        if match:
            conn.execute("UPDATE settlements SET txn_id = ?, link_source = 'detected' WHERE settlement_id = ?",
                         (match[0], sid))
