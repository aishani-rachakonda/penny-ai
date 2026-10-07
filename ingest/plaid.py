"""Plaid connector: /transactions/sync responses -> accounts and transactions.

Handles the parts of Plaid's model that matter for correctness:
- Sign: Plaid reports money out as a positive amount; Penny stores it negative.
- Pending -> posted: when a posted transaction arrives with pending_transaction_id,
  the pending row is updated in place (keeping any links to it) instead of
  being deleted and re-added.
- Cursors: each sync asks only for what changed since the last one, following
  has_more until the update is complete.
"""

import json
import re

import db


# Plaid personal_finance_category (detailed, then primary) -> Penny category
PFC_TO_CATEGORY = {
    "FOOD_AND_DRINK_COFFEE": "Coffee", "FOOD_AND_DRINK_GROCERIES": "Groceries",
    "FOOD_AND_DRINK_BEER_WINE_AND_LIQUOR": "Nightlife", "FOOD_AND_DRINK_FAST_FOOD": "Dining",
    "FOOD_AND_DRINK_RESTAURANT": "Dining", "ENTERTAINMENT_MUSIC_AND_AUDIO": "Subscriptions",
    "RENT_AND_UTILITIES_RENT": "Rent", "RENT_AND_UTILITIES_TELEPHONE": "Utilities",
    "PERSONAL_CARE_GYMS_AND_FITNESS_CENTERS": "Personal Care",
    "GENERAL_SERVICES_OTHER_GENERAL_SERVICES": "Subscriptions",
    # primary fallbacks
    "FOOD_AND_DRINK": "Dining", "TRANSPORTATION": "Transport", "GENERAL_MERCHANDISE": "Shopping",
    "ENTERTAINMENT": "Entertainment", "RENT_AND_UTILITIES": "Utilities", "PERSONAL_CARE": "Personal Care",
    "GENERAL_SERVICES": "Other", "INCOME": "Income", "TRANSFER_IN": "Transfer", "TRANSFER_OUT": "Transfer",
}


def categorize(conn, merchant: str, pfc: dict | None) -> tuple[str, str]:
    """(category, source). A merchant rule (built-in or the user's correction) beats Plaid's category."""
    rule = conn.execute("SELECT category, source FROM merchant_rules WHERE merchant = ?", (merchant.lower(),)).fetchone()
    if rule:
        return rule[0], "user_rule" if rule[1] == "user" else "penny_rule"
    if pfc:
        for key in (pfc.get("detailed"), pfc.get("primary")):
            if key in PFC_TO_CATEGORY:
                return PFC_TO_CATEGORY[key], "plaid"
    return "Other", "plaid"


def person_for(conn, counterparty: str) -> str:
    row = conn.execute("SELECT name FROM people WHERE lower(full_name) = lower(?)", (counterparty,)).fetchone()
    return row[0] if row else counterparty.split()[0].title()


def clean_name(name: str) -> str:
    name = re.split(r"\s(DES:|ID:|Conf#|JPM\d|\d{6,})", name)[0]
    return " ".join(w.capitalize() for w in name.split())


def account_slug(conn, institution: str, subtype: str) -> str:
    words = institution.split()
    slug = institution.lower() if len(words) == 1 else "".join(w[0] for w in words).lower()
    exists = conn.execute("SELECT 1 FROM accounts WHERE account_id = ?", (slug,)).fetchone()
    return f"{slug}_{subtype}" if exists else slug


def normalize(conn, t: dict, account_id: str) -> dict:
    """One Plaid transaction -> a row for Penny's transactions table."""
    pfc = t.get("personal_finance_category") or {}
    primary = pfc.get("primary", "")
    individual = next((c["name"] for c in t.get("counterparties", []) if c.get("type") == "individual"), None)
    person = None
    if primary == "INCOME":
        kind = "income"
    elif primary in ("TRANSFER_IN", "TRANSFER_OUT"):
        kind = ("p2p_in" if primary == "TRANSFER_IN" else "p2p_out") if individual else "transfer"
        person = person_for(conn, individual) if individual else None
    else:
        kind = "purchase"
    merchant = t.get("merchant_name") or (f"Zelle {'from' if kind == 'p2p_in' else 'to'} {individual}"
                                         if individual else clean_name(t["name"]))
    if kind == "purchase":
        category, cat_source = categorize(conn, merchant, pfc)
    else:
        category, cat_source = {"income": "Income", "transfer": "Transfer"}.get(kind, "Payments"), "plaid"
    return {
        "account_id": account_id, "external_id": t["transaction_id"],
        "date": t.get("authorized_date") or t["date"], "posted_date": None if t["pending"] else t["date"],
        "pending": int(t["pending"]), "amount_cents": -round(t["amount"] * 100),
        "description": t["name"], "merchant": merchant, "category": category, "category_source": cat_source,
        "provider_category": pfc.get("detailed"), "kind": kind, "person": person, "source": "plaid",
    }


COLUMNS = ["account_id", "external_id", "date", "posted_date", "pending", "amount_cents", "description", "merchant",
           "category", "category_source", "provider_category", "kind", "person", "source"]


def sync(conn, client, connection: tuple) -> dict:
    connection_id, institution, access_token, cursor = connection
    today = db.today(conn).isoformat()
    added, posted, removed, new_items = 0, 0, 0, []
    while True:
        resp = client.transactions_sync(access_token, cursor)
        conn.execute("INSERT INTO raw_events (connection_id, received_on, endpoint, payload) VALUES (?, ?, ?, ?)",
                     (connection_id, today, "/transactions/sync", json.dumps(resp)))

        accounts = {}
        for a in resp["accounts"]:
            row = conn.execute("SELECT account_id FROM accounts WHERE external_id = ?", (a["account_id"],)).fetchone()
            slug = row[0] if row else account_slug(conn, institution, a.get("subtype") or "account")
            b = a["balances"]
            conn.execute(
                """INSERT INTO accounts (account_id, connection_id, external_id, name, mask, subtype,
                       balance_current_cents, balance_available_cents, balance_as_of) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(external_id) DO UPDATE SET balance_current_cents = excluded.balance_current_cents,
                       balance_available_cents = excluded.balance_available_cents, balance_as_of = excluded.balance_as_of""",
                (slug, connection_id, a["account_id"], a.get("official_name") or a["name"], a.get("mask"),
                 a.get("subtype"), round(b["current"] * 100), round((b.get("available") or b["current"]) * 100), today))
            accounts[a["account_id"]] = slug

        removed_ids = {r["transaction_id"] for r in resp["removed"]}
        for t in resp["added"] + resp["modified"]:
            account_id = accounts.get(t["account_id"]) or conn.execute(
                "SELECT account_id FROM accounts WHERE external_id = ?", (t["account_id"],)).fetchone()[0]
            row = normalize(conn, t, account_id)
            pending_id = t.get("pending_transaction_id")
            existing = conn.execute("SELECT txn_id FROM transactions WHERE external_id IN (?, ?)",
                                    (t["transaction_id"], pending_id or "")).fetchone()
            if existing:  # pending -> posted (or a modification): update in place, keep links
                sets = ", ".join(f"{c} = ?" for c in COLUMNS)
                conn.execute(f"UPDATE transactions SET {sets} WHERE txn_id = ?", [row[c] for c in COLUMNS] + [existing[0]])
                removed_ids.discard(pending_id)
                posted += 1
            else:
                conn.execute(f"INSERT INTO transactions ({', '.join(COLUMNS)}) VALUES ({', '.join('?' * len(COLUMNS))})",
                             [row[c] for c in COLUMNS])
                added += 1
                new_items.append({"merchant": row["merchant"], "amount": row["amount_cents"] / 100,
                                  "pending": bool(row["pending"]), "account": account_id})
        for ext in removed_ids:
            removed += conn.execute("DELETE FROM transactions WHERE external_id = ?", (ext,)).rowcount

        cursor = resp["next_cursor"]
        if not resp["has_more"]:
            break

    conn.execute("UPDATE connections SET cursor = ?, last_synced = ? WHERE connection_id = ?",
                 (cursor, today, connection_id))
    return {"institution": institution, "provider": "plaid", "added": added, "modified": posted, "removed": removed,
            "new": new_items}
