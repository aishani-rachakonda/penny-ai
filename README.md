# Penny: a personal finance agent

Penny connects to your bank accounts and Splitwise, detects your subscriptions and bills on its own, plans each
month with you in conversation, and answers the question you actually have, like *"how much can I spend
tonight?"*, by reasoning over the live state of all of those sources together.

It grew out of a real problem: two bank accounts (one for everyday spending, one for salary and rent), bills
that vary month to month, friends who owe money on Splitwise, and a monthly cap that's easy to blow without
noticing. Budgeting apps show you what already happened. Penny is an agent: it uses tools to look things up,
compute, plan, and record, and it tells you how it knows each thing it says.

**Live demo:** https://penny-ai-pyv3lfglpq-ue.a.run.app (Columbia sign-in)

## Try it

The demo user is **Maya**, a fictional NYC grad student, so anyone can try Penny without sharing real data. Her
Chase and Bank of America accounts arrive through a **Plaid sandbox**, her shared expenses through a **Splitwise
sandbox**, and her subscriptions are **detected** from the transactions. At sign-up she told Penny two things:
her rent ($1,250, due between the 1st and the 10th) and her monthly cap ($1,800, rent included).

The app starts on a simulated date, **Friday, Sep 18, 2026**. **Next day** advances the calendar and syncs, as
a Plaid webhook would: pending card purchases post, new ones appear, bills come due, reminders fire. Anything you
change only affects your own session; **Reset** starts over.

### Three sample queries

1. **"I'm going out for drinks tonight. How much should I spend?"** then **"I just spent $30 at Zara."** then
   **"Now how much for drinks, and why did it change?"**
   Penny recommends a number (Maya's usual night out is about $23, but shopping is far over target, so it's much
   less), records the Zara purchase, re-runs the recommendation, and explains the drop with exact before/after
   numbers.
2. **"Who still owes me money?"**
   Penny reads Splitwise and cross-checks the bank: Jordan has owed $48 for 20 days; Priya is marked paid in
   Splitwise but no deposit ever arrived; Alex sent a Zelle that nobody recorded in Splitwise.
3. **"What subscriptions am I paying for?"** then **"Let's plan October."**
   Penny lists recurring payments it detected (nobody entered them), notices Spotify's price went up and the gym
   stopped charging, drafts October's plan from what Maya told it plus what it detected, and saves the changes
   she asks for ("add Hulu, ignore iCloud, cap $1,750").

More: *"Where am I overspending?"*, *"Can I afford a $60 dinner this weekend?"*, *"I paid $90 for dinner with Sam
and Priya. Split it."*, *"Remove the Coffee category and count it as Dining."*, *"Sephora is Personal Care."*,
*"Remember that I'm saving for a trip to Italy in December."*

## Where every piece of data comes from

Penny distinguishes four kinds of information and labels each one in tool results and in the UI:

| Kind | What | Production source | How this project simulates it |
|---|---|---|---|
| **Synced** | Bank accounts, balances, transactions | **Plaid** (`/transactions/sync`, `/accounts/balance/get`), one Item per bank login | `SandboxPlaid` serves `sandbox/data/plaid_items.json`, written in Plaid's schema, through the same method names and response shapes |
| **Synced** | Shared expenses, IOUs, settle-ups | **Splitwise API v3** (`get_expenses`, `get_friends`, `create_expense`) with the user's OAuth token | `SandboxSplitwise` serves `sandbox/data/splitwise.json` in Splitwise's schema; writes are stored per session |
| **Detected** | Subscriptions, bills, paychecks; links between sources | Penny's own algorithms, run after every sync | Identical: the same code runs on sandbox data |
| **User-provided** | Monthly cap, known bills and due windows, category targets, merchant rules, notes | Penny's onboarding and chat | `sandbox/data/user_profile.json` holds Maya's onboarding answers; everything after that comes from chat |

The key design rule: **the sandbox only replaces the network call.** Everything downstream of the API response
(normalization, linking, detection, budgeting, the agent) is the production code path. `ingest/clients.py` is
the single place that decides whether Penny talks to the sandbox or to the real APIs.

### How the simulation stays faithful

- **Plaid's formats and semantics.** Money out is a *positive* amount in Plaid; Penny flips the sign on ingest.
  Transactions carry `transaction_id`, `account_id`, `authorized_date` vs `date`, `merchant_name`,
  `personal_finance_category`, `counterparties` (how Zelle senders appear) and `payment_channel`.
- **Pending → posted.** A card purchase shows up as `pending` on its authorized date; a day or two later Plaid's
  sync returns it in `removed` and returns the posted version in `added` with `pending_transaction_id`. Penny
  updates the row in place, so a split or link made while it was pending survives.
- **Cursors.** `transactions_sync` returns only what changed since the last cursor and pages with `has_more`,
  so Penny's connector has to handle incremental sync exactly as it would against Plaid.
- **Bank-reported balances.** Balances come from the bank (`current`, and `available` net of pending), not
  from adding up transactions, which is how a real aggregator works. Purchases the user tells Penny about are
  shown on top until the bank reports them, then reconciled (matched on account, amount, kind, date and merchant
  or category, so a $30 Zara purchase is never "confirmed" by a $30 phone bill).
- **Splitwise's model.** Every expense lists each member's `paid_share` and `owed_share`; settle-ups are
  expenses with `payment: true`; sync uses `updated_after`, and `deleted_at` removes an expense. When you ask
  Penny to split a bill, it calls `create_expense` and then syncs it back, as it would with the real API.
- **Time.** The sandbox is cut off at each session's simulated date. Advancing a day stands in for Plaid's
  `SYNC_UPDATES_AVAILABLE` webhook and runs the same pipeline.
- **An audit trail.** Every raw provider response is stored in `raw_events`, and each sync's changes in
  `sync_log`, so the normalized data can always be traced back to what the provider said.

### What Penny detects (nobody tells it)

`recurring.py` runs after every sync. It groups bank transactions by merchant (and the user's share of Splitwise
expenses by description), and calls something recurring when it has at least 3 occurrences with a regular rhythm:
one per calendar month in consecutive months, or 12–16 / 6–8 day gaps for biweekly / weekly. For each stream it
works out the typical amount, whether it varies, the most recent **price change** (the latest step between fixed
amounts, so a second increase is caught too), the day-of-month window it lands in, the next expected date, and whether it has **stopped** (overdue by
more than half a cycle). Habits with irregular amounts, like lunch at the same place about once a month, are
not bills and are filtered out.

On Maya's data it finds Spotify (including July's $10.99 → $11.99 increase), Hulu, iCloud, Mint Mobile, rent, her
share of ConEd (variable) and Spectrum from Splitwise, her biweekly paycheck, and a gym membership that stopped
in June. None of these are declared anywhere in the sandbox.

Penny also detects links across sources: transfers between Maya's own accounts (so they're never counted as
income or spending), the bank charge behind each Splitwise expense she paid, and the Zelle behind each
Splitwise settle-up. When they disagree, Penny says so.

### What the user tells Penny

Known facts beat inference. Maya told Penny her rent is $1,250 and due between the 1st and the 10th; Penny's
detector independently found the rent payments and links the two, so the bill shows as *"you told Penny"* with a
detected match. Detected subscriptions she hasn't confirmed still count as expected costs (labelled *detected*)
until she confirms them into the plan or dismisses them.

Plans are **per month**. A month nobody planned inherits the previous month's plan, so budgeting never stops.
Planning happens in chat: `draft_monthly_plan` proposes the next month from the known bills, detected recurring
payments not yet in the plan, price changes, stopped subscriptions and spending history; `update_plan` saves
what the user decides. Categories belong to the user too: there is no preset list (each category is created the
first time the user's own data uses it), the user can add, remove or merge them in chat, and merchant
corrections become rules that apply to future transactions.

Nothing in the interface is hard-coded to the demo user. The welcome text is built from the linked accounts and
the current plan, and the suggested questions are chosen by `suggestions.py` from the current situation (a
category over target, someone who owes money, a bill Penny found that isn't in the plan, the next month to
plan), recomputed after every chat turn and every new day. When a day passes, `notifications.py` rules run
against the new data and any new reminders (a bill coming due, a new recurring charge) appear in that day's message.

## Architecture

```
 sandbox/providers.py            ingest/                                  Penny's database (SQLite)
 ┌──────────────────┐   API      ┌───────────────┐                      ┌──────────────────────────┐
 │ SandboxPlaid     │ responses  │ plaid.py      │ normalize, upsert    │ connections, accounts,   │
 │ SandboxSplitwise │──────────► │ splitwise.py  │────────────────────► │ transactions, people,    │
 └──────────────────┘            │ pipeline.py   │ link, reconcile,     │ shared_expenses, settle- │
  (production: real Plaid and    └───────────────┘ detect (recurring.py)│ ments, recurring_series, │
   Splitwise clients, chosen                                            │ plans, plan_bills, ...   │
   in ingest/clients.py)                                                └────────────┬─────────────┘
                                                                                     │ SQL
 browser ──/chat──► app.py harness ──► Gemini ──tool calls──► tools.py ──► finance.py, planning.py
        ◄── reply + tool cards ◄─────────────────────────────────────────────────────┘
```

**The model never does the math.** Gemini decides which tools to call and explains the results. Every number
comes from deterministic Python, the system prompt forbids stating a figure that didn't come from a tool, and
tools return precomputed totals so there's nothing left to add up.

**Facts are stored; conclusions are computed.** The database holds facts (synced data, the user's plan, rules
and notes) with their source. Who owes whom, budget left and recommendations are computed on demand, so they
can't go stale. The exceptions are records of things that happened: the recommendation log (so Penny can
explain why its advice changed) and the detected recurring series (rebuilt after every sync).

**The adaptive budget.** Bills (known and detected) come off the top of the cap; the rest is the flexible pool.
Whatever is left of it is shared among categories in proportion to each one's remaining room, so overspending
in one category, or a bill higher than expected, shrinks every other category by the same percentage. A
recommendation for "tonight" divides a category's remaining room by how many more occasions the user usually
has before the month ends, counting tonight as one.

**Memory.** Within a session, the conversation is kept and sent with every turn. Penny's notes about the user
(goals, preferences) live in `memory_notes` and are injected into the system prompt every turn; plans, rules
and the recommendation log live in the database.

**Sessions are separate.** At startup Penny onboards Maya through the real pipeline (connect, sync, link,
detect, draft a plan) into a seed database, and writes a copy to `penny.db` that can be opened with any SQLite
browser. Each chat session gets its own in-memory copy, so one visitor's changes never affect another's.

## Tools

| Tool | What it does |
|---|---|
| `get_financial_position` | Bank-reported balances (minus pending and purchases you mentioned), owed to you, you owe, net position, spendable cash |
| `query_transactions` | Search transactions; shows pending status, source, and how each was categorized |
| `summarize_spending` | Spending by category / merchant / week / day, net of splits; small purchases; comparison with last month |
| `get_budget_status` | Cap, spending, every bill (known or detected, paid or due), per-category target / spent / re-balanced room |
| **`recommend_spending`** | *Original.* How much to spend in a category today / this weekend / rest of month, with reasons and what changed since last time |
| **`evaluate_purchase`** | *Original.* What-if for a planned purchase: fits, needs cuts elsewhere, or breaks the cap |
| **`get_shared_balances`** | *Original.* Who owes whom from Splitwise, cross-checked against bank deposits |
| **`get_recurring_payments`** | *Original.* Subscriptions, bills and income Penny detected, with price changes, stopped ones, and plan status |
| **`draft_monthly_plan`** | *Original.* Proposes a month's plan from known bills, detected payments and history; saves nothing |
| `update_plan` | Change a month's cap, bills (add, confirm a detected one, remove, dismiss), targets, and categories |
| `recategorize_merchant` | Move a merchant to another category and remember the rule |
| `update_memory` | Remember or forget a goal or preference |
| `add_transaction` | Record a purchase, income, transfer, or a payment to/from a friend (which also settles up in Splitwise) |
| `split_transaction` | Split a purchase with friends by creating the expense in Splitwise |

Tools validate their arguments and return errors the model can act on, e.g. `'Bob' isn't one of your
Splitwise friends. Friends: Alex, Jordan, Priya, Sam.` or `2026-12-01 is in the future. Today is 2026-09-18`.

## From this project to a production product

| Area | This project | Production |
|---|---|---|
| Bank data | Plaid-format sandbox | Plaid Link for onboarding; `/transactions/sync` on webhooks and a daily schedule. Aggregators like Plaid or MX cover thousands of institutions with one connector |
| Splitwise | Splitwise-format sandbox | Splitwise OAuth 2; poll `get_expenses?updated_after=` (Splitwise has no webhooks) |
| More sources | — | Each new source is one connector writing the same schema (Venmo, credit cards, ...); `source` + `external_id` keep syncs idempotent |
| Database | SQLite, one copy per session | Postgres with `user_id` on every row, row-level security, access tokens encrypted with a KMS |
| Reminders | `notifications.py` rules; new reminders appear in the message for each new day, the way a push notification would | The same rules in a scheduled job (Cloud Scheduler → Cloud Run job) after each sync, stored once per notification and delivered by Web Push, FCM or email, with user preferences and quiet hours |
| Memory | Per-session notes and conversation | Persistent per user, with long conversations summarized |
| Auth | Cloud Run IAP (Columbia accounts) | Real user accounts; each user sees only their own data |
| Time | Simulated date + "Next day" | Real time and provider webhooks |

## Run locally

```bash
gcloud auth application-default login
uv run app.py                   # http://localhost:8000 (also writes penny.db)
uv run pytest                   # pipeline, detection, budget and tool tests
uv run generate_sandbox.py      # regenerate the sandbox fixtures (deterministic)
```

Set `PENNY_MODEL` to use a different LiteLLM model (default `vertex_ai/gemini-3.5-flash`).

## Files

| File | |
|---|---|
| `app.py` | FastAPI app, harness loop (from the course starter), sessions, system prompt, `/chat`, `/state`, `/next-day`, `/clear` |
| `tools.py` | Tool functions, argument validation, and the JSON schemas the model sees |
| `finance.py` | Engine: spending net of splits, budget re-balancing, recommendations, shared balances, position |
| `planning.py` | Monthly plans: known bills, detected bills, carry-forward, suggested targets, drafts, totals |
| `recurring.py` | Recurring payment detection |
| `notifications.py` | Reminder and alert rules |
| `suggestions.py` | Suggested questions chosen from the user's current situation |
| `ingest/` | `clients.py` (sandbox vs production clients), `plaid.py` and `splitwise.py` connectors, `pipeline.py` (sync, reconcile, link, detect) |
| `sandbox/` | `providers.py` (fake Plaid and Splitwise APIs) and `data/` (fixtures in each provider's schema, plus the onboarding profile) |
| `simulation.py` | The simulated clock: advance a day, then sync |
| `db.py` | Schema (every row records its source) and per-session copies |
| `generate_sandbox.py` | Writes Maya's sandbox fixtures |
| `index.html` | Dashboard and chat with tool calls shown |
