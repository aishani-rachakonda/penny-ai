"""The finance engine: every number Penny reports is computed here.

These are plain functions over a session's database. Nothing here calls the
model, and nothing derived is stored except the recommendation log (which is a
record of what Penny said, not a cached number).

Every function only sees data dated on or before the session's simulated today.
"""

import calendar
import json
import statistics
from collections import defaultdict
from datetime import date, timedelta

from db import FIXED_CATEGORIES, SPENDING_CATEGORIES, today

def dollars(c: int | float) -> float:
    return round(c / 100, 2)


def month_bounds(d: date) -> tuple[date, date]:
    return d.replace(day=1), d.replace(day=calendar.monthrange(d.year, d.month)[1])


def prev_month_start(d: date) -> date:
    return (d.replace(day=1) - timedelta(days=1)).replace(day=1)


# --- Spending: what the user actually paid for, net of splits -------------------


def spending_items(conn, start: date, end: date) -> list[dict]:
    """Every piece of the user's own spending between two dates (inclusive).

    - A bank purchase counts in full, unless it was split: then only the user's share counts.
    - A shared expense someone else paid counts at the user's share (it's money they owe).
    - Transfers between own accounts, income and Zelle settle-ups are never spending.
    """
    s, e = start.isoformat(), end.isoformat()
    items = []
    for txn_id, d, amount, merchant, category, expense_id, share, sh_category in conn.execute(
        """SELECT t.txn_id, t.date, t.amount_cents, t.merchant, t.category, x.expense_id, s.owed_cents, x.category
           FROM transactions t
           LEFT JOIN shared_expenses x ON x.txn_id = t.txn_id
           LEFT JOIN shared_shares s ON s.expense_id = x.expense_id AND s.person = 'me'
           WHERE t.kind = 'purchase' AND t.date BETWEEN ? AND ?""", (s, e)):
        if expense_id is None:
            items.append({"date": d, "category": category, "cents": -amount, "merchant": merchant, "txn_id": txn_id})
        else:
            items.append({"date": d, "category": sh_category, "cents": share or 0, "merchant": merchant,
                          "txn_id": txn_id, "split": True, "full_cents": -amount})
    for expense_id, d, desc, category, paid_by, share in conn.execute(
        """SELECT x.expense_id, x.date, x.description, x.category, x.paid_by, s.owed_cents
           FROM shared_expenses x JOIN shared_shares s ON s.expense_id = x.expense_id AND s.person = 'me'
           WHERE x.paid_by != 'me' AND x.date BETWEEN ? AND ?""", (s, e)):
        items.append({"date": d, "category": category, "cents": share, "merchant": f"{desc} (paid by {paid_by})",
                      "expense_id": expense_id})
    return items


def spending_by_category(conn, start: date, end: date) -> dict[str, int]:
    totals = defaultdict(int)
    for it in spending_items(conn, start, end):
        totals[it["category"]] += it["cents"]
    return dict(totals)


def historical_category_averages(conn, as_of: date, months: int = 6) -> dict[str, int]:
    """Average monthly spend per category over the full months before as_of's month.

    The mean, not the median: lumpy categories (shopping, haircuts) are zero in many
    months, and a median would budget nothing for them.
    """
    per_month = []
    m = as_of.replace(day=1)
    for _ in range(months):
        m = prev_month_start(m)
        start, end = month_bounds(m)
        if conn.execute("SELECT 1 FROM accounts WHERE opening_date <= ?", (start.isoformat(),)).fetchone():
            per_month.append(spending_by_category(conn, start, end))
    cats = {c for month in per_month for c in month}
    return {c: round(statistics.mean(month.get(c, 0) for month in per_month)) for c in cats}


def spending_pattern(conn, category: str, as_of: date, months: int = 6) -> dict:
    """How the user spends in a category, from the months before this one.

    Returns, for each weekday, the chance they spend in the category on that day,
    plus what one occasion typically costs (median of days with any spend).
    """
    start = as_of.replace(day=1)
    for _ in range(months):
        start = prev_month_start(start)
    end = as_of.replace(day=1) - timedelta(days=1)
    per_day = defaultdict(int)
    for it in spending_items(conn, start, end):
        if it["category"] == category and it["cents"] > 0:
            per_day[it["date"]] += it["cents"]
    n_days, n_spend = [0] * 7, [0] * 7
    d = start
    while d <= end:
        n_days[d.weekday()] += 1
        n_spend[d.weekday()] += d.isoformat() in per_day
        d += timedelta(days=1)
    if not per_day:  # no history: assume an even, occasional habit
        return {"chance_by_weekday": [0.3] * 7, "typical_per_occasion": 0.0}
    return {"chance_by_weekday": [n_spend[i] / n_days[i] if n_days[i] else 0 for i in range(7)],
            "typical_per_occasion": dollars(statistics.median(per_day.values()))}


# --- Shared balances: Splitwise cross-checked against the bank ------------------


def shared_balances(conn, as_of: date | None = None) -> dict:
    """Who owes whom, per person, and where Splitwise and the bank disagree.

    Positive balance = that person owes the user.
    """
    as_of = as_of or today(conn)
    d = as_of.isoformat()
    events = defaultdict(list)  # person -> [(date, delta_cents, description)]

    for expense_id, xd, desc, paid_by in conn.execute(
            "SELECT expense_id, date, description, paid_by FROM shared_expenses WHERE date <= ?", (d,)):
        shares = dict(conn.execute("SELECT person, owed_cents FROM shared_shares WHERE expense_id = ?", (expense_id,)))
        if paid_by == "me":
            for person, owed in shares.items():
                if person != "me":
                    events[person].append((xd, owed, f"{desc}: their share"))
        elif "me" in shares:
            events[paid_by].append((xd, -shares["me"], f"{desc}: your share"))

    flags = []
    for sid, sd, src, dst, amount, txn_id in conn.execute(
            "SELECT settlement_id, date, from_person, to_person, amount_cents, txn_id FROM settlements WHERE date <= ?",
            (d,)):
        if dst == "me":
            events[src].append((sd, -amount, "paid you"))
            if txn_id is None:
                flags.append({"person": src, "type": "recorded_but_not_received", "amount": dollars(amount),
                              "date": sd, "detail": f"Splitwise says {src} paid you ${dollars(amount):.2f} on {sd}, "
                                                    "but no matching deposit is in your bank accounts."})
        else:
            events[dst].append((sd, amount, "you paid them"))

    for txn_id, td, amount, person in conn.execute(
            """SELECT txn_id, date, amount_cents, person FROM transactions
               WHERE kind = 'p2p_in' AND date <= ?
               AND txn_id NOT IN (SELECT txn_id FROM settlements WHERE txn_id IS NOT NULL)""", (d,)):
        flags.append({"person": person, "type": "received_but_not_recorded", "amount": dollars(amount), "date": td,
                      "txn_id": txn_id, "detail": f"{person} sent you ${dollars(amount):.2f} on {td}, but it isn't "
                                                  "recorded as a payment in Splitwise, so your balance with them "
                                                  "may be out of date."})

    people = []
    for person, evs in events.items():
        evs.sort()
        balance, since, items_since = 0, None, []
        for ed, delta, desc in evs:
            was_zero = abs(balance) < 1
            balance += delta
            if abs(balance) < 1:
                since, items_since = None, []
            else:
                if was_zero:
                    since, items_since = ed, []
                items_since.append({"date": ed, "amount": dollars(delta), "description": desc})
        if abs(balance) >= 1:
            people.append({
                "person": person,
                "balance": dollars(balance),
                "direction": "owes you" if balance > 0 else "you owe",
                "open_since": since,
                "days_open": (as_of - date.fromisoformat(since)).days if since else 0,
                "items": items_since[-6:],
            })
    people.sort(key=lambda p: -p["balance"])
    owed = sum(p["balance"] for p in people if p["balance"] > 0)
    owe = -sum(p["balance"] for p in people if p["balance"] < 0)
    return {"as_of": d, "people": people, "total_owed_to_you": round(owed, 2), "total_you_owe": round(owe, 2),
            "net": round(owed - owe, 2), "mismatches": flags}


# --- Obligations and the monthly budget ------------------------------------------


def estimate_shared_obligation(conn, match: str, as_of: date, months: int = 3) -> int:
    """Average of the user's share of a recurring shared bill over recent months."""
    start = as_of.replace(day=1)
    for _ in range(months):
        start = prev_month_start(start)
    rows = conn.execute(
        """SELECT s.owed_cents FROM shared_expenses x JOIN shared_shares s ON s.expense_id = x.expense_id
           WHERE s.person = 'me' AND x.description LIKE ? AND x.date >= ? AND x.date < ?""",
        (f"%{match}%", start.isoformat(), as_of.replace(day=1).isoformat())).fetchall()
    return round(sum(r[0] for r in rows) / len(rows)) if rows else 0


def obligations_status(conn, as_of: date) -> list[dict]:
    start, _ = month_bounds(as_of)
    s, d = start.isoformat(), as_of.isoformat()
    out = []
    for name, category, expected, variable, source, match, due_day in conn.execute(
            "SELECT name, category, amount_cents, variable, source, match, due_day FROM obligations"):
        if source == "bank":
            row = conn.execute("SELECT SUM(-amount_cents), MAX(date) FROM transactions WHERE kind = 'purchase' "
                               "AND description LIKE ? AND date BETWEEN ? AND ?", (f"%{match}%", s, d)).fetchone()
        else:
            row = conn.execute(
                """SELECT SUM(s.owed_cents), MAX(x.date) FROM shared_expenses x
                   JOIN shared_shares s ON s.expense_id = x.expense_id AND s.person = 'me'
                   WHERE x.description LIKE ? AND x.date BETWEEN ? AND ?""", (f"%{match}%", s, d)).fetchone()
        paid = row[0] is not None
        out.append({"name": name, "category": category, "status": "paid" if paid else "upcoming",
                    "amount": dollars(row[0] if paid else expected), "expected": dollars(expected),
                    "estimated": bool(variable) and not paid, "due_day": due_day,
                    "paid_on": row[1] if paid else None})
    return out


def budget_status(conn, as_of: date | None = None, extra: dict[str, int] | None = None) -> dict:
    """The month's plan versus reality, with flexible budgets re-balanced.

    How re-balancing works: the cap minus fixed costs (bills paid so far plus bills
    still expected) is the flexible pool. Whatever is left of that pool is shared
    out across categories in proportion to each one's remaining room. So when one
    category goes over its target, or a bill comes in higher than estimated, every
    other category's remaining room shrinks by the same percentage.

    `extra` adds hypothetical spending ({category: cents}) for what-if questions.
    """
    as_of = as_of or today(conn)
    start, end = month_bounds(as_of)
    cap = conn.execute("SELECT value FROM budget WHERE key = 'monthly_cap_cents'").fetchone()[0]
    targets = dict(conn.execute("SELECT category, target_cents FROM budget_categories"))
    spent = spending_by_category(conn, start, as_of)
    for c, v in (extra or {}).items():
        spent[c] = spent.get(c, 0) + v

    obligations = obligations_status(conn, as_of)
    obligations_planned = sum(round(o["expected"] * 100) for o in obligations)
    upcoming = sum(round(o["amount"] * 100) for o in obligations if o["status"] == "upcoming")
    fixed_spent = sum(v for c, v in spent.items() if c in FIXED_CATEGORIES)
    fixed_committed = fixed_spent + upcoming

    flex_cats = sorted(set(targets) | {c for c in spent if c not in FIXED_CATEGORIES and spent[c]})
    pool = cap - fixed_committed
    flex_spent = sum(spent.get(c, 0) for c in flex_cats)
    available = max(0, pool - flex_spent)
    room = {c: max(0, targets.get(c, 0) - spent.get(c, 0)) for c in flex_cats}
    total_room = sum(room.values())
    factor = min(1.0, available / total_room) if total_room else 0.0

    categories = []
    for c in flex_cats:
        t, sp = targets.get(c, 0), spent.get(c, 0)
        categories.append({
            "category": c, "target": dollars(t), "spent": dollars(sp),
            "over_by": dollars(max(0, sp - t)), "room_left": dollars(room[c]),
            "adjusted_room_left": dollars(room[c] * factor),
        })

    over = [x for x in categories if x["over_by"] > 0]
    total_spent = sum(spent.values())
    return {
        "month": start.strftime("%B %Y"), "as_of": as_of.isoformat(),
        "days_left_including_today": (end - as_of).days + 1,
        "monthly_cap": dollars(cap),
        "spent_so_far": dollars(total_spent),
        "bills_still_expected": dollars(upcoming),
        "projected_if_no_more_flexible_spending": dollars(total_spent + upcoming),
        "flexible_pool": dollars(pool),
        "flexible_spent": dollars(flex_spent),
        "flexible_left": dollars(available),
        "over_cap_by": dollars(max(0, flex_spent - pool)),
        "rebalance": {
            "categories_over_target": [{"category": x["category"], "over_by": x["over_by"]} for x in over],
            "bills_vs_plan": dollars(fixed_committed - obligations_planned),
            "remaining_room_scaled_to": f"{factor:.0%}",
        },
        "categories": categories,
        "obligations": obligations,
    }


# --- Position: what exists vs what is actually spendable -------------------------


def balances(conn, as_of: date) -> list[dict]:
    rows = conn.execute(
        """SELECT a.account_id, a.name, a.role,
                  a.opening_cents + COALESCE(SUM(t.amount_cents), 0)
           FROM accounts a LEFT JOIN transactions t ON t.account_id = a.account_id AND t.date <= ?
           GROUP BY a.account_id ORDER BY a.account_id DESC""", (as_of.isoformat(),)).fetchall()
    return [{"account": r[0], "name": r[1], "role": r[2], "balance": dollars(r[3])} for r in rows]


def financial_position(conn, as_of: date | None = None) -> dict:
    as_of = as_of or today(conn)
    accts = balances(conn, as_of)
    cash = round(sum(a["balance"] for a in accts), 2)
    shared = shared_balances(conn, as_of)
    upcoming = [o for o in obligations_status(conn, as_of) if o["status"] == "upcoming"]
    reserved = round(sum(o["amount"] for o in upcoming), 2)
    return {
        "as_of": as_of.isoformat(),
        "accounts": accts,
        "cash_total": cash,
        "owed_to_you": shared["total_owed_to_you"],
        "you_owe": shared["total_you_owe"],
        "net_position": round(cash + shared["total_owed_to_you"] - shared["total_you_owe"], 2),
        "reserved_for_bills_this_month": reserved,
        "upcoming_bills": [{"name": o["name"], "amount": o["amount"], "due_day": o["due_day"],
                            "estimated": o["estimated"]} for o in upcoming],
        "spendable_cash": round(cash - reserved - shared["total_you_owe"], 2),
        "note": "net_position counts money owed to you; spendable_cash does not (it isn't yours to spend until it "
                "arrives) and it sets aside upcoming bills and what you owe others.",
    }


# --- Recommendations: how much to spend, and why ---------------------------------


def horizon_days(as_of: date, horizon: str) -> list[date]:
    _, end = month_bounds(as_of)
    remaining = [as_of + timedelta(days=i) for i in range((end - as_of).days + 1)]
    if horizon == "today":
        return [as_of]
    if horizon == "weekend":
        # Friday through Sunday of this week (or the coming one), within the month.
        sunday = as_of + timedelta(days=(6 - as_of.weekday()))
        return [d for d in remaining if d <= sunday and d.weekday() >= 4]
    return remaining


def recommend(conn, category: str, horizon: str = "today", log: bool = True) -> dict:
    as_of = today(conn)
    status = budget_status(conn, as_of)
    cat = next((c for c in status["categories"] if c["category"] == category), None)
    if cat is None:
        cat = {"category": category, "target": 0.0, "spent": 0.0, "over_by": 0.0, "room_left": 0.0,
               "adjusted_room_left": 0.0}

    # Split what's left of the category across the occasions still to come. The
    # window being asked about counts as at least one occasion (the user is asking
    # because they plan to spend); other days count by how often they usually spend.
    pattern = spending_pattern(conn, category, as_of)
    chance = pattern["chance_by_weekday"]
    remaining = horizon_days(as_of, "rest_of_month")
    window = horizon_days(as_of, horizon)
    in_window = max(1.0, sum(chance[d.weekday()] for d in window)) if window else 0.0
    later = sum(chance[d.weekday()] for d in remaining if d not in window)
    share = 1.0 if horizon == "rest_of_month" else (in_window / (in_window + later) if window else 0.0)

    amount = round(cat["adjusted_room_left"] * share, 2)
    baseline = round(cat["room_left"] * share, 2)  # what it would be if every category were on plan
    typical = pattern["typical_per_occasion"]

    position = financial_position(conn, as_of)
    capped_by_cash = amount > max(0, position["spendable_cash"])
    if capped_by_cash:
        amount = max(0.0, position["spendable_cash"])

    reasons = [f"{category} plan this month: target ${cat['target']:.2f}, spent ${cat['spent']:.2f}, "
               f"${cat['room_left']:.2f} of room left."]
    rb = status["rebalance"]
    if cat["over_by"] > 0:
        reasons.append(f"{category} is already ${cat['over_by']:.2f} over its target, so there is no room left in it.")
    others = [o for o in rb["categories_over_target"] if o["category"] != category]
    for o in others:
        reasons.append(f"{o['category']} is ${o['over_by']:.2f} over its target; that has to come out of other categories.")
    if abs(rb["bills_vs_plan"]) >= 1:
        word = "higher" if rb["bills_vs_plan"] > 0 else "lower"
        reasons.append(f"Bills are coming in ${abs(rb['bills_vs_plan']):.2f} {word} than planned.")
    if rb["remaining_room_scaled_to"] != "100%" and cat["room_left"] > 0:
        reasons.append(f"To stay under the ${status['monthly_cap']:.0f} cap, every category's remaining room is scaled "
                       f"to {rb['remaining_room_scaled_to']}, leaving ${cat['adjusted_room_left']:.2f} for {category}.")
    if horizon != "rest_of_month" and window:
        reasons.append(f"Based on how often you usually spend on {category}, you can expect about {later:.1f} more "
                       f"occasions after this one before the month ends, so this one gets {share:.0%} of what's left.")
    if capped_by_cash:
        reasons.append("Capped at your spendable cash after bills and what you owe.")

    result = {
        "category": category, "horizon": horizon, "as_of": as_of.isoformat(),
        "recommended": amount, "if_everything_were_on_plan": baseline,
        "your_typical_spend_per_occasion": typical,
        "days_in_window": [d.isoformat() for d in window],
        "reasons": reasons,
        "budget": {"monthly_cap": status["monthly_cap"], "flexible_left": status["flexible_left"],
                   "days_left": status["days_left_including_today"]},
    }

    inputs = {"spent": {c["category"]: c["spent"] for c in status["categories"]},
              "over": {o["category"]: o["over_by"] for o in rb["categories_over_target"]},
              "bills_vs_plan": rb["bills_vs_plan"], "days_left": status["days_left_including_today"]}
    prev = conn.execute("SELECT sim_date, amount_cents, inputs FROM recommendations WHERE category = ? AND horizon = ? "
                        "ORDER BY rec_id DESC LIMIT 1", (category, horizon)).fetchone()
    if prev:
        result["change_since_last_recommendation"] = explain_change(prev, amount, inputs)
    if log:
        conn.execute("INSERT INTO recommendations (sim_date, category, horizon, amount_cents, inputs) VALUES (?, ?, ?, ?, ?)",
                     (as_of.isoformat(), category, horizon, round(amount * 100), json.dumps(inputs)))
    return result


def explain_change(prev: tuple, amount: float, inputs: dict) -> dict:
    prev_date, prev_cents, prev_inputs = prev[0], prev[1], json.loads(prev[2])
    changes = []
    for c, v in inputs["spent"].items():
        delta = round(v - prev_inputs["spent"].get(c, 0), 2)
        if abs(delta) >= 0.01:
            changes.append(f"You spent ${delta:.2f} more on {c}." if delta > 0 else f"{c} spending dropped by ${-delta:.2f}.")
    for c, v in inputs["over"].items():
        before = prev_inputs["over"].get(c, 0)
        if v > before:
            changes.append(f"{c} went from ${before:.2f} to ${v:.2f} over target.")
    if inputs["bills_vs_plan"] != prev_inputs["bills_vs_plan"]:
        changes.append(f"Bills vs plan moved from ${prev_inputs['bills_vs_plan']:.2f} to ${inputs['bills_vs_plan']:.2f}.")
    if inputs["days_left"] != prev_inputs["days_left"]:
        changes.append(f"Days left in the month went from {prev_inputs['days_left']} to {inputs['days_left']}.")
    return {"previous_recommendation": dollars(prev_cents), "previous_date": prev_date,
            "difference": round(amount - dollars(prev_cents), 2), "what_changed": changes or ["Nothing changed."]}


def evaluate_purchase(conn, amount_cents: int, category: str) -> dict:
    """What happens to the budget and cash if the user buys this. Records nothing."""
    as_of = today(conn)
    before = budget_status(conn, as_of)
    after = budget_status(conn, as_of, extra={category: amount_cents})
    position = financial_position(conn, as_of)
    find = lambda s: next((c for c in s["categories"] if c["category"] == category), None)
    cb, ca = find(before), find(after)
    amount = dollars(amount_cents)
    cat_room = cb["adjusted_room_left"] if cb else 0.0

    if amount > position["spendable_cash"]:
        verdict = "not_enough_cash"
    elif amount <= cat_room:
        verdict = "fits_category_budget"
    elif amount <= before["flexible_left"]:
        verdict = "fits_only_by_cutting_other_categories"
    else:
        verdict = "pushes_month_over_cap"

    squeezed = []
    for c in after["categories"]:
        if c["category"] == category:
            continue
        b = next(x for x in before["categories"] if x["category"] == c["category"])
        cut = round(b["adjusted_room_left"] - c["adjusted_room_left"], 2)
        if cut >= 0.5:
            squeezed.append({"category": c["category"], "room_before": b["adjusted_room_left"],
                             "room_after": c["adjusted_room_left"]})
    return {
        "purchase": amount, "category": category, "verdict": verdict,
        "category_room_before": cat_room, "category_room_after": ca["adjusted_room_left"] if ca else 0.0,
        "flexible_left_before": before["flexible_left"], "flexible_left_after": after["flexible_left"],
        "month_spent_after": after["spent_so_far"], "monthly_cap": after["monthly_cap"],
        "over_cap_by_after": after["over_cap_by"],
        "spendable_cash_before": position["spendable_cash"],
        "spendable_cash_after": round(position["spendable_cash"] - amount, 2),
        "other_categories_squeezed": squeezed,
    }


# --- Simulation clock -------------------------------------------------------------


def advance_day(conn) -> dict:
    current = today(conn)
    last = date.fromisoformat(conn.execute("SELECT value FROM meta WHERE key = 'last_day'").fetchone()[0])
    if current >= last:
        return {"today": current.isoformat(), "new_transactions": [], "message": "This is the last day of data."}
    nxt = current + timedelta(days=1)
    conn.execute("UPDATE meta SET value = ? WHERE key = 'today'", (nxt.isoformat(),))
    rows = conn.execute("SELECT account_id, merchant, amount_cents FROM transactions WHERE date = ? AND source = 'statement'",
                        (nxt.isoformat(),)).fetchall()
    return {"today": nxt.isoformat(),
            "new_transactions": [{"account": a, "merchant": m, "amount": dollars(c)} for a, m, c in rows]}
