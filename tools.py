"""The tools the harness can run, and the JSON that describes them to the model.

Every tool takes the session's database connection first, then the model's
arguments. Tools validate their inputs and return errors as JSON the model can
act on (what was wrong and what the valid options are) instead of raising.
"""

import difflib
import json
from datetime import date, timedelta

import db
import finance
from finance import dollars


class ToolError(Exception):
    """Raised inside a tool; run_tool turns it into {"error": ...} for the model."""


# --- Argument helpers --------------------------------------------------------------


def _category(name: str | None, allow_none: bool = False) -> str | None:
    if name is None and allow_none:
        return None
    lookup = {c.lower(): c for c in db.SPENDING_CATEGORIES}
    key = (name or "").strip().lower()
    if key in lookup:
        return lookup[key]
    close = difflib.get_close_matches(key, lookup, n=3, cutoff=0.5)
    hint = f" Did you mean: {', '.join(lookup[c] for c in close)}?" if close else ""
    raise ToolError(f"Unknown category '{name}'.{hint} Valid categories: {', '.join(db.SPENDING_CATEGORIES)}.")


def _account(conn, name: str) -> str:
    accounts = [r[0] for r in conn.execute("SELECT account_id FROM accounts")]
    key = (name or "").strip().lower()
    for a in accounts:
        if key == a or key in a or a in key.replace("bank of america", "boa"):
            return a
    raise ToolError(f"Unknown account '{name}'. Available accounts: {', '.join(accounts)}.")


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


def _amount(value) -> int:
    try:
        c = round(float(value) * 100)
    except (TypeError, ValueError):
        raise ToolError(f"Amount '{value}' isn't a number. Pass dollars as a number, e.g. 30 or 12.50.")
    if c <= 0:
        raise ToolError("Amount must be a positive number of dollars; use 'direction' to say which way it moved.")
    return c


def _people(conn) -> list[str]:
    rows = conn.execute("SELECT person FROM shared_shares UNION SELECT paid_by FROM shared_expenses "
                        "UNION SELECT person FROM transactions WHERE person IS NOT NULL").fetchall()
    return sorted({r[0] for r in rows} - {"me"})


# --- Read tools --------------------------------------------------------------------


def get_financial_position(conn) -> dict:
    return finance.financial_position(conn)


def query_transactions(conn, start_date=None, end_date=None, account=None, category=None,
                       merchant_contains=None, min_amount=None, max_amount=None, direction="any", limit=25) -> dict:
    today = db.today(conn)
    start = _date(start_date, today.replace(day=1), conn)
    end = _date(end_date, today, conn)
    sql = ["SELECT txn_id, date, account_id, merchant, category, amount_cents, kind, person FROM transactions "
           "WHERE date BETWEEN ? AND ?"]
    params: list = [start.isoformat(), end.isoformat()]
    if account:
        sql.append("AND account_id = ?"); params.append(_account(conn, account))
    if category:
        sql.append("AND category = ?"); params.append(_category(category))
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
        "count": len(rows),
        "total": dollars(sum(r[5] for r in rows)),
        "showing": min(limit, len(rows)),
        "transactions": [{"id": r[0], "date": r[1], "account": r[2], "merchant": r[3], "category": r[4],
                          "amount": dollars(r[5]), "type": r[6], **({"person": r[7]} if r[7] else {})}
                         for r in rows[:limit]],
        "note": "Amounts are signed: negative is money out. Split purchases show the full charge here; "
                "use summarize_spending for your share.",
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
    cat = _category(category, allow_none=True)

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
              "note": "Your share only: split expenses count at your share, transfers and settle-ups are excluded."}
    if compare_to_previous_period:
        # Same calendar span one month earlier (Sep 1-18 -> Aug 1-18), clipped to month length.
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
    cat = _category(category)
    if cat in db.FIXED_CATEGORIES:
        raise ToolError(f"{cat} is a fixed bill, not a flexible category. Use get_budget_status to see bills.")
    return finance.recommend(conn, cat, horizon)


def evaluate_purchase(conn, amount, category) -> dict:
    return finance.evaluate_purchase(conn, _amount(amount), _category(category))


def get_shared_balances(conn, person=None) -> dict:
    result = finance.shared_balances(conn)
    if person:
        name = person.strip().title()
        known = _people(conn)
        if name not in known:
            raise ToolError(f"No one named '{person}' in your shared expenses. People: {', '.join(known)}.")
        result["people"] = [p for p in result["people"] if p["person"] == name] or \
                           [{"person": name, "balance": 0.0, "direction": "settled up"}]
        result["mismatches"] = [m for m in result["mismatches"] if m["person"] == name]
    return result


# --- Write tools -------------------------------------------------------------------


def add_transaction(conn, account, amount, direction, description, category=None, person=None, to_account=None) -> dict:
    acct = _account(conn, account)
    cents = _amount(amount)
    today = db.today(conn).isoformat()
    if direction not in ("spent", "received", "transfer"):
        raise ToolError("direction must be 'spent', 'received' or 'transfer'.")

    if direction == "transfer":
        if not to_account:
            raise ToolError("A transfer needs to_account (the account the money goes into).")
        dest = _account(conn, to_account)
        if dest == acct:
            raise ToolError("A transfer needs two different accounts.")
        out_id = db.add_bank_txn(conn, acct, today, -cents, f"Online Transfer to {dest} ({description})", "user")
        in_id = db.add_bank_txn(conn, dest, today, cents, f"Online Transfer from {acct} ({description})", "user")
        conn.execute("UPDATE transactions SET transfer_id = ? WHERE txn_id IN (?, ?)", (out_id, out_id, in_id))
        return {"recorded": f"Transfer of ${dollars(cents):.2f} from {acct} to {dest} on {today}.",
                "transaction_ids": [out_id, in_id], "balances": finance.balances(conn, db.today(conn))}

    if person:  # money to or from a person settles a shared balance
        name = person.strip().title()
        if direction == "received":
            desc, frm, to, signed = f"Zelle Payment From {name} ({description})", name, "me", cents
        else:
            desc, frm, to, signed = f"Zelle Payment To {name} ({description})", "me", name, -cents
        txn_id = db.add_bank_txn(conn, acct, today, signed, desc, "user")
        conn.execute("UPDATE transactions SET person = ? WHERE txn_id = ?", (name, txn_id))
        conn.execute("INSERT INTO settlements (date, from_person, to_person, amount_cents, txn_id) VALUES (?, ?, ?, ?, ?)",
                     (today, frm, to, cents, txn_id))
        balance = get_shared_balances(conn, name)["people"][0]
        return {"recorded": f"${dollars(cents):.2f} {'from' if signed > 0 else 'to'} {name} on {acct}, logged as a "
                            "shared-expense payment.", "transaction_id": txn_id, "balance_with_person_now": balance}

    if direction == "received":
        txn_id = db.add_bank_txn(conn, acct, today, cents, description, "user")
        conn.execute("UPDATE transactions SET category = 'Income', kind = 'income' WHERE txn_id = ?", (txn_id,))
        return {"recorded": f"${dollars(cents):.2f} received in {acct} on {today}.", "transaction_id": txn_id}

    txn_id = db.add_bank_txn(conn, acct, today, -cents, description, "user")
    cat = _category(category) if category else db.categorize(description)[0]
    conn.execute("UPDATE transactions SET category = ?, kind = 'purchase' WHERE txn_id = ?", (cat, txn_id))
    status = finance.budget_status(conn)
    c = next((x for x in status["categories"] if x["category"] == cat), None)
    return {"recorded": f"${dollars(cents):.2f} spent at {description} from {acct} on {today}, category {cat}.",
            "transaction_id": txn_id,
            "category_now": c, "month_spent_so_far": status["spent_so_far"],
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
    names = sorted({p.strip().title() for p in (people or []) if p.strip() and p.strip().lower() not in ("me", "maya")})
    if not names:
        raise ToolError("Give at least one other person to split with, e.g. people=['Sam', 'Priya'].")

    cost = -amount
    if my_share is not None:
        mine = round(float(my_share) * 100)
        if not 0 <= mine <= cost:
            raise ToolError(f"my_share must be between 0 and the full charge (${dollars(cost):.2f}).")
        others = cost - mine
        base, extra = divmod(others, len(names))
        shares = {"me": mine} | {n: base + (1 if i < extra else 0) for i, n in enumerate(names)}
    else:
        base, extra = divmod(cost, len(names) + 1)
        shares = {p: base + (1 if i < extra else 0) for i, p in enumerate(["me"] + names)}

    cur = conn.execute("INSERT INTO shared_expenses (date, description, category, cost_cents, paid_by, txn_id) "
                       "VALUES (?, ?, ?, ?, 'me', ?)", (d, merchant, category, cost, transaction_id))
    conn.executemany("INSERT INTO shared_shares VALUES (?, ?, ?)", [(cur.lastrowid, p, c) for p, c in shares.items()])
    return {"split": f"{merchant} (${dollars(cost):.2f}) on {d}",
            "your_share": dollars(shares["me"]),
            "owed_to_you": {n: dollars(shares[n]) for n in names},
            "budget_effect": f"{category} now counts ${dollars(shares['me']):.2f} instead of ${dollars(cost):.2f}."}


def update_budget(conn, monthly_cap=None, category_targets=None) -> dict:
    if monthly_cap is None and not category_targets:
        raise ToolError("Pass monthly_cap, category_targets, or both.")
    if monthly_cap is not None:
        conn.execute("UPDATE budget SET value = ? WHERE key = 'monthly_cap_cents'", (_amount(monthly_cap),))
    for t in category_targets or []:
        if not isinstance(t, dict) or "category" not in t or "amount" not in t:
            raise ToolError("Each category target needs 'category' and 'amount', e.g. {'category': 'Dining', 'amount': 80}.")
        cat = _category(t["category"])
        if cat in db.FIXED_CATEGORIES:
            raise ToolError(f"{cat} is covered by fixed bills, not a flexible target.")
        cents = round(float(t["amount"]) * 100)
        if cents < 0:
            raise ToolError("Targets can't be negative.")
        conn.execute("INSERT OR REPLACE INTO budget_categories VALUES (?, ?)", (cat, cents))
    cap = conn.execute("SELECT value FROM budget WHERE key = 'monthly_cap_cents'").fetchone()[0]
    targets = dict(conn.execute("SELECT category, target_cents FROM budget_categories"))
    bills = conn.execute("SELECT SUM(amount_cents) FROM obligations").fetchone()[0]
    gap = cap - bills - sum(targets.values())
    return {"monthly_cap": dollars(cap), "planned_bills": dollars(bills),
            "category_targets": {c: dollars(v) for c, v in sorted(targets.items())},
            "unallocated": dollars(gap),
            "note": "Targets plus bills exceed the cap; the budget will scale remaining room down to fit." if gap < 0
                    else "Targets plus bills fit within the cap.",
            "flexible_left_this_month": finance.budget_status(conn)["flexible_left"]}


# --- What the model sees -----------------------------------------------------------

_DATE = {"type": "string", "description": "YYYY-MM-DD. Defaults to the first of the current month (start) or today (end)."}
_CATEGORY = {"type": "string", "description": "Spending category, e.g. 'Dining', 'Nightlife', 'Shopping', 'Groceries'."}

TOOLS = [
    {"type": "function", "function": {
        "name": "get_financial_position",
        "description": "Current balance of every bank account, total cash, money others owe the user, money the user "
                       "owes, net position, bills still due this month, and spendable cash (cash minus upcoming bills "
                       "and debts; money owed to the user is NOT counted until it arrives). Use for 'how much do I "
                       "have' or 'what can I actually spend' questions.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "query_transactions",
        "description": "Search raw bank transactions with filters. Returns matching rows with ids (needed for "
                       "split_transaction), a count and a total. Use to find specific purchases or deposits.",
        "parameters": {"type": "object", "properties": {
            "start_date": _DATE, "end_date": _DATE,
            "account": {"type": "string", "description": "Account id, e.g. 'chase' or 'boa'."},
            "category": _CATEGORY,
            "merchant_contains": {"type": "string", "description": "Text to match in the merchant or description, e.g. 'uber'."},
            "min_amount": {"type": "number", "description": "Minimum absolute amount in dollars."},
            "max_amount": {"type": "number", "description": "Maximum absolute amount in dollars."},
            "direction": {"type": "string", "enum": ["spent", "received", "any"], "description": "Money out, in, or both."},
            "limit": {"type": "integer", "description": "Max rows to return (default 25, max 100)."}}}}},
    {"type": "function", "function": {
        "name": "summarize_spending",
        "description": "Total spending for a period grouped by category, merchant, week or day, counting only the "
                       "user's share of split expenses and excluding transfers. Also counts small purchases under $15. "
                       "Set compare_to_previous_period to compare with the same dates last month.",
        "parameters": {"type": "object", "properties": {
            "start_date": _DATE, "end_date": _DATE,
            "group_by": {"type": "string", "enum": ["category", "merchant", "week", "day"]},
            "category": {"type": "string", "description": "Only include this category (optional)."},
            "compare_to_previous_period": {"type": "boolean", "description": "Also summarize the same dates one month earlier."}}}}},
    {"type": "function", "function": {
        "name": "get_budget_status",
        "description": "This month's budget: the monthly cap, money spent so far, bills paid and still due, and for "
                       "each flexible category its target, spending, how far over it is, and its room left after "
                       "re-balancing (overspending in one category shrinks the room in all others). Use for 'where am "
                       "I overspending', 'how much is left this month' and 'what bills are left'.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "recommend_spending",
        "description": "Recommend how much the user should spend in one category today, this weekend, or for the rest "
                       "of the month, given the re-balanced budget and how often they usually spend in that category. "
                       "Returns the amount, what it would be if everything were on plan, their typical spend per "
                       "occasion, step-by-step reasons, and what changed since the last recommendation for the same "
                       "category (use that to answer 'why did you lower it?').",
        "parameters": {"type": "object", "properties": {
            "category": _CATEGORY,
            "horizon": {"type": "string", "enum": ["today", "weekend", "rest_of_month"]}},
            "required": ["category"]}}},
    {"type": "function", "function": {
        "name": "evaluate_purchase",
        "description": "What-if check for a planned purchase without recording it: whether it fits the category, "
                       "needs cuts elsewhere, or pushes the month over the cap, and which categories get squeezed. Use "
                       "for 'can I afford X?'.",
        "parameters": {"type": "object", "properties": {
            "amount": {"type": "number", "description": "Planned amount in dollars."},
            "category": _CATEGORY},
            "required": ["amount", "category"]}}},
    {"type": "function", "function": {
        "name": "get_shared_balances",
        "description": "Who owes the user and whom the user owes from shared expenses (Splitwise), with how long each "
                       "balance has been open, cross-checked against bank deposits. 'mismatches' lists payments marked "
                       "paid with no deposit, and deposits not recorded as payments.",
        "parameters": {"type": "object", "properties": {
            "person": {"type": "string", "description": "Only this person (optional)."}}}}},
    {"type": "function", "function": {
        "name": "update_budget",
        "description": "Change the monthly spending cap and/or the monthly target for flexible categories. Returns the "
                       "new plan and whether targets plus bills still fit under the cap.",
        "parameters": {"type": "object", "properties": {
            "monthly_cap": {"type": "number", "description": "New total monthly cap in dollars, bills included."},
            "category_targets": {"type": "array", "description": "New targets, e.g. [{'category': 'Dining', 'amount': 80}].",
                                 "items": {"type": "object", "properties": {
                                     "category": {"type": "string"}, "amount": {"type": "number"}},
                                     "required": ["category", "amount"]}}}}}},
    {"type": "function", "function": {
        "name": "add_transaction",
        "description": "Record new money movement dated today: a purchase ('spent'), money in ('received'), or a "
                       "transfer between the user's own accounts ('transfer'). Set person when paying or being paid back "
                       "by someone, which also settles shared balances. Call this when the user says they spent, "
                       "received or moved money.",
        "parameters": {"type": "object", "properties": {
            "account": {"type": "string", "description": "Account id: 'chase' (everyday spending) or 'boa' (salary)."},
            "amount": {"type": "number", "description": "Positive amount in dollars."},
            "direction": {"type": "string", "enum": ["spent", "received", "transfer"]},
            "description": {"type": "string", "description": "Merchant or a short note, e.g. 'Zara' or 'dinner at Tomo'."},
            "category": {"type": "string", "description": "Category for purchases; guessed from the description if omitted."},
            "person": {"type": "string", "description": "The friend paid or paying back, if any."},
            "to_account": {"type": "string", "description": "Destination account for transfers."}},
            "required": ["account", "amount", "direction", "description"]}}},
    {"type": "function", "function": {
        "name": "split_transaction",
        "description": "Split an existing purchase with other people, like adding it to Splitwise. Creates what each "
                       "person owes the user, and the budget then counts only the user's share. Equal split unless "
                       "my_share is given.",
        "parameters": {"type": "object", "properties": {
            "transaction_id": {"type": "integer", "description": "Id from query_transactions or add_transaction."},
            "people": {"type": "array", "items": {"type": "string"}, "description": "Other people, e.g. ['Sam', 'Priya']."},
            "my_share": {"type": "number", "description": "The user's own share in dollars (optional)."}},
            "required": ["transaction_id", "people"]}}},
]

TOOL_MAP = {
    "get_financial_position": get_financial_position,
    "query_transactions": query_transactions,
    "summarize_spending": summarize_spending,
    "get_budget_status": get_budget_status,
    "recommend_spending": recommend_spending,
    "evaluate_purchase": evaluate_purchase,
    "get_shared_balances": get_shared_balances,
    "update_budget": update_budget,
    "add_transaction": add_transaction,
    "split_transaction": split_transaction,
}


def run_tool(conn, name: str, args: dict) -> str:
    """Run one tool call. Models invent tool names and arguments; never let that crash the loop."""
    if name not in TOOL_MAP:
        return json.dumps({"error": f"Unknown tool '{name}'. Available: {list(TOOL_MAP)}"})
    try:
        result = TOOL_MAP[name](conn, **args)
        conn.commit()
        return json.dumps(result)
    except ToolError as e:
        conn.rollback()
        return json.dumps({"error": str(e)})
    except TypeError as e:
        conn.rollback()
        return json.dumps({"error": f"Bad arguments for {name}: {e}"})
    except Exception as e:  # a bug or bad value we didn't anticipate: tell the model, don't crash the chat
        conn.rollback()
        return json.dumps({"error": f"{name} failed: {type(e).__name__}: {e}. Check the arguments and try again."})
