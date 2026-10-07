import json
import os
import threading
import uuid
from pathlib import Path

import litellm
import uvicorn
from fastapi import FastAPI
from fastapi.responses import FileResponse
from pydantic import BaseModel

import db
import finance
import notifications
import planning
import recurring
import simulation
from tools import TOOLS, run_tool

# --- Config ---

MODEL = os.environ.get("PENNY_MODEL", "vertex_ai/gemini-3.5-flash")
MAX_TOOL_ROUNDS = 8

SYSTEM_PROMPT = """You are Penny, a personal finance agent. You help {user} keep spending under a monthly cap, \
track who owes whom, stay ahead of bills, and decide how much they can spend right now.

Today is {weekday}, {today}. This is a simulated date: never refer to data after it.

Where your information comes from (say which when it matters):
- Bank accounts and transactions: synced from {banks} through Plaid. Purchases can be pending for a day or two.
- Shared expenses and IOUs: synced from Splitwise, cross-checked against the bank.
- Recurring payments (subscriptions, bills, paychecks): DETECTED by you from the transactions. Nobody entered them.
- The monthly plan (cap, bills with due windows, category targets): provided by {user}, with targets you suggested \
from their history unless they set their own. The cap is ${cap:,.0f} for {month} and includes bills.

What {user} has asked you to remember:
{memory}

Rules:
- Never calculate money in your head or guess a number. Every dollar figure must come from a tool result in this \
conversation. If a tool can't answer it, say so.
- Data changes during the conversation (new syncs, new transactions, the date moving), so call tools again rather \
than reusing numbers from earlier turns.
- "Net position" counts money people owe {user}; "spendable cash" does not.
- When asked how much to spend, use recommend_spending and explain the main reasons plainly.
- When {user} says they spent, received or moved money, record it with add_transaction. If they paid for a group, \
record it, then split it with split_transaction. Only ask if the account, amount or people are genuinely unclear.
- Planning a month: call draft_monthly_plan, walk {user} through it (bills they told you about, recurring payments \
you detected that aren't in the plan, suggested targets), ask what to change, then save with update_plan.
- Categories belong to {user}: add, remove, rename or re-map them with update_plan and recategorize_merchant.
- When {user} shares a goal or preference worth keeping, save it with update_memory.
- If a tool returns an error, fix the arguments and retry, or tell {user} what you need.
- Be concise and warm: lead with the answer, then 1-3 short reasons."""


def system_prompt(conn) -> str:
    today = db.today(conn)
    month = planning.ensure_plan(conn, planning.month_key(today))
    cap = conn.execute("SELECT monthly_cap_cents FROM plans WHERE month = ?", (month,)).fetchone()[0] / 100
    banks = " and ".join(r[0] for r in conn.execute("SELECT institution FROM connections WHERE provider = 'plaid'"))
    notes = conn.execute("SELECT note_id, text FROM memory_notes ORDER BY note_id").fetchall()
    memory = "\n".join(f"- (note {i}) {t}" for i, t in notes) or "- Nothing yet."
    return SYSTEM_PROMPT.format(user=db.user_name(conn), weekday=today.strftime("%A"), today=today.strftime("%B %d, %Y"),
                                banks=banks, cap=cap, month=today.strftime("%B"), memory=memory)


# --- The Harness ---


def run_agent(messages: list[dict], conn) -> tuple[str, list[dict]]:
    """Complete until the model answers without asking for a tool.

    Returns the final text and a record of every tool call made along the way.
    """
    tool_calls = []

    for _ in range(MAX_TOOL_ROUNDS):
        reply = litellm.completion(
            model=MODEL,
            vertex_location="global",
            messages=messages,
            tools=TOOLS,
        ).choices[0].message

        # Append assistant's reply (text, tool calls, or both) to the context.
        # model_dump() keeps it a plain dict: the raw object carries provider-specific
        # fields that trip Pydantic when LiteLLM re-serializes it next round.
        messages += [reply.model_dump()]

        if not reply.tool_calls:
            if (reply.content or "").strip():
                return reply.content, tool_calls
            # Models occasionally return an empty turn. Drop it and ask again.
            messages.pop()
            continue

        # The harness, not the model, runs each tool and appends the result
        for call in reply.tool_calls:
            try:
                args = json.loads(call.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            result = run_tool(conn, call.function.name, args)
            tool_calls += [{"name": call.function.name, "args": args, "result": result}]

            messages += [{"role": "tool", "tool_call_id": call.id, "content": result}]

    return "Sorry, I hit my tool-call limit before finishing.", tool_calls


# --- Session Store ---

# At startup, onboard the demo user through the real pipeline: connect the (sandbox) Plaid and
# Splitwise sources, sync, link, detect recurring payments, draft the first plan. A file copy is
# written to penny.db so the database can be opened and inspected with any SQLite browser.
try:
    SEED = db.build_seed(Path(__file__).parent / "penny.db")
except OSError:  # read-only filesystem: skip the inspectable copy
    SEED = db.build_seed()


class Session:
    """One visitor: their conversation and their own private copy of the finances."""

    def __init__(self):
        self.db = db.session_copy(SEED)
        self.messages = [{"role": "system", "content": system_prompt(self.db)}]
        self.lock = threading.Lock()


# session_id -> Session. In-memory, single process.
sessions: dict[str, Session] = {}


def get_session(session_id: str | None) -> tuple[str, Session]:
    session_id = session_id or str(uuid.uuid4())
    if session_id not in sessions:
        sessions[session_id] = Session()
    return session_id, sessions[session_id]


# --- FastAPI App ---

app = FastAPI()


class ChatRequest(BaseModel):
    message: str
    session_id: str | None = None


class ChatResponse(BaseModel):
    response: str
    session_id: str
    tool_calls: list[dict]


@app.get("/")
def index():
    return FileResponse(Path(__file__).parent / "index.html")


@app.post("/chat", response_model=ChatResponse)
def chat(request: ChatRequest):
    session_id, s = get_session(request.session_id)
    with s.lock:
        # The date or plan may have changed since the last turn; keep the system prompt current.
        s.messages[0] = {"role": "system", "content": system_prompt(s.db)}
        s.messages += [{"role": "user", "content": request.message}]
        try:
            response, tool_calls = run_agent(s.messages, s.db)
        except Exception as e:
            # Auth, billing, a model that is not running: show it in the chat, not as a 500.
            response, tool_calls = f"Model call failed: {type(e).__name__}: {str(e)[:300]}", []

    return ChatResponse(response=response or "", session_id=session_id, tool_calls=tool_calls)


@app.get("/state")
def state(session_id: str | None = None):
    """Everything the dashboard shows, computed fresh from this session's data."""
    session_id, s = get_session(session_id)
    with s.lock:
        conn = s.db
        connections = []
        for cid, provider, institution, last in conn.execute(
                "SELECT connection_id, provider, institution, last_synced FROM connections").fetchall():
            if provider == "plaid":
                accounts = [f"...{m}" for (m,) in conn.execute("SELECT mask FROM accounts WHERE connection_id = ?", (cid,))]
                count = conn.execute("SELECT COUNT(*) FROM transactions t JOIN accounts a USING (account_id) "
                                     "WHERE a.connection_id = ?", (cid,)).fetchone()[0]
                what = f"{len(accounts)} account ({', '.join(accounts)}) · {count} transactions"
            else:
                count = conn.execute("SELECT (SELECT COUNT(*) FROM shared_expenses) + (SELECT COUNT(*) FROM settlements)").fetchone()[0]
                friends = conn.execute("SELECT COUNT(*) FROM people").fetchone()[0]
                what = f"{friends} friends · {count} expenses & payments"
            connections.append({"provider": "Plaid" if provider == "plaid" else "Splitwise API", "institution": institution,
                                "last_synced": last, "detail": what})
        return {
            "session_id": session_id,
            "today": db.meta(conn, "today"), "start": db.meta(conn, "start"), "last_day": db.meta(conn, "last_day"),
            "user": db.user_name(conn),
            "connections": connections,
            "position": finance.financial_position(conn),
            "budget": finance.budget_status(conn),
            "shared": finance.shared_balances(conn),
            "recurring": recurring.series(conn),
            "notifications": notifications.build(conn),
            "memory": [{"id": i, "text": t} for i, t in conn.execute("SELECT note_id, text FROM memory_notes")],
        }


@app.post("/next-day")
def next_day(session_id: str):
    """Advance this session's simulated date and sync, as Plaid's webhook would trigger in production."""
    _, s = get_session(session_id)
    with s.lock:
        result = simulation.advance_day(s.db)
        s.db.commit()
    return result


@app.post("/clear")
def clear(session_id: str | None = None):
    """Forget the conversation and restore the original data."""
    sessions.pop(session_id, None)
    return {"status": "ok"}


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
