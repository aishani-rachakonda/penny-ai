"""Recurring payment detection.

Nobody tells Penny about subscriptions. After every sync it looks for them:

1. Gather candidate streams: bank purchases and income grouped by merchant, and
   the user's share of Splitwise expenses grouped by description (bills a
   roommate pays and splits, like electricity).
2. A stream is recurring if it has at least 3 occurrences and a regular rhythm:
   - monthly: at most one per calendar month, in consecutive months, for >= 75% of gaps
   - biweekly / weekly: >= 75% of gaps within 12-16 / 6-8 days
3. Describe it: typical amount, whether it varies, a price change (one clean
   step from an old amount to a new one), the day-of-month window it lands in,
   and the next expected date.
4. Status: 'stopped' if it's overdue by more than half a cycle (a cancelled gym).
5. Kind: income, subscription (fixed amount, subscription-type category), bill
   (rent, utilities, or a varying monthly charge), or other (a regular habit,
   e.g. a monthly Target run, which is not an obligation).

The table is rebuilt from scratch after each sync, so it always reflects the data.
"""

import calendar
import json
import re
import statistics
from collections import defaultdict
from datetime import date, timedelta

import db

BILL_CATEGORIES = {"Rent", "Utilities"}
SUBSCRIPTION_CATEGORIES = {"Subscriptions", "Entertainment", "Personal Care"}


def _key(text: str) -> str:
    return re.sub(r"[^a-z ]", "", text.lower()).strip()


def _streams(conn, today: date) -> dict[str, dict]:
    since = (today - timedelta(days=200)).isoformat()
    streams: dict[str, dict] = {}

    def add(key, label, origin, direction, category, d, cents):
        s = streams.setdefault(key, {"label": label, "origin": origin, "direction": direction, "category": category,
                                     "events": defaultdict(int)})
        s["events"][d] += cents

    for merchant, category, kind, d, cents in conn.execute(
            """SELECT merchant, category, kind, date, amount_cents FROM transactions
               WHERE kind IN ('purchase', 'income') AND date BETWEEN ? AND ?""", (since, today.isoformat())):
        add(f"bank:{_key(merchant)}", merchant, "bank", "in" if kind == "income" else "out", category, d, abs(cents))
    for desc, category, d, share in conn.execute(
            """SELECT x.description, x.category, x.date, s.owed_cents FROM shared_expenses x
               JOIN shared_shares s ON s.expense_id = x.expense_id AND s.person = 'me'
               WHERE x.paid_by != 'me' AND x.date BETWEEN ? AND ?""", (since, today.isoformat())):
        add(f"splitwise:{_key(desc)}", f"{desc} (your share)", "splitwise", "out", category, d, share)
    return streams


def _cadence(dates: list[date]) -> tuple[str, float] | None:
    gaps = [(b - a).days for a, b in zip(dates, dates[1:])]
    months = [d.year * 12 + d.month for d in dates]
    month_steps = [b - a for a, b in zip(months, months[1:])]
    if len(set(months)) == len(months):
        regular = sum(1 for m in month_steps if m == 1) / len(month_steps)
        if regular >= 0.75:
            return "monthly", regular
    for name, lo, hi in (("biweekly", 12, 16), ("weekly", 6, 8)):
        regular = sum(1 for g in gaps if lo <= g <= hi) / len(gaps)
        if regular >= 0.75:
            return name, regular
    return None


def _price_change(amounts: list[int], dates: list[date]) -> dict | None:
    """One clean step from an old fixed amount to a new one (e.g. 10.99 -> 11.99)."""
    for i in range(1, len(amounts)):
        before, after = set(amounts[:i]), set(amounts[i:])
        if len(before) == 1 and len(after) == 1 and before != after and len(amounts[i:]) >= 1:
            return {"from": amounts[0] / 100, "to": amounts[i] / 100, "on": dates[i].isoformat()}
    return None


def _next_due(cadence: str, dates: list[date], today: date) -> date:
    last = dates[-1]
    if cadence != "monthly":
        step = 14 if cadence == "biweekly" else 7
        nxt = last + timedelta(days=step)
        return nxt
    day = round(statistics.median(d.day for d in dates[-6:]))
    y, m = (last.year + (last.month // 12), last.month % 12 + 1)
    return date(y, m, min(day, calendar.monthrange(y, m)[1]))


def detect(conn) -> list[dict]:
    today = db.today(conn)
    found = []
    for key, s in _streams(conn, today).items():
        dates = sorted(date.fromisoformat(d) for d in s["events"])
        if len(dates) < 3:
            continue
        cadence = _cadence(dates)
        if not cadence:
            continue
        cadence, regularity = cadence
        amounts = [s["events"][d.isoformat()] for d in dates]
        change = _price_change(amounts, dates)
        typical = amounts[-1] if change else round(statistics.median(amounts[-6:]))
        variable = not change and (max(amounts) - min(amounts)) > 0.05 * typical
        if cadence != "monthly" and variable and (max(amounts) - min(amounts)) > 0.25 * typical:
            continue  # irregular amounts on a weekly rhythm are a habit, not a bill
        nxt = _next_due(cadence, dates, today)
        cycle = {"monthly": 30, "biweekly": 14, "weekly": 7}[cadence]
        stopped = (today - nxt).days > cycle / 2
        if s["direction"] == "in":
            kind = "income"
        elif s["category"] in BILL_CATEGORIES or (variable and cadence == "monthly" and s["origin"] == "splitwise"):
            kind = "bill"
        elif not variable and s["category"] in SUBSCRIPTION_CATEGORIES | {"Subscriptions"}:
            kind = "subscription"
        else:
            kind = "other"
        if kind == "other" and variable:
            continue  # e.g. lunch at the same place about once a month: a habit, not a recurring payment
        recent_days = [d.day for d in dates[-6:]]
        found.append({
            "series_key": key, "label": s["label"], "origin": s["origin"], "direction": s["direction"], "kind": kind,
            "category": s["category"], "cadence": cadence, "typical_cents": typical, "last_cents": amounts[-1],
            "low_cents": min(amounts[-6:]), "high_cents": max(amounts[-6:]), "variable": int(variable),
            "occurrences": len(dates), "first_date": dates[0].isoformat(), "last_date": dates[-1].isoformat(),
            "next_due": None if stopped else nxt.isoformat(),
            "due_day_low": min(recent_days) if cadence == "monthly" else None,
            "due_day_high": max(recent_days) if cadence == "monthly" else None,
            "status": "stopped" if stopped else "active",
            "price_change": json.dumps(change) if change else None,
            "confidence": round(min(0.99, 0.5 + 0.08 * len(dates)) * regularity, 2),
        })

    conn.execute("DELETE FROM recurring_series")
    if found:
        cols = list(found[0])
        conn.executemany(f"INSERT INTO recurring_series ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                         [[f[c] for c in cols] for f in found])
    return found


def series(conn, include_stopped: bool = True) -> list[dict]:
    rows = conn.execute("SELECT * FROM recurring_series ORDER BY direction DESC, typical_cents DESC").fetchall()
    cols = [c[0] for c in conn.execute("SELECT * FROM recurring_series LIMIT 0").description]
    verdicts = dict(conn.execute("SELECT series_key, verdict FROM recurring_verdicts"))
    out = []
    for r in rows:
        s = dict(zip(cols, r))
        if s["status"] == "stopped" and not include_stopped:
            continue
        s["price_change"] = json.loads(s["price_change"]) if s["price_change"] else None
        s["dismissed"] = verdicts.get(s["series_key"]) == "dismissed"
        out.append(s)
    return out
