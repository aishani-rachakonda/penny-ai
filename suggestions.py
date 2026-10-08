"""Suggested questions, chosen from the user's current situation.

Rebuilt on every dashboard refresh (after each chat turn and each new day), so
the suggestions always point at what is worth asking right now: whatever is
over budget, who owes money, a bill Penny found that isn't in the plan, the
next month to plan. Generic questions only fill the remaining slots.
"""

from datetime import date, timedelta

import db
import finance
import planning
import recurring

MAX = 6


def build(conn) -> list[str]:
    today = db.today(conn)
    status = finance.budget_status(conn, today)
    shared = finance.shared_balances(conn, today)
    bills = planning.bills_for_month(conn, today)
    out: list[str] = []

    def add(q: str):
        if q not in out:
            out.append(q)

    over = sorted((c for c in status["categories"] if c["over_by"] >= 5), key=lambda c: -c["over_by"])
    if over:
        add(f"Why am I over on {over[0]['category']}?")

    flexible = [c for c in status["categories"] if c["target"] > 0]
    if today.weekday() in (4, 5):
        add("I'm going out tonight. How much should I spend?")
    elif flexible:
        top = next((c for c in flexible if c["category"] == "Dining"), max(flexible, key=lambda c: c["target"]))
        add(f"How much can I spend on {top['category'].lower()} today?")

    for m in shared["mismatches"]:
        if m["type"] == "recorded_but_not_received":
            add(f"Did {m['person']} actually pay me back?")
            break
    for p in shared["people"]:
        if p["balance"] > 0 and p["days_open"] >= 14:
            add(f"{p['person']} has owed me for {p['days_open']} days. What should I do?")
            break

    detected = [b for b in bills if not b["in_plan"]]
    if detected:
        add(f"Should I add {detected[0]['name']} to my plan?")
    for s in recurring.series(conn, include_stopped=False):
        if s["price_change"] and (today - date.fromisoformat(s["price_change"]["on"])).days <= 120:
            add(f"{s['label']} went up. Is it still worth it?")
            break
    if any(b["status"] == "upcoming" and 0 <= b["due_day_end"] - today.day <= 3 for b in bills):
        add("What's due this week?")

    next_month = (today.replace(day=28) + timedelta(days=4)).strftime("%B")
    if status["plan"].startswith("carried"):
        add(f"Let's set up my {today.strftime('%B')} plan.")
    elif today.day >= 20:
        add(f"Let's plan {next_month}.")

    if conn.execute("SELECT 1 FROM recommendations").fetchone():
        add("Why did you change my recommendation?")

    # One slot is kept for something Penny can *do*, so its actions are discoverable.
    situational, out = out[:MAX - 1], []
    if not conn.execute("SELECT 1 FROM transactions WHERE source = 'manual'").fetchone():
        add("I just spent $30 at Zara.")
    if not conn.execute("SELECT 1 FROM shared_expenses WHERE link_source = 'user'").fetchone():
        friends = [r[0] for r in conn.execute(
            """SELECT s.person FROM shared_shares s JOIN shared_expenses x USING (expense_id)
               WHERE s.person != 'me' AND x.paid_by = 'me' GROUP BY s.person ORDER BY COUNT(*) DESC LIMIT 2""")]
        if len(friends) == 2:
            add(f"I paid $90 for dinner with {friends[0]} and {friends[1]}. Split it.")

    actions = out[:1]
    out = situational
    spent = sorted(flexible, key=lambda c: -c["spent"])
    if spent:
        add(f"How does my {spent[0]['category'].lower()} spending compare to last month?")
    add("How much can I spend for the rest of the month?")
    add("What are my small purchases adding up to?")
    return out[:MAX - len(actions)] + actions
