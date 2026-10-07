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
from tools import TOOLS, run_tool

# --- Config ---

MODEL = os.environ.get("PENNY_MODEL", "vertex_ai/gemini-3.5-flash-lite")
MAX_TOOL_ROUNDS = 8

SYSTEM_PROMPT = """You are Penny, a personal finance agent. You help {user} keep spending under a monthly cap, \
track who owes whom from shared expenses, and decide how much they can spend right now.

Today is {weekday}, {today}. This is a simulated date: never refer to data after it.

What you can see: {user}'s bank accounts ({accounts}), their shared expenses with {people} (from Splitwise), \
and their monthly budget (a ${cap:,.0f} cap that includes rent and bills).

Rules:
- Never calculate money in your head or guess a number. Every dollar figure you state must come from a tool \
result in this conversation. If a tool can't answer it, say so.
- Data changes during the conversation (new transactions, the date moving forward), so call tools again rather \
than reusing numbers from earlier turns.
- "Net position" counts money people owe {user}; "spendable cash" does not. Explain the difference when it matters.
- When asked how much to spend, use recommend_spending and explain the main reasons in plain words \
(e.g. "Shopping is $109 over, so I've pulled back the rest of your budget").
- When {user} says they spent, received or transferred money, record it with add_transaction. If they say they \
paid for a group, record it and then split it with split_transaction. Only ask a question if the account, \
amount or people are genuinely unclear.
- If a tool returns an error, fix the arguments and try again, or tell {user} what you need.
- Splitwise and the bank can disagree. When get_shared_balances lists mismatches, point them out.
- Be concise and warm: lead with the answer, then 1-3 short reasons. Use $ amounts with cents only when useful."""


def system_prompt(conn) -> str:
    today = db.today(conn)
    accounts = ", ".join(f"{r[0]} = {r[1]} ({r[2]})" for r in conn.execute("SELECT account_id, name, role FROM accounts"))
    people = ", ".join(sorted({r[0] for r in conn.execute("SELECT person FROM shared_shares")} - {"me"}))
    cap = conn.execute("SELECT value FROM budget WHERE key = 'monthly_cap_cents'").fetchone()[0] / 100
    return SYSTEM_PROMPT.format(user=db.ME, weekday=today.strftime("%A"), today=today.strftime("%B %d, %Y"),
                                accounts=accounts, people=people, cap=cap)


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
            return reply.content, tool_calls

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

# Seed database built once from the statement files at startup.
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
    """Everything the dashboard panel shows, computed fresh from this session's data."""
    session_id, s = get_session(session_id)
    with s.lock:
        conn = s.db
        meta = dict(conn.execute("SELECT key, value FROM meta"))
        return {
            "session_id": session_id,
            "today": meta["today"], "start": meta["start"], "last_day": meta["last_day"],
            "user": db.ME,
            "position": finance.financial_position(conn),
            "budget": finance.budget_status(conn),
            "shared": finance.shared_balances(conn),
        }


@app.post("/next-day")
def next_day(session_id: str):
    """Move this session's simulated date forward and reveal that day's transactions."""
    _, s = get_session(session_id)
    with s.lock:
        result = finance.advance_day(s.db)
        s.db.commit()
    return result


@app.post("/clear")
def clear(session_id: str | None = None):
    """Forget the conversation and restore the original data."""
    sessions.pop(session_id, None)
    return {"status": "ok"}


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
