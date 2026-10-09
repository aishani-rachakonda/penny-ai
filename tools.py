"""The tools the harness can run, and the JSON that describes them to the model.

Every tool takes the session's database connection first, then the model's
arguments. Tools validate their inputs and return errors as JSON the model can
act on (what was wrong and what the valid options are) instead of raising.

Results say where facts came from (synced from Plaid or Splitwise, detected by
Penny, or provided by the user) so the agent can tell the user how it knows.
"""

import difflib
import json
from datetime import date, timedelta

import db
import finance
import planning
import recurring
from finance import dollars
from ingest import clients, pipeline
from sandbox.providers import ProviderError


class ToolError(Exception):
    """Raised inside a tool; run_tool turns it into {"error": ...} for the model."""


# --- Argument helpers --------------------------------------------------------------


def _category(conn, name: str | None, allow_none: bool = False, create: bool = False) -> str | None:
    if name is None and allow_none:
        return None
    known = db.categories(conn)
    lookup = {c.lower(): c for c in known}
    key = (name or "").strip().lower()
    if key in lookup:
        return lookup[key]
    close = difflib.get_close_matches(key, lookup, n=3, cutoff=0.6)
    if create and not close and key:
        return db.ensure_category(conn, name.strip().title(), source="user")
    hint = f" Did you mean: {', '.join(lookup[c] for c in close)}?" if close else ""
    extra = " To make a new category, pass create_category=true." if create is False and not close else ""
    raise ToolError(f"Unknown category '{name}'.{hint} Categories: {', '.join(known)}.{extra}")


def _account(conn, name: str) -> str:
    rows = conn.execute("SELECT a.account_id, c.institution, a.mask FROM accounts a JOIN connections c USING (connection_id)").fetchall()
    key = (name or "").strip().lower()
    for account, institution, mask in rows:
        if key in (account, institution.lower(), mask) or key in institution.lower() or account in key:
            return account
    raise ToolError(f"Unknown account '{name}'. Accounts: " + ", ".join(f"{a} ({i} ...{m})" for a, i, m in rows) + ".")


def _date(value: str | None, default: date, conn) -> date:
    if not value:
        return default
    try:
        d = date.fromisoformat(value)
    except ValueError:
        raise ToolError(f"Date '{value}' isn't in YYYY-MM-DD format, e.g. '2026-09-01'.")
    today = db.today(conn)
    if d > today:
        raise ToolError(f"{value} is in the future. Today is {today.isoformat()}; no data exists after it.")
    return d


def _month(conn, value: str | None) -> str:
    current = planning.month_key(db.today(conn))
    if not value:
        return current
    try:
        month = date.fromisoformat(value[:7] + "-01").strftime("%Y-%m")
    except ValueError:
        raise ToolError(f"Month '{value}' isn't in YYYY-MM format, e.g. '2026-10'.")
    if month < current:
        raise ToolError(f"{month} is in the past; plans can be set for {current} or later.")
    return month


def _amount(value) -> int:
    try:
        c = round(float(value) * 100)
    except (TypeError, ValueError):
        raise ToolError(f"Amount '{value}' isn't a number. Pass dollars as a number, e.g. 30 or 12.50.")
    if c <= 0:
        raise ToolError("Amount must be a positive number of dollars.")
    return c


def _person(conn, name: str) -> tuple[str, int]:
    people = conn.execute("SELECT name, splitwise_user_id FROM people ORDER BY name").fetchall()
    for n, uid in people:
        if name.strip().lower() in (n.lower(), n.lower().split()[0]):
            return n, uid
    raise ToolError(f"'{name}' isn't one of your Splitwise friends. Friends: {', '.join(n for n, _ in people)}.")


# --- Read tools --------------------------------------------------------------------


def get_financial_position(conn) -> dict:
    return finance.financial_position(conn)


def query_transactions(conn, start_date=None, end_date=None, account=None, category=None,
                       merchant_contains=None, min_amount=None, max_amount=None, direction="any", limit=25) -> dict:
    today = db.today(conn)
    start = _date(start_date, today.replace(day=1), conn)
    end = _date(end_date, today, conn)
    sql = ["SELECT txn_id, date, account_id, merchant, category, amount_cents, kind, person, pending, source, "
           "category_source FROM transactions WHERE date BETWEEN ? AND ?"]
    params: list = [start.isoformat(), end.isoformat()]
    if account:
        sql.append("AND account_id = ?"); params.append(_account(conn, account))
    if category:
        sql.append("AND category = ?"); params.append(_category(conn, category))
    if merchant_contains:
        sql.append("AND (merchant LIKE ? OR description LIKE ?)"); params += [f"%{merchant_contains}%"] * 2
    if direction == "spent":
        sql.append("AND amount_cents < 0")
    elif direction == "received":
        sql.append("AND amount_cents > 0")
    if min_amount is not None:
        sql.append("AND abs(amount_cents) >= ?"); params.append(round(float(min_amount) * 100))
    if max_amount is not None:
        sql.append("AND abs(amount_cents) <= ?"); params.append(round(float(max_amount) * 100))
    rows = conn.execute(" ".join(sql) + " ORDER BY date DESC, txn_id DESC", params).fetchall()
    limit = max(1, min(int(limit or 25), 100))
    return {
        "count": len(rows), "total": dollars(sum(r[5] for r in rows)), "showing": min(limit, len(rows)),
        "transactions": [{"id": r[0], "date": r[1], "account": r[2], "merchant": r[3], "category": r[4],
                          "amount": dollars(r[5]), "type": r[6], **({"person": r[7]} if r[7] else {}),
                          **({"pending": True} if r[8] else {}),
                          "source": "you told Penny" if r[9] == "manual" else "Plaid",
                          "categorized_by": {"plaid": "Plaid", "penny_rule": "Penny's rules",
                                             "user_rule": "your rule"}.get(r[10], r[10])}
                         for r in rows[:limit]],
        "note": "Amounts are signed: negative is money out. Split purchases show the full charge; "
                "summarize_spending counts only your share.",
    }


def summarize_spending(conn, start_date=None, end_date=None, group_by="category", category=None,
                       compare_to_previous_period=False) -> dict:
    today = db.today(conn)
    start = _date(start_date, today.replace(day=1), conn)
    end = _date(end_date, today, conn)
    if start > end:
        raise ToolError(f"start_date {start} is after end_date {end}.")
    if group_by not in ("category", "merchant", "week", "day"):
        raise ToolError("group_by must be one of: category, merchant, week, day.")
    cat = _category(conn, category, allow_none=True)

    def summarize(s: date, e: date) -> dict:
        items = [i for i in finance.spending_items(conn, s, e) if cat is None or i["category"] == cat]
        groups: dict[str, int] = {}
        for i in items:
            d = date.fromisoformat(i["date"])
            key = {"category": i["category"], "merchant": i["merchant"], "day": i["date"],
                   "week": (d - timedelta(days=d.weekday())).isoformat()}[group_by]
            groups[key] = groups.get(key, 0) + i["cents"]
        small = [i for i in items if 0 < i["cents"] < 1500]
        return {"start": s.isoformat(), "end": e.isoformat(), "total": dollars(sum(i["cents"] for i in items)),
                "purchases": len(items),
                "small_purchases_under_15": {"count": len(small), "total": dollars(sum(i["cents"] for i in small))},
                "groups": {k: dollars(v) for k, v in sorted(groups.items(), key=lambda kv: -kv[1])}}

    result = {"current": summarize(start, end),
              "note": "Your share only: split expenses count at your share; transfers and settle-ups are excluded."}
    if compare_to_previous_period:
        def back(d: date) -> date:
            m = finance.prev_month_start(d)
            return m.replace(day=min(d.day, finance.month_bounds(m)[1].day))
        prev = summarize(back(start), back(end))
        result["previous"] = prev
        result["change"] = {"total": round(result["current"]["total"] - prev["total"], 2),
                            "percent": round(100 * (result["current"]["total"] / prev["total"] - 1), 1)
                            if prev["total"] else None}
    return result


def get_budget_status(conn) -> dict:
    return finance.budget_status(conn)


def recommend_spending(conn, category, horizon="today") -> dict:
    if horizon not in ("today", "weekend", "rest_of_month"):
        raise ToolError("horizon must be 'today', 'weekend' or 'rest_of_month'.")
    cat = _category(conn, category)
    if cat in db.categories(conn, "bill"):
        raise ToolError(f"{cat} is covered by fixed bills, not a flexible budget. Use get_budget_status to see bills.")
    return finance.recommend(conn, cat, horizon)


def evaluate_purchase(conn, amount, category) -> dict:
    return finance.evaluate_purchase(conn, _amount(amount), _category(conn, category))


def get_shared_balances(conn, person=None) -> dict:
    result = finance.shared_balances(conn)
    if person:
        name, _ = _person(conn, person)
        result["people"] = [p for p in result["people"] if p["person"] == name] or \
                           [{"person": name, "balance": 0.0, "direction": "settled up"}]
        result["mismatches"] = [m for m in result["mismatches"] if m["person"] == name]
    result["source"] = "Splitwise (synced), cross-checked against Plaid bank transactions"
    return result


def get_recurring_payments(conn, include_income=False) -> dict:
    today = db.today(conn)
    in_plan = {b["detected_match"]: b["name"] for b in planning.bills_for_month(conn, today) if b["in_plan"]}
    out = []
    for s in recurring.series(conn):
        if s["direction"] == "in" and not include_income:
            continue
        out.append({
            "name": s["label"], "kind": s["kind"], "cadence": s["cadence"],
            "amount": s["last_cents"] / 100, "varies": bool(s["variable"]),
            "range": [s["low_cents"] / 100, s["high_cents"] / 100] if s["variable"] else None,
            "times_seen": s["occurrences"], "first_seen": s["first_date"], "last_charged": s["last_date"],
            "next_expected": s["next_due"], "status": s["status"], "price_change": s["price_change"],
            "confidence": s["confidence"],
            "in_your_plan": in_plan.get(s["label"]) or (False if s["direction"] == "out" else None),
            "dismissed": s["dismissed"],
        })
    return {"as_of": today.isoformat(), "recurring": out,
            "how": "Detected by Penny from your synced transactions: same merchant, regular rhythm, at least 3 times. "
                   "Nothing here was entered by you; use update_plan to confirm one into your plan or dismiss it."}


def draft_monthly_plan(conn, month=None) -> dict:
    return planning.draft_plan(conn, _month(conn, month))


# --- Write tools -------------------------------------------------------------------


def update_plan(conn, month=None, monthly_cap=None, bills_to_set=None, bills_to_remove=None,
                detected_to_dismiss=None, category_targets=None, categories_to_remove=None) -> dict:
    month = planning.ensure_plan(conn, _month(conn, month))
    if not any([monthly_cap, bills_to_set, bills_to_remove, detected_to_dismiss, category_targets, categories_to_remove]):
        raise ToolError("Nothing to change. Pass monthly_cap, bills_to_set, bills_to_remove, detected_to_dismiss, "
                        "category_targets or categories_to_remove.")
    today = db.today(conn).isoformat()
    changes = []
    if monthly_cap is not None:
        conn.execute("UPDATE plans SET monthly_cap_cents = ?, source = 'user', updated_on = ? WHERE month = ?",
                     (_amount(monthly_cap), today, month))
        changes.append(f"Monthly cap set to ${float(monthly_cap):,.2f}.")

    detected = {s["label"].lower(): s for s in recurring.series(conn)}
    for b in bills_to_set or []:
        if not isinstance(b, dict) or not b.get("name"):
            raise ToolError("Each bill needs at least a 'name', e.g. {'name': 'Rent', 'amount': 1650, "
                            "'due_day_start': 1, 'due_day_end': 10}.")
        name = b["name"].strip()
        existing = conn.execute("SELECT category, amount_cents, due_day_start, due_day_end, how_paid, match, source "
                                "FROM plan_bills WHERE month = ? AND lower(name) = lower(?)", (month, name)).fetchone()
        series = detected.get(name.lower()) or next((s for k, s in detected.items() if name.lower() in k), None)
        if existing:
            category, amount, lo, hi, how, match, source = existing
        elif series:  # confirming a detected payment into the plan
            name, category, how, source = series["label"], series["category"] or "Utilities", \
                "bank" if series["origin"] == "bank" else "splitwise", "detected"
            match = series["label"].replace(" (your share)", "")
            amount = None if series["variable"] else series["last_cents"]
            lo = hi = series["due_day_low"] or 1
        else:
            if b.get("due_day_start") is None:
                raise ToolError(f"New bill '{name}' needs due_day_start (and amount, unless it varies).")
            category, amount, lo, hi, how, match, source = "Utilities", None, b["due_day_start"], b["due_day_start"], \
                "bank", name.split()[0], "user"
        if "amount" in b:
            amount = _amount(b["amount"]) if b["amount"] is not None else None
        lo = int(b.get("due_day_start", lo)); hi = int(b.get("due_day_end", b.get("due_day_start", hi)))
        if not (1 <= lo <= hi <= 31):
            raise ToolError("Due days must satisfy 1 <= due_day_start <= due_day_end <= 31.")
        if b.get("category"):
            category = _category(conn, b["category"])
        match = b.get("match", match)
        conn.execute("INSERT OR REPLACE INTO plan_bills VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                     (month, name, category, amount, lo, hi, how, match, source))
        changes.append(f"Bill '{name}': {'$' + format(amount / 100, ',.2f') if amount else 'varies'}, due "
                       f"{lo}{'-' + str(hi) if hi != lo else ''}" + (" (confirmed from detection)." if series and not existing else "."))
    for name in bills_to_remove or []:
        n = conn.execute("DELETE FROM plan_bills WHERE month = ? AND lower(name) = lower(?)", (month, name)).rowcount
        if not n:
            names = [r[0] for r in conn.execute("SELECT name FROM plan_bills WHERE month = ?", (month,))]
            raise ToolError(f"No bill named '{name}' in the {month} plan. Bills: {', '.join(names)}.")
        changes.append(f"Removed bill '{name}'.")
    for name in detected_to_dismiss or []:
        s = detected.get(name.lower()) or next((s for k, s in detected.items() if name.lower() in k), None)
        if not s:
            raise ToolError(f"No detected recurring payment called '{name}'. Detected: {', '.join(x['label'] for x in detected.values())}.")
        conn.execute("INSERT OR REPLACE INTO recurring_verdicts VALUES (?, 'dismissed')", (s["series_key"],))
        changes.append(f"'{s['label']}' won't be counted as a bill.")

    for t in category_targets or []:
        if not isinstance(t, dict) or "category" not in t or "amount" not in t:
            raise ToolError("Each target needs 'category' and 'amount', e.g. {'category': 'Dining', 'amount': 80}.")
        cat = _category(conn, t["category"], create=bool(t.get("create_category")))
        if cat in db.categories(conn, "bill"):
            raise ToolError(f"{cat} is covered by bills; set the bill instead of a target.")
        conn.execute("INSERT OR REPLACE INTO plan_categories VALUES (?, ?, ?, 'user')", (month, cat, round(float(t["amount"]) * 100)))
        changes.append(f"{cat} target set to ${float(t['amount']):,.2f}.")
    for r in categories_to_remove or []:
        r = {"category": r} if isinstance(r, str) else r
        cat = _category(conn, r.get("category"))
        conn.execute("DELETE FROM plan_categories WHERE month = ? AND category = ?", (month, cat))
        if r.get("move_spending_to"):
            dest = _category(conn, r["move_spending_to"], create=bool(r.get("create_category")))
            merchants = [m for (m,) in conn.execute("SELECT DISTINCT merchant FROM transactions WHERE category = ?", (cat,))]
            conn.executemany("INSERT OR REPLACE INTO merchant_rules VALUES (?, ?, 'user')", [(m.lower(), dest) for m in merchants])
            conn.execute("UPDATE transactions SET category = ?, category_source = 'user_rule' WHERE category = ?", (dest, cat))
            conn.execute("UPDATE shared_expenses SET category = ? WHERE category = ?", (dest, cat))
            changes.append(f"Removed {cat}; its {len(merchants)} merchants now count as {dest}.")
        else:
            changes.append(f"Removed the {cat} target. Any spending there now counts as unplanned.")
        if conn.execute("SELECT source FROM categories WHERE name = ?", (cat,)).fetchone()[0] == "user" and \
                not conn.execute("SELECT 1 FROM transactions WHERE category = ?", (cat,)).fetchone():
            conn.execute("DELETE FROM categories WHERE name = ?", (cat,))

    conn.execute("UPDATE plans SET source = 'user', updated_on = ? WHERE month = ?", (today, month))
    status = finance.budget_status(conn) if month == planning.month_key(db.today(conn)) else None
    return {"month": month, "changes": changes, "totals": planning.plan_totals(conn, month),
            "flexible_left_this_month": status["flexible_left"] if status else None,
            "bills": [{"name": n, "amount": a / 100 if a else "varies", "due": f"{lo}-{hi}", "source": s}
                      for n, a, lo, hi, s in conn.execute("SELECT name, amount_cents, due_day_start, due_day_end, source "
                                                          "FROM plan_bills WHERE month = ?", (month,))],
            "targets": {c: t / 100 for c, t in conn.execute("SELECT category, target_cents FROM plan_categories "
                                                            "WHERE month = ? ORDER BY target_cents DESC", (month,))}}


def recategorize_merchant(conn, merchant, category, create_category=False) -> dict:
    merchants = [m for (m,) in conn.execute("SELECT DISTINCT merchant FROM transactions")]
    exact = [m for m in merchants if m.lower() == merchant.strip().lower()]
    matches = exact or [m for m in merchants if merchant.strip().lower() in m.lower()]
    if not matches:
        close = difflib.get_close_matches(merchant, merchants, n=4, cutoff=0.4)
        raise ToolError(f"No transactions from '{merchant}'." + (f" Did you mean: {', '.join(close)}?" if close else ""))
    cat = _category(conn, category, create=bool(create_category))
    conn.executemany("INSERT OR REPLACE INTO merchant_rules VALUES (?, ?, 'user')", [(m.lower(), cat) for m in matches])
    moved = conn.execute(f"UPDATE transactions SET category = ?, category_source = 'user_rule' WHERE merchant IN "
                         f"({', '.join('?' * len(matches))})", [cat] + matches).rowcount
    return {"merchants": matches, "now_category": cat, "transactions_moved": moved,
            "note": "Saved as your rule: future transactions from these merchants will be categorized this way too."}


def update_memory(conn, add_note=None, remove_note_id=None) -> dict:
    if not add_note and remove_note_id is None:
        raise ToolError("Pass add_note (text to remember) or remove_note_id.")
    if add_note:
        conn.execute("INSERT INTO memory_notes (text, created_on) VALUES (?, ?)", (add_note.strip(), db.today(conn).isoformat()))
    if remove_note_id is not None:
        if not conn.execute("DELETE FROM memory_notes WHERE note_id = ?", (int(remove_note_id),)).rowcount:
            raise ToolError(f"No note with id {remove_note_id}.")
    return {"notes": [{"id": i, "text": t, "since": d} for i, t, d in
                      conn.execute("SELECT note_id, text, created_on FROM memory_notes ORDER BY note_id")]}


def add_transaction(conn, account, amount, direction, description, category=None, person=None, to_account=None,
                    new_category=None) -> dict:
    """Manual entries: what the user did before the bank reports it. Payments with friends also go to Splitwise."""
    acct = _account(conn, account)
    cents = _amount(amount)
    today = db.today(conn).isoformat()
    if direction not in ("spent", "received", "transfer"):
        raise ToolError("direction must be 'spent', 'received' or 'transfer'.")

    def insert(account_id, signed, desc, merchant, cat, kind, who=None):
        return conn.execute(
            "INSERT INTO transactions (account_id, date, pending, amount_cents, description, merchant, category, "
            "category_source, kind, person, source) VALUES (?, ?, 1, ?, ?, ?, ?, 'user', ?, ?, 'manual')",
            (account_id, today, signed, desc, merchant, cat, kind, who)).lastrowid

    if direction == "transfer":
        if not to_account:
            raise ToolError("A transfer needs to_account (the account the money goes into).")
        dest = _account(conn, to_account)
        if dest == acct:
            raise ToolError("A transfer needs two different accounts.")
        a = insert(acct, -cents, f"Transfer to {dest}: {description}", f"Transfer to {dest}", "Transfer", "transfer")
        b = insert(dest, cents, f"Transfer from {acct}: {description}", f"Transfer from {acct}", "Transfer", "transfer")
        conn.execute("UPDATE transactions SET transfer_id = ? WHERE txn_id IN (?, ?)", (a, a, b))
        return {"recorded": f"Transfer of ${cents / 100:.2f} from {acct} to {dest}.", "balances": finance.balances(conn)}

    if person:  # paying or being paid back: record the money and settle up in Splitwise
        name, uid = _person(conn, person)
        me = int(conn.execute("SELECT external_id FROM connections WHERE provider = 'splitwise'").fetchone()[0])
        frm, to = (uid, me) if direction == "received" else (me, uid)
        txn = insert(acct, cents if direction == "received" else -cents,
                     f"Zelle payment {'from' if direction == 'received' else 'to'} {name}",
                     f"Zelle {'from' if direction == 'received' else 'to'} {name}", "Payments",
                     "p2p_in" if direction == "received" else "p2p_out", name)
        clients.splitwise(conn).create_expense(cost=f"{cents / 100:.2f}", description="Payment", payment=True,
                                               users=[{"user_id": frm, "paid_share": cents / 100, "owed_share": 0},
                                                      {"user_id": to, "paid_share": 0, "owed_share": cents / 100}])
        pipeline.sync_all(conn, only="splitwise")
        return {"recorded": f"${cents / 100:.2f} {'from' if direction == 'received' else 'to'} {name}, and the "
                            "settle-up was sent to Splitwise.", "transaction_id": txn,
                "balance_with_person_now": get_shared_balances(conn, name)["people"][0]}

    if direction == "received":
        txn = insert(acct, cents, description, description, "Income", "income")
        return {"recorded": f"${cents / 100:.2f} received in {acct}.", "transaction_id": txn}

    # A merchant the user has bought from before keeps its usual name and category, so "Blue Bottle"
    # lands in Coffee next to past "Blue Bottle Coffee" purchases instead of wherever the model guessed.
    key = description.strip().lower()
    known = conn.execute(
        """SELECT merchant, category FROM transactions WHERE kind = 'purchase' AND
           (lower(merchant) = ? OR lower(merchant) LIKE ? OR ? LIKE lower(merchant) || '%')
           ORDER BY lower(merchant) = ? DESC, date DESC LIMIT 1""", (key, key + "%", key, key)).fetchone() \
        if len(key) >= 4 else None
    rule = conn.execute("SELECT category FROM merchant_rules WHERE merchant = ?",
                        ((known[0] if known else description).lower(),)).fetchone()
    merchant = known[0] if known else description
    usual = rule[0] if rule else (known[1] if known else None)
    note, created = None, None
    if new_category:
        # The user agreed to (or asked for) a new category. Refuse near-duplicates of existing ones.
        existing = {c.lower(): c for c in db.categories(conn)}
        name = new_category.strip().title()
        if name.lower() in existing:
            cat = existing[name.lower()]
        else:
            close = difflib.get_close_matches(name.lower(), existing, n=1, cutoff=0.75)
            if close:
                raise ToolError(f"'{name}' is very close to the existing category '{existing[close[0]]}'. Use that "
                                f"one (category='{existing[close[0]]}'), or pick a clearly different name.")
            cat = created = db.ensure_category(conn, name, source="user")
        conn.execute("INSERT OR REPLACE INTO merchant_rules VALUES (?, ?, 'user')", (merchant.lower(), cat))
    elif usual:
        if category and _category(conn, category) != usual:
            note = (f"Filed under {usual}, like your earlier {merchant} purchases. If it should be "
                    f"{_category(conn, category)} from now on, use recategorize_merchant.")
        cat = usual
    elif category:
        try:
            cat = _category(conn, category)
        except ToolError:
            raise ToolError(f"'{category}' isn't one of the user's categories ({', '.join(db.categories(conn))}). "
                            f"If one of those fits, use it. If not, ask the user whether to create a new "
                            f"'{category.strip().title()}' category; if they agree, call again with "
                            f"new_category='{category.strip().title()}'.")
    else:
        # Unfamiliar merchant: don't guess "Other". Ask the model to choose; it knows what Zara is.
        raise ToolError(f"Penny hasn't seen '{description}' before, so it can't tell what kind of purchase this "
                        f"is. If one of the user's categories fits, call again with category set to it: "
                        f"{', '.join(db.categories(conn, 'flexible'))}. If none fits, don't force it: ask the user "
                        f"whether to create a new category (suggest a name), then call again with new_category.")
    txn = insert(acct, -cents, description, merchant, cat, "purchase")
    status = finance.budget_status(conn)
    c = next((x for x in status["categories"] if x["category"] == cat), None)
    return {"recorded": f"${cents / 100:.2f} at {merchant} from {acct}, category {cat}. It counts now and will be "
                        "matched to the bank's transaction when it syncs.", **({"note": note} if note else {}),
            **({"new_category_created": created,
                "budget_note": f"{created} has no monthly target yet, so this counts as unplanned spending and "
                               f"shrinks the room left in other categories. Offer to set a monthly amount for "
                               f"{created} with update_plan. Future purchases at {merchant} will go to {created}."}
               if created else {}),
            "transaction_id": txn, "category_now": c, "month_spent_so_far": status["spent_so_far"],
            "flexible_left_this_month": status["flexible_left"]}


def split_transaction(conn, transaction_id, people, my_share=None) -> dict:
    row = conn.execute("SELECT date, amount_cents, merchant, category, kind FROM transactions WHERE txn_id = ? "
                       "AND date <= ?", (transaction_id, db.today(conn).isoformat())).fetchone()
    if not row:
        raise ToolError(f"No transaction with id {transaction_id}. Find the id with query_transactions first.")
    d, amount, merchant, category, kind = row
    if kind != "purchase" or amount >= 0:
        raise ToolError(f"Transaction {transaction_id} ({merchant}) isn't a purchase, so it can't be split.")
    if conn.execute("SELECT 1 FROM shared_expenses WHERE txn_id = ?", (transaction_id,)).fetchone():
        raise ToolError(f"Transaction {transaction_id} ({merchant}) is already split.")
    friends = [_person(conn, p) for p in people or [] if p.strip().lower() not in ("me", "i", db.user_name(conn).lower())]
    if not friends:
        raise ToolError("Give at least one friend to split with, e.g. people=['Sam', 'Priya'].")

    cost = -amount
    if my_share is not None:
        mine = round(float(my_share) * 100)
        if not 0 <= mine <= cost:
            raise ToolError(f"my_share must be between 0 and the full charge (${cost / 100:.2f}).")
        base, extra = divmod(cost - mine, len(friends))
        shares = [mine] + [base + (1 if i < extra else 0) for i in range(len(friends))]
    else:
        base, extra = divmod(cost, len(friends) + 1)
        shares = [base + (1 if i < extra else 0) for i in range(len(friends) + 1)]
    me = int(conn.execute("SELECT external_id FROM connections WHERE provider = 'splitwise'").fetchone()[0])
    users = [{"user_id": me, "paid_share": cost / 100, "owed_share": shares[0] / 100}] + \
            [{"user_id": uid, "paid_share": 0, "owed_share": s / 100} for (_, uid), s in zip(friends, shares[1:])]
    try:
        resp = clients.splitwise(conn).create_expense(cost=f"{cost / 100:.2f}", description=merchant, users=users,
                                                      category_name={"Dining": "Dining out"}.get(category, "General"))
    except ProviderError as e:
        raise ToolError(f"Splitwise rejected the expense: {e}")
    pipeline.sync_all(conn, only="splitwise")
    ext = str(resp["expenses"][0]["id"])
    conn.execute("UPDATE shared_expenses SET txn_id = ?, link_source = 'user', category = ? WHERE external_id = ?",
                 (transaction_id, category, ext))
    return {"split": f"{merchant} (${cost / 100:.2f}) on {d}, added to Splitwise",
            "your_share": shares[0] / 100,
            "owed_to_you": {n: s / 100 for (n, _), s in zip(friends, shares[1:])},
            "budget_effect": f"{category} now counts ${shares[0] / 100:.2f} instead of ${cost / 100:.2f}."}


# --- What the model sees -----------------------------------------------------------

_DATE = {"type": "string", "description": "YYYY-MM-DD. Defaults to the first of the current month (start) or today (end)."}
_CATEGORY = {"type": "string", "description": "Spending category, e.g. 'Dining', 'Nightlife', 'Shopping', 'Groceries'."}
_MONTH = {"type": "string", "description": "YYYY-MM. Defaults to the current month."}


def _fn(name, description, properties=None, required=None):
    return {"type": "function", "function": {"name": name, "description": description, "parameters": {
        "type": "object", "properties": properties or {}, **({"required": required} if required else {})}}}


TOOLS = [
    _fn("get_financial_position",
        "Balance of every connected bank account (as last synced from the bank, minus pending charges and purchases "
        "the user reported), total cash, money others owe the user, money the user owes, net position, bills still "
        "due this month, and spendable cash (cash minus upcoming bills and debts; money owed to the user is NOT "
        "counted until it arrives)."),
    _fn("query_transactions",
        "Search transactions with filters. Returns rows with ids (needed for split_transaction), whether each is "
        "pending, where it came from, and how it was categorized.",
        {"start_date": _DATE, "end_date": _DATE,
         "account": {"type": "string", "description": "Account, e.g. 'chase' or 'boa'."},
         "category": _CATEGORY,
         "merchant_contains": {"type": "string", "description": "Text to match in the merchant or description, e.g. 'uber'."},
         "min_amount": {"type": "number", "description": "Minimum absolute amount in dollars."},
         "max_amount": {"type": "number", "description": "Maximum absolute amount in dollars."},
         "direction": {"type": "string", "enum": ["spent", "received", "any"]},
         "limit": {"type": "integer", "description": "Max rows (default 25, max 100)."}}),
    _fn("summarize_spending",
        "Total spending for a period grouped by category, merchant, week or day, counting only the user's share of "
        "split expenses and excluding transfers. Also counts small purchases under $15. Set "
        "compare_to_previous_period to compare with the same dates last month.",
        {"start_date": _DATE, "end_date": _DATE,
         "group_by": {"type": "string", "enum": ["category", "merchant", "week", "day"]},
         "category": {"type": "string", "description": "Only include this category (optional)."},
         "compare_to_previous_period": {"type": "boolean"}}),
    _fn("get_budget_status",
        "This month's plan versus reality: the cap, spending so far, every bill (with whether the user told Penny "
        "about it or Penny detected it, and whether it's paid), and for each flexible category its target, spending, "
        "how far over it is, and its room left after re-balancing (overspending in one category shrinks the others)."),
    _fn("recommend_spending",
        "Recommend how much to spend in one category today, this weekend, or for the rest of the month, from the "
        "re-balanced budget and how often the user usually spends in it. Returns the amount, what it would be if "
        "everything were on plan, the typical spend per occasion, reasons, and what changed since the last "
        "recommendation for this category (use it to answer 'why did you lower it?').",
        {"category": _CATEGORY, "horizon": {"type": "string", "enum": ["today", "weekend", "rest_of_month"]}},
        ["category"]),
    _fn("evaluate_purchase",
        "What-if check for a planned purchase without recording it: fits the category, needs cuts elsewhere, or "
        "pushes the month over the cap, and which categories get squeezed.",
        {"amount": {"type": "number", "description": "Planned amount in dollars."}, "category": _CATEGORY},
        ["amount", "category"]),
    _fn("get_shared_balances",
        "Who owes the user and whom the user owes, from Splitwise, with how long each balance has been open, "
        "cross-checked against bank deposits. 'mismatches' lists payments marked paid with no deposit, and deposits "
        "not recorded in Splitwise.",
        {"person": {"type": "string", "description": "Only this person (optional)."}}),
    _fn("get_recurring_payments",
        "Subscriptions and bills Penny DETECTED in the transactions (nobody entered them): amount, cadence, next "
        "expected date, price changes, ones that stopped, and whether each is in the user's plan.",
        {"include_income": {"type": "boolean", "description": "Also list recurring income like paychecks."}}),
    _fn("draft_monthly_plan",
        "Start a planning conversation for a month: the current cap, bills the user told Penny about, detected "
        "recurring payments not in the plan, price changes, stopped subscriptions, and suggested category targets "
        "with last month's spending. Saves nothing; discuss it, then call update_plan.",
        {"month": _MONTH}),
    _fn("update_plan",
        "Change a month's plan: the cap, bills (add, change, or confirm a detected payment by its name), remove "
        "bills, dismiss a detected payment that isn't a real bill, set category targets (optionally creating a new "
        "category), or remove categories (optionally moving their spending into another category).",
        {"month": _MONTH,
         "monthly_cap": {"type": "number", "description": "Total monthly cap in dollars, bills included."},
         "bills_to_set": {"type": "array", "description": "Bills to add or change. To confirm a detected payment, "
                                                          "pass just its name, e.g. {'name': 'Hulu'}.",
                          "items": {"type": "object", "properties": {
                              "name": {"type": "string"}, "amount": {"type": "number", "description": "Omit or null if it varies."},
                              "due_day_start": {"type": "integer"}, "due_day_end": {"type": "integer"},
                              "category": {"type": "string"}}, "required": ["name"]}},
         "bills_to_remove": {"type": "array", "items": {"type": "string"}, "description": "Bill names to remove."},
         "detected_to_dismiss": {"type": "array", "items": {"type": "string"},
                                 "description": "Detected payments that shouldn't count as bills."},
         "category_targets": {"type": "array", "items": {"type": "object", "properties": {
             "category": {"type": "string"}, "amount": {"type": "number"},
             "create_category": {"type": "boolean", "description": "True to create a new category with this name."}},
             "required": ["category", "amount"]}},
         "categories_to_remove": {"type": "array", "items": {"type": "object", "properties": {
             "category": {"type": "string"},
             "move_spending_to": {"type": "string", "description": "Category that absorbs its merchants (optional)."}},
             "required": ["category"]}}}),
    _fn("recategorize_merchant",
        "Move a merchant's transactions to another category and remember it as the user's rule for the future, "
        "e.g. 'Sephora is Personal Care'. Can create a new category.",
        {"merchant": {"type": "string"}, "category": _CATEGORY,
         "create_category": {"type": "boolean", "description": "True to create the category if it doesn't exist."}},
        ["merchant", "category"]),
    _fn("update_memory",
        "Remember something the user wants Penny to keep in mind (a goal, a preference, a constraint), or forget a "
        "note. Notes are shown to Penny in every conversation turn.",
        {"add_note": {"type": "string"}, "remove_note_id": {"type": "integer"}}),
    _fn("add_transaction",
        "Record money the user just spent, received, or moved, before their bank reports it. Set person when paying "
        "or being paid back by a friend: that also records the settle-up in Splitwise.",
        {"account": {"type": "string", "description": "'chase' (everyday spending) or 'boa' (salary)."},
         "amount": {"type": "number", "description": "Positive amount in dollars."},
         "direction": {"type": "string", "enum": ["spent", "received", "transfer"]},
         "description": {"type": "string", "description": "Merchant or short note, e.g. 'Zara'."},
         "category": {"type": "string", "description": "An existing category for the purchase. Required for a merchant the user hasn't bought from before; for a familiar merchant it can be omitted."},
         "new_category": {"type": "string", "description": "Create this category and file the purchase (and future ones at this merchant) under it. Only after the user agreed to the new category or named it themselves."},
         "person": {"type": "string", "description": "The friend paid or paying back, if any."},
         "to_account": {"type": "string", "description": "Destination account for transfers."}},
        ["account", "amount", "direction", "description"]),
    _fn("split_transaction",
        "Split an existing purchase with friends by creating the expense in Splitwise. Each friend then owes the "
        "user their share and the budget counts only the user's share. Equal split unless my_share is given.",
        {"transaction_id": {"type": "integer", "description": "Id from query_transactions or add_transaction."},
         "people": {"type": "array", "items": {"type": "string"}, "description": "Friends, e.g. ['Sam', 'Priya']."},
         "my_share": {"type": "number", "description": "The user's own share in dollars (optional)."}},
        ["transaction_id", "people"]),
]

TOOL_MAP = {t["function"]["name"]: globals()[t["function"]["name"]] for t in TOOLS}


def run_tool(conn, name: str, args: dict) -> str:
    """Run one tool call. Models invent tool names and arguments; never let that crash the loop."""
    if name not in TOOL_MAP:
        return json.dumps({"error": f"Unknown tool '{name}'. Available: {list(TOOL_MAP)}"})
    try:
        result = TOOL_MAP[name](conn, **args)
        conn.commit()
        return json.dumps(result, default=str)
    except ToolError as e:
        conn.rollback()
        return json.dumps({"error": str(e)})
    except TypeError as e:
        conn.rollback()
        return json.dumps({"error": f"Bad arguments for {name}: {e}"})
    except Exception as e:  # a bug or bad value we didn't anticipate: tell the model, don't crash the chat
        conn.rollback()
        return json.dumps({"error": f"{name} failed: {type(e).__name__}: {e}. Check the arguments and try again."})
