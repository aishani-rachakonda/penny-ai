"""Reminders and alerts: the rules a production notification job would run.

In production a scheduled job (e.g. Cloud Scheduler -> Cloud Run job, daily per
user and after every sync) evaluates these rules, stores each notification once,
and delivers it by Web Push, mobile push (FCM/APNs) or email. In this demo the
same rules run whenever the dashboard refreshes and show up in the notification
feed; new ones are highlighted after "Next day".

Every notification says where its facts came from (synced, detected, or the user's plan).
"""

from datetime import date, timedelta

import db

LABEL = {"you told Penny": "Added by you", "detected, confirmed by you": "Found by Penny",
         "detected by Penny": "Found by Penny"}


EVERY = {"monthly": "every month", "biweekly": "every 2 weeks", "weekly": "every week"}


def _month(iso: str) -> str:
    return date.fromisoformat(iso).strftime("%B")


def _day(iso: str) -> str:
    d = date.fromisoformat(iso)
    return f"{d.strftime('%b')} {d.day}"
import finance
import planning
import recurring

REMIND_DAYS_BEFORE = 3


def build(conn) -> list[dict]:
    today = db.today(conn)
    out = []

    # Bills due soon (plan bills and detected ones), each reminded once per due date.
    for b in planning.bills_for_month(conn, today):
        if b["status"] != "upcoming":
            continue
        start = today.replace(day=min(b["due_day_start"], 28))
        end = today.replace(day=min(b["due_day_end"], 28))
        if start - timedelta(days=REMIND_DAYS_BEFORE) <= today <= end:
            when = "today" if end == today else f"by {end.strftime('%b %d')}" if start != end else end.strftime("%b %d")
            amount = f"~${b['amount']:.2f}" if b["estimated"] else f"${b['amount']:.2f}"
            out.append({"id": f"bill:{b['name']}:{end}", "type": "bill_due", "severity": "info",
                        "title": f"{b['name']} due {when}", "detail": f"{amount} · {LABEL.get(b['source'], b['source'])}",
                        "source": b["source"]})

    # Recurring charges Penny found that aren't in the plan, price changes, and probable cancellations.
    in_plan = {b["detected_match"] for b in planning.bills_for_month(conn, today) if b["in_plan"]}
    for s in recurring.series(conn):
        if s["direction"] != "out" or s["kind"] not in ("bill", "subscription") or s["dismissed"]:
            continue
        if s["status"] == "active" and s["label"] not in in_plan:
            out.append({"id": f"found:{s['series_key']}", "type": "detected_recurring", "severity": "info",
                        "title": f"Recurring charge found: {s['label']}",
                        "detail": f"${s['last_cents'] / 100:.2f} {EVERY[s['cadence']]} "
                                  f"since {_month(s['first_date'])}. Not in your plan yet.", "source": "detected by Penny"})
        if s["price_change"] and (today - date.fromisoformat(s["price_change"]["on"])).days <= 120:
            pc = s["price_change"]
            out.append({"id": f"price:{s['series_key']}:{pc['on']}", "type": "price_change", "severity": "warn",
                        "title": f"{s['label']} went up", "detail": f"${pc['from']:.2f} → ${pc['to']:.2f} since {_day(pc['on'])}",
                        "source": "detected by Penny"})
        if s["status"] == "stopped" and (today - date.fromisoformat(s["last_date"])).days <= 120:
            out.append({"id": f"stopped:{s['series_key']}", "type": "stopped", "severity": "info",
                        "title": f"{s['label']} stopped charging",
                        "detail": f"Last charged {_day(s['last_date'])}. If you cancelled it, you're all set.",
                        "source": "detected by Penny"})

    # Budget pace and cross-source mismatches.
    status = finance.budget_status(conn, today)
    for c in sorted(status["categories"], key=lambda c: -c["over_by"])[:2]:
        if c["over_by"] >= 10:
            out.append({"id": f"over:{c['category']}:{status['month']}:{int(c['over_by'] // 10)}", "type": "over_budget",
                        "severity": "warn", "title": f"{c['category']} is ${c['over_by']:.0f} over target",
                        "detail": f"${c['spent']:.2f} of ${c['target']:.2f} this month", "source": "your plan"})
    for m in finance.shared_balances(conn, today)["mismatches"]:
        out.append({"id": f"mismatch:{m['person']}:{m['type']}:{m['date']}", "type": "mismatch", "severity": "warn",
                    "title": "Splitwise and your bank disagree", "detail": m["detail"], "source": "Splitwise × bank"})
    return out
