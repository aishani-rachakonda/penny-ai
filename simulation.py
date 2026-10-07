"""The simulated clock: the one part of Penny that exists only because this is a demo.

In production, time passes on its own and Plaid calls Penny's webhook
(SYNC_UPDATES_AVAILABLE) when an Item has new transactions; Penny then runs
the same sync pipeline. Here, "Next day" moves the session's date forward and
runs that pipeline, so the fake providers reveal the next day's data.
"""

from datetime import timedelta

import db
import notifications
from ingest import pipeline


def advance_day(conn) -> dict:
    current = db.today(conn)
    last = db.meta(conn, "last_day")
    if current.isoformat() >= last:
        return {"today": current.isoformat(), "message": "This is the last day of sandbox data.", "sync": None}
    before = {n["id"] for n in notifications.build(conn)}
    conn.execute("UPDATE meta SET value = ? WHERE key = 'today'", ((current + timedelta(days=1)).isoformat(),))
    report = pipeline.sync_all(conn)  # what the Plaid webhook would trigger
    new = [n for n in notifications.build(conn) if n["id"] not in before]
    return {"today": db.today(conn).isoformat(), "sync": report, "new_notifications": new}
