"""Fake Plaid and Splitwise clients that serve the sandbox fixtures.

They mirror the real APIs' method names and response shapes, so the ingestion
code in ingest/ cannot tell them apart from the real thing:

    SandboxPlaid.transactions_sync(access_token, cursor)   ~ POST /transactions/sync
    SandboxPlaid.accounts_balance_get(access_token)        ~ POST /accounts/balance/get
    SandboxSplitwise.get_current_user()                    ~ GET  /get_current_user
    SandboxSplitwise.get_friends() / get_groups()          ~ GET  /get_friends, /get_groups
    SandboxSplitwise.get_expenses(updated_after, ...)      ~ GET  /get_expenses
    SandboxSplitwise.create_expense(...)                   ~ POST /create_expense

What makes it a simulation is the clock: each session has a simulated "today",
and the fake providers only reveal what would exist by then. A card purchase is
visible as pending from its authorized date and replaced by its posted version
on the posting date, just as Plaid reports it. Writes (create_expense) are kept
per session, so one visitor's Splitwise changes never leak into another's.
"""

import base64
import json
from datetime import date, datetime, timedelta
from pathlib import Path

DATA = Path(__file__).parent / "data"
_PLAID = json.load(open(DATA / "plaid_items.json"))
_SPLITWISE = json.load(open(DATA / "splitwise.json"))
PROFILE = json.load(open(DATA / "user_profile.json"))


class ProviderError(Exception):
    """Same role as plaid.ApiException / an HTTP 4xx from Splitwise."""


def _public(t: dict) -> dict:
    return {k: v for k, v in t.items() if not k.startswith("_sandbox")}


class SandboxPlaid:
    def __init__(self, clock):
        self.clock = clock  # callable returning the session's simulated date
        self.items = {i["access_token"]: i for i in _PLAID["items"]}

    def link_tokens(self) -> list[dict]:
        """The Items a user has connected. In production these come from Plaid Link + /item/public_token/exchange."""
        return [{"access_token": i["access_token"], "item_id": i["item_id"], "institution_id": i["institution_id"],
                 "institution_name": i["institution_name"]} for i in _PLAID["items"]]

    def _visible(self, item: dict, on: date) -> dict[str, dict]:
        """Every transaction Plaid would report on a given day, keyed by transaction_id."""
        out = {}
        day = on.isoformat()
        for t in item["transactions"]:
            if t["_sandbox_has_pending_phase"] and t["authorized_date"] <= day < t["date"]:
                pending = _public(t) | {"transaction_id": "pend-" + t["transaction_id"], "pending": True,
                                        "pending_transaction_id": None, "date": t["authorized_date"]}
                out[pending["transaction_id"]] = pending
            elif t["date"] <= day:
                posted = _public(t) | {"pending": False,
                                       "pending_transaction_id": "pend-" + t["transaction_id"]
                                       if t["_sandbox_has_pending_phase"] else None}
                out[posted["transaction_id"]] = posted
        return out

    def _accounts(self, item: dict, on: date) -> list[dict]:
        visible = self._visible(item, on).values()
        accounts = []
        for a in item["accounts"]:
            mine = [t for t in visible if t["account_id"] == a["account_id"]]
            current = a["_sandbox_opening_balance"] - sum(t["amount"] for t in mine if not t["pending"])
            available = current - sum(t["amount"] for t in mine if t["pending"])
            accounts.append(_public(a) | {"balances": {"current": round(current, 2), "available": round(available, 2),
                                                       "iso_currency_code": "USD", "limit": None}})
        return accounts

    def transactions_sync(self, access_token: str, cursor: str | None = None, count: int = 500) -> dict:
        item = self.items.get(access_token)
        if item is None:
            raise ProviderError("INVALID_ACCESS_TOKEN: the provided access token is invalid or was revoked")
        today = self.clock()
        state = json.loads(base64.b64decode(cursor)) if cursor else {"through": None, "sent": []}
        before = set(state["sent"])
        now = self._visible(item, today)
        added_ids = [i for i in now if i not in before]
        removed_ids = [i for i in before if i not in now]
        # Plaid pages large updates; mimic that so the client has to follow has_more.
        page, rest = added_ids[:count], added_ids[count:]
        sent = sorted((before - set(removed_ids)) | set(page))
        next_cursor = base64.b64encode(json.dumps({"through": today.isoformat(), "sent": sent}).encode()).decode()
        return {
            "added": [now[i] for i in page],
            "modified": [],
            "removed": [{"transaction_id": i, "account_id": None} for i in removed_ids],
            "next_cursor": next_cursor,
            "has_more": bool(rest),
            "accounts": self._accounts(item, today),
            "transactions_update_status": "HISTORICAL_UPDATE_COMPLETE",
            "request_id": f"sandbox-{today.isoformat()}",
        }

    def accounts_balance_get(self, access_token: str) -> dict:
        item = self.items[access_token]
        return {"accounts": self._accounts(item, self.clock()), "item": {"item_id": item["item_id"]}}


class SandboxSplitwise:
    def __init__(self, clock, conn):
        self.clock = clock
        self.conn = conn  # session database, which holds this session's own Splitwise writes

    def get_current_user(self) -> dict:
        return {"user": _SPLITWISE["current_user"]}

    def get_friends(self) -> dict:
        return {"friends": _SPLITWISE["friends"]}

    def get_groups(self) -> dict:
        return {"groups": _SPLITWISE["groups"]}

    def _end_of_today(self) -> str:
        d = self.clock()
        return datetime(d.year, d.month, d.day, 23, 59, 59).isoformat() + "Z"

    def get_expenses(self, updated_after: str | None = None, limit: int = 100, offset: int = 0) -> dict:
        cutoff = self._end_of_today()
        writes = [json.loads(r[0]) for r in self.conn.execute("SELECT payload FROM sandbox_splitwise_writes")]
        pool = [e for e in _SPLITWISE["expenses"] + writes
                if e["created_at"] <= cutoff and (updated_after is None or e["updated_at"] > updated_after)]
        pool.sort(key=lambda e: (e["updated_at"], e["id"]))
        return {"expenses": pool[offset:offset + limit]}

    def create_expense(self, cost: str, description: str, users: list[dict], payment: bool = False,
                       category_name: str = "General", group_id: int | None = None) -> dict:
        """users: [{"user_id", "paid_share", "owed_share"}], as in Splitwise's users__N__ form fields."""
        friends = {f["id"]: f for f in _SPLITWISE["friends"]} | {_SPLITWISE["current_user"]["id"]: _SPLITWISE["current_user"]}
        for u in users:
            if u["user_id"] not in friends:
                raise ProviderError(f"user {u['user_id']} is not a friend of the current user")
        paid = sum(float(u["paid_share"]) for u in users)
        owed = sum(float(u["owed_share"]) for u in users)
        if abs(paid - float(cost)) > 0.01 or abs(owed - float(cost)) > 0.01:
            raise ProviderError("paid_share and owed_share must each add up to cost")
        n = self.conn.execute("SELECT COUNT(*) FROM sandbox_splitwise_writes").fetchone()[0]
        d = self.clock()  # strictly increasing timestamps so updated_after never skips a write
        now = (datetime(d.year, d.month, d.day, 22, 0, 0) + timedelta(seconds=n)).isoformat() + "Z"
        expense = {
            "id": 3499000000 + n, "group_id": group_id, "description": description, "payment": payment,
            "cost": f"{float(cost):.2f}", "currency_code": "USD", "date": now, "created_at": now, "updated_at": now,
            "deleted_at": None, "category": {"id": 18, "name": category_name},
            "users": [{"user_id": u["user_id"],
                       "user": {"id": u["user_id"], "first_name": friends[u["user_id"]]["first_name"],
                                "last_name": friends[u["user_id"]]["last_name"]},
                       "paid_share": f"{float(u['paid_share']):.2f}", "owed_share": f"{float(u['owed_share']):.2f}",
                       "net_balance": f"{float(u['paid_share']) - float(u['owed_share']):.2f}"} for u in users],
        }
        self.conn.execute("INSERT INTO sandbox_splitwise_writes (payload) VALUES (?)", (json.dumps(expense),))
        return {"expenses": [expense], "errors": {}}
