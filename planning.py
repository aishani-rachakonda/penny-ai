"""Monthly plans: what the user told Penny, plus what Penny detected and suggests.

A plan belongs to one month and holds:
    the monthly cap                   user-provided
    bills (with due windows)          user-provided ("rent is $1,250, due 1st-10th"),
                                      or detected payments the user confirmed into the plan
    category targets                  Penny's suggestion from history, or the user's own numbers

A month nobody planned inherits the previous month's plan ("carried_forward"),
so budgeting never stops. Detected bills that aren't in the plan still count as
expected costs, labelled as detected, until the user confirms or dismisses them.
"""

import calendar
from datetime import date, timedelta

import db
import recurring


def month_key(d: date) -> str:
    return d.strftime("%Y-%m")


def month_start(month: str) -> date:
    return date.fromisoformat(month + "-01")


def ensure_plan(conn, month: str) -> str:
    """Make sure a plan exists for this month, carrying the latest earlier plan forward if needed."""
    if conn.execute("SELECT 1 FROM plans WHERE month = ?", (month,)).fetchone():
        return month
    prev = conn.execute("SELECT month, monthly_cap_cents FROM plans WHERE month < ? ORDER BY month DESC LIMIT 1",
                        (month,)).fetchone()
    if not prev:
        raise ValueError(f"No plan exists before {month}.")
    conn.execute("INSERT INTO plans VALUES (?, ?, 'carried_forward', ?)", (month, prev[1], db.today(conn).isoformat()))
    conn.execute("INSERT INTO plan_bills SELECT ?, name, category, amount_cents, due_day_start, due_day_end, how_paid, "
                 "match, source FROM plan_bills WHERE month = ?", (month, prev[0]))
    conn.execute("INSERT INTO plan_categories SELECT ?, category, target_cents, source FROM plan_categories "
                 "WHERE month = ?", (month, prev[0]))
    return month


def _series_for(conn, match: str) -> dict | None:
    m = match.lower()
    for s in recurring.series(conn):
        if m in s["label"].lower() or m in s["series_key"]:
            return s
    return None


def bills_for_month(conn, as_of: date) -> list[dict]:
    """Every expected bill this month: from the plan (user-provided or confirmed) and detected ones not in the plan."""
    month = ensure_plan(conn, month_key(as_of))
    start = month_start(month)
    end = start.replace(day=calendar.monthrange(start.year, start.month)[1])
    s, d = start.isoformat(), min(as_of, end).isoformat()
    out, claimed = [], set()

    def paid(how: str, match: str):
        if how == "bank":
            return conn.execute("SELECT SUM(-amount_cents), MAX(date) FROM transactions WHERE kind = 'purchase' "
                                "AND (merchant LIKE ? OR description LIKE ?) AND date BETWEEN ? AND ?",
                                (f"%{match}%", f"%{match}%", s, d)).fetchone()
        return conn.execute("""SELECT SUM(sh.owed_cents), MAX(x.date) FROM shared_expenses x
                               JOIN shared_shares sh ON sh.expense_id = x.expense_id AND sh.person = 'me'
                               WHERE x.description LIKE ? AND x.date BETWEEN ? AND ?""",
                            (f"%{match}%", s, d)).fetchone()

    for name, category, amount, lo, hi, how, match, source in conn.execute(
            "SELECT name, category, amount_cents, due_day_start, due_day_end, how_paid, match, source "
            "FROM plan_bills WHERE month = ?", (month,)).fetchall():
        series = _series_for(conn, match)
        if series:
            claimed.add(series["series_key"])
        expected = amount if amount is not None else (series["typical_cents"] if series else 0)
        row = paid(how, match)
        is_paid = row[0] is not None
        out.append({"name": name, "category": category, "status": "paid" if is_paid else "upcoming",
                    "amount": (row[0] if is_paid else expected) / 100, "expected": expected / 100,
                    "estimated": amount is None and not is_paid, "due": f"{lo}-{hi}" if lo != hi else str(lo),
                    "due_day_start": lo, "due_day_end": hi, "paid_on": row[1] if is_paid else None,
                    "source": "you told Penny" if source == "user" else "detected, confirmed by you",
                    "in_plan": True, "detected_match": series["label"] if series else None})

    for sr in recurring.series(conn, include_stopped=False):
        if sr["series_key"] in claimed or sr["dismissed"] or sr["direction"] != "out" \
                or sr["kind"] not in ("bill", "subscription"):
            continue
        match = sr["label"].replace(" (your share)", "")
        row = paid("bank" if sr["origin"] == "bank" else "splitwise", match)
        is_paid = row[0] is not None
        due_day = date.fromisoformat(sr["next_due"]).day if sr["next_due"] else sr["due_day_low"]
        if not is_paid and sr["next_due"] and sr["next_due"] > end.isoformat():
            continue  # its next charge falls after this month
        out.append({"name": sr["label"], "category": sr["category"], "status": "paid" if is_paid else "upcoming",
                    "amount": (row[0] if is_paid else sr["last_cents"]) / 100, "expected": sr["last_cents"] / 100,
                    "estimated": bool(sr["variable"]) and not is_paid, "due": str(due_day),
                    "due_day_start": due_day, "due_day_end": due_day, "paid_on": row[1] if is_paid else None,
                    "source": "detected by Penny", "in_plan": False, "detected_match": sr["label"],
                    "series_key": sr["series_key"]})
    return out


def plan_totals(conn, month: str, targets_override: dict[str, int] | None = None) -> dict:
    """Totals for a month's plan, so nobody has to add them up by hand (least of all the model)."""
    month = ensure_plan(conn, month)
    today = db.today(conn)
    as_of = today if month == month_key(today) else month_start(month)
    bills = bills_for_month(conn, as_of)
    in_plan = sum(round(b["expected"] * 100) for b in bills if b["in_plan"])
    detected = sum(round(b["expected"] * 100) for b in bills if not b["in_plan"])
    cap = conn.execute("SELECT monthly_cap_cents FROM plans WHERE month = ?", (month,)).fetchone()[0]
    targets = targets_override if targets_override is not None else dict(
        conn.execute("SELECT category, target_cents FROM plan_categories WHERE month = ?", (month,)))
    total = in_plan + detected + sum(targets.values())
    return {"monthly_cap": cap / 100, "bills_in_plan": in_plan / 100, "detected_bills_not_in_plan": detected / 100,
            "category_targets": sum(targets.values()) / 100, "planned_total": total / 100,
            "left_unallocated": (cap - total) / 100}


def suggest_targets(conn, month: str, cap_cents: int, bills_cents: int) -> dict[str, int]:
    """Average monthly spend per flexible category over 6 months, scaled so bills + targets = cap."""
    import finance
    averages = finance.historical_category_averages(conn, month_start(month), months=6)
    flexible = set(db.categories(conn, "flexible"))
    base = {c: v for c, v in averages.items() if c in flexible and v > 0}
    pool = max(0, cap_cents - bills_cents)
    if not base:
        return {}
    scale = pool / sum(base.values())
    targets = {c: round(v * scale / 500) * 500 for c, v in base.items()}
    largest = max(targets, key=targets.get)
    targets[largest] += pool - sum(targets.values())
    return targets


def create_plan_from_profile(conn, profile: dict, as_of: date):
    """The first plan: the user's cap and known bills, Penny's suggested targets."""
    month = month_key(as_of)
    conn.execute("INSERT INTO plans VALUES (?, ?, 'user', ?)", (month, round(profile["monthly_cap"] * 100), as_of.isoformat()))
    for b in profile["known_bills"]:
        category = "Rent" if "rent" in b["name"].lower() else "Utilities"
        conn.execute("INSERT INTO plan_bills VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'user')",
                     (month, b["name"], category, round(b["amount"] * 100) if b["amount"] is not None else None,
                      b["due_day_start"], b["due_day_end"], b["how_paid"], b["match"]))
    bills = sum(round(b["expected"] * 100) for b in bills_for_month(conn, as_of))
    for c, t in suggest_targets(conn, month, round(profile["monthly_cap"] * 100), bills).items():
        conn.execute("INSERT INTO plan_categories VALUES (?, ?, ?, 'penny')", (month, c, t))


def draft_plan(conn, month: str) -> dict:
    """A proposal for a month, for the planning conversation. Saves nothing."""
    import finance
    today = db.today(conn)
    current = ensure_plan(conn, month_key(today))
    cap = conn.execute("SELECT monthly_cap_cents FROM plans WHERE month = ?", (current,)).fetchone()[0]
    bills = [{"name": n, "amount": a / 100 if a is not None else None, "due": f"{lo}-{hi}" if lo != hi else str(lo),
              "source": "you told Penny" if src == "user" else "detected, confirmed by you"}
             for n, a, lo, hi, src in conn.execute(
                 "SELECT name, amount_cents, due_day_start, due_day_end, source FROM plan_bills WHERE month = ?",
                 (current,))]
    in_plan = {b["detected_match"] for b in bills_for_month(conn, today) if b["in_plan"] and b["detected_match"]}
    suggestions, stopped, price_changes = [], [], []
    for s in recurring.series(conn):
        if s["direction"] != "out" or s["kind"] not in ("bill", "subscription") or s["dismissed"]:
            continue
        if s["price_change"]:
            price_changes.append({"name": s["label"], **s["price_change"]})
        if s["status"] == "stopped":
            stopped.append({"name": s["label"], "last_charged": s["last_date"], "amount": s["last_cents"] / 100})
        elif s["label"] not in in_plan:
            suggestions.append({"name": s["label"], "amount": s["last_cents"] / 100, "cadence": s["cadence"],
                                "next_due": s["next_due"], "series_key": s["series_key"],
                                "variable": bool(s["variable"])})
    estimate = sum((b["amount"] or 0) for b in bills) + sum(x["amount"] for x in suggestions)
    for b in bills:
        if b["amount"] is None:
            sr = _series_for(conn, b["name"].split("(")[-1].split(")")[0]) or _series_for(conn, b["name"].split()[0])
            b["estimated_amount"] = sr["typical_cents"] / 100 if sr else None
            estimate += b["estimated_amount"] or 0
    targets = suggest_targets(conn, month, cap, round(estimate * 100))
    last_month = finance.spending_by_category(conn, *finance.month_bounds(month_start(month) - timedelta(days=1)))
    current_targets = dict(conn.execute("SELECT category, target_cents FROM plan_categories WHERE month = ?", (current,)))
    return {
        "month": month, "monthly_cap": cap / 100,
        "totals_if_accepted": plan_totals(conn, current, targets) | {
            "note": "Detected bills not in the plan are already counted as expected costs."},
        "bills_you_told_penny": bills,
        "detected_recurring_not_in_plan": suggestions,
        "detected_stopped": stopped,
        "price_changes": price_changes,
        "suggested_category_targets": [
            {"category": c, "suggested": t / 100, "current_target": current_targets.get(c, 0) / 100,
             "spent_in_previous_month": last_month.get(c, 0) / 100} for c, t in sorted(targets.items(), key=lambda kv: -kv[1])],
        "note": "Nothing is saved yet. Ask the user what to change, then call update_plan with the final numbers.",
    }
