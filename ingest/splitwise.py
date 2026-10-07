"""Splitwise connector: get_expenses -> shared expenses, shares and settlements.

Splitwise returns every expense with each member's paid_share and owed_share.
Payments ("settle up") are expenses with payment = true. Each sync asks only for
expenses updated after the newest one already seen; deleted expenses come back
with deleted_at set and are removed.
"""

import json

import db

# Splitwise category -> Penny category
CATEGORY = {"Electricity": "Utilities", "TV/Phone/Internet": "Utilities", "Water": "Utilities", "Heat/gas": "Utilities",
            "Household supplies": "Household", "Dining out": "Dining", "Groceries": "Groceries",
            "Movies": "Entertainment", "Liquor": "Nightlife", "Taxi": "Transport"}


def _cents(s: str) -> int:
    return round(float(s) * 100)


def person_name(conn, user: dict, me_id: int) -> str:
    if user["user_id"] == me_id:
        return "me"
    u = user["user"]
    row = conn.execute("SELECT name FROM people WHERE splitwise_user_id = ?", (user["user_id"],)).fetchone()
    if row:
        return row[0]
    full = f"{u['first_name']} {u.get('last_name') or ''}".strip()
    conn.execute("INSERT OR IGNORE INTO people VALUES (?, ?, ?)", (u["first_name"], full, user["user_id"]))
    return u["first_name"]


def sync(conn, client, connection: tuple) -> dict:
    connection_id, me_id, cursor = connection
    me_id = int(me_id)
    today = db.today(conn).isoformat()
    added, modified, removed, new_items = 0, 0, 0, []
    newest, offset = cursor, 0
    while True:
        resp = client.get_expenses(updated_after=cursor, limit=100, offset=offset)
        if not resp["expenses"]:
            break
        conn.execute("INSERT INTO raw_events (connection_id, received_on, endpoint, payload) VALUES (?, ?, ?, ?)",
                     (connection_id, today, "/get_expenses", json.dumps(resp)))
        for e in resp["expenses"]:
            newest = max(newest or "", e["updated_at"])
            ext = str(e["id"])
            if e.get("deleted_at"):
                removed += conn.execute("DELETE FROM shared_expenses WHERE external_id = ?", (ext,)).rowcount
                removed += conn.execute("DELETE FROM settlements WHERE external_id = ?", (ext,)).rowcount
                continue
            users = [(person_name(conn, u, me_id), _cents(u["paid_share"]), _cents(u["owed_share"])) for u in e["users"]]
            day = e["date"][:10]
            if e["payment"]:
                frm = next(n for n, paid, _ in users if paid > 0)
                to = next(n for n, _, owed in users if owed > 0)
                cur = conn.execute(
                    """INSERT INTO settlements (external_id, date, from_person, to_person, amount_cents) VALUES (?, ?, ?, ?, ?)
                       ON CONFLICT(external_id) DO UPDATE SET date = excluded.date, from_person = excluded.from_person,
                       to_person = excluded.to_person, amount_cents = excluded.amount_cents""",
                    (ext, day, frm, to, _cents(e["cost"])))
                desc = f"{'You' if frm == 'me' else frm} paid {'you' if to == 'me' else to} ${float(e['cost']):.2f}"
            else:
                payer = max(users, key=lambda u: u[1])[0]
                exists = conn.execute("SELECT expense_id FROM shared_expenses WHERE external_id = ?", (ext,)).fetchone()
                if exists:
                    conn.execute("UPDATE shared_expenses SET date = ?, description = ?, category = ?, cost_cents = ?, "
                                 "paid_by = ? WHERE expense_id = ?",
                                 (day, e["description"], CATEGORY.get(e["category"]["name"], "Other"), _cents(e["cost"]),
                                  payer, exists[0]))
                    expense_id = exists[0]
                    conn.execute("DELETE FROM shared_shares WHERE expense_id = ?", (expense_id,))
                else:
                    expense_id = conn.execute(
                        "INSERT INTO shared_expenses (external_id, date, description, category, cost_cents, paid_by) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (ext, day, e["description"], CATEGORY.get(e["category"]["name"], "Other"), _cents(e["cost"]),
                         payer)).lastrowid
                conn.executemany("INSERT INTO shared_shares VALUES (?, ?, ?)",
                                 [(expense_id, n, owed) for n, _, owed in users if owed])
                desc = f"{e['description']} (${float(e['cost']):.2f}, paid by {'you' if payer == 'me' else payer})"
            if e["created_at"] == e["updated_at"]:
                added += 1
                new_items.append({"description": desc})
            else:
                modified += 1
        offset += len(resp["expenses"])

    conn.execute("UPDATE connections SET cursor = ?, last_synced = ? WHERE connection_id = ?",
                 (newest, today, connection_id))
    return {"institution": "Splitwise", "provider": "splitwise", "added": added, "modified": modified,
            "removed": removed, "new": new_items}
