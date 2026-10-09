# Penny

**Your money, explained. Your month, planned.**

Penny is a personal finance agent. It connects your bank accounts and Splitwise, keeps track of every bill and
subscription, plans your month with you, and tells you, in the moment, how much you can actually afford.

**Live demo:** https://penny-ai-pyv3lfglpq-ue.a.run.app (Columbia sign-in)

---

## The problem

Life is busy, and money is spread everywhere. A paycheck lands in one account, everyday spending comes out of
another. Rent is due sometime between the 1st and the 10th. The electric bill is different every month. A
roommate covered dinner, a friend still owes you for brunch, Splitwise says someone paid you back but you're
not sure they did. Subscriptions renew quietly. And somewhere in the background there's a big cost coming:
tuition, a trip, a wedding.

Keeping all of that in your head is exhausting, so most of us don't. Small purchases slip through because each
one is too small to matter, until a month of $6 coffees and $3 rides adds up to real money. And at the moment
it counts, standing at the bar or about to check out, nobody tells you what you can actually afford. So you
guess, you overspend, you regret it, and you promise to "be more mindful next month."

Budgeting apps don't fix this. They show you charts of what already happened and leave the thinking to you.

## What Penny does

Penny is the one place that does the thinking for you:

- **Tracks everything in one place.** Every account, bill, subscription, paycheck and shared expense, kept
  current as new transactions arrive.
- **Plans your month with you.** Tell it your rent and your limit; it finds the rest, drafts a plan, and
  adjusts it as you talk.
- **Tells you what you can spend right now.** Based on where you are in the month, what you've already spent,
  and what's still coming, not a fixed number set on the 1st.
- **Watches your subscriptions.** It finds every recurring charge on its own, flags price increases and
  forgotten ones, and helps you decide what to cancel.

Just ask, in your own words.

---

## Try it

The demo runs on **Maya**, a fictional grad student in New York, so anyone can try Penny without sharing real
data. She has a Chase account for everyday spending, a Bank of America account for her paycheck and rent,
roommates and friends on Splitwise, and a $2,375 monthly limit that includes $1,650 rent. The demo starts on
Friday, September 18; **Next day** moves through the month as new transactions arrive.

**Start with these three:**

1. *"I'm going out for drinks tonight. How much can I spend?"* Then press **Next day** twice and ask *"Why is
   today's recommendation lower than yesterday's?"*
2. *"Splitwise says Priya paid me back. Did she actually? How much does she really owe me?"*
3. *"Let's plan October."*

### What you can ask

**Quick answers.** Penny looks it up.
- How much did I spend on the subway in the last 30 days?
- Did I get my Amazon refund?
- When is rent due, and have I paid it?
- What subscriptions am I paying for?
- Who still owes me money?

**Reasoning.** Penny combines several sources to answer the hard questions.
- I'm going out for drinks tonight. How much can I spend?
- Can I afford a $190 concert ticket this weekend without breaking my monthly limit?
- Am I on track to stay under my limit this month? If not, what should I cut?
- Splitwise says Priya paid me back. Did she actually?
- Are there subscriptions I've forgotten about?
- Which small purchases are quietly adding up?
- Why is today's recommendation lower than yesterday's?

**Doing things for you.**
- I paid $90 for dinner with Sam and Priya. Split it, then tell me what I can still spend this week.
- Sephora should count as Personal Care, not Shopping.
- I spent $85 at the vet. That deserves its own category.
- Remember I'm saving for a trip to Italy in December.

**Planning.**
- Let's plan October.
- Spotify raised its price. Is it still worth keeping?

---

## What makes Penny different

**It never does math in its head.** Language models are fluent with numbers they've never checked. In finance
that's unacceptable, so Gemini never computes anything. It decides which tools to call and explains what they
return. Every dollar figure comes from deterministic Python, the tools hand back totals already added up, and
the system prompt forbids stating a number no tool produced.

**It finds what you never told it.** Maya told Penny two things: her rent and her limit. Penny found the rest by
itself: Spotify, Hulu, iCloud, her phone plan, her share of the electric and internet bills, her paycheck
schedule, Spotify's price increase, and a gym membership that stopped charging in June. Detection re-runs on
every sync, so what Penny knows always matches the data. Facts you give it outrank its guesses, and every
bill is labeled *Added by you* or *Found by Penny*.

**Its budget bends instead of breaking.** Overspend on shopping and a normal budget just turns red. Penny
rebalances: bills come off the top of the cap, and whatever is left is shared across categories in proportion
to their remaining room. Overspend in one place and every other category shrinks a little, so the month still
fits. "How much can I spend tonight?" counts tonight as one outing and divides what's left by how many more
outings you usually have before the month ends. That's why the answer changes as the month goes on, and Penny
can explain exactly why it changed.

**It cross-checks your sources.** Splitwise and your bank often disagree. Penny matches every Splitwise
settle-up to a real bank deposit, so it can tell *recorded* from *received*: "Splitwise says Priya paid you,
but the money never arrived." It also pairs transfers between your own accounts, so moving money is never
mistaken for spending, and counts only your share of anything split.

**It plans with you and asks before assuming.** Plans are per month and live in the conversation: Penny drafts
one from what you told it, what it detected and your history, then saves what you decide. Categories are
yours too. There's no preset list, and when a purchase doesn't fit any existing category, Penny suggests a new
one and asks before creating it.

**It's built the way real data arrives.** Bank data comes in Plaid's exact format (cursor-based sync, pending
charges that post a day later, balances reported by the bank) and shared expenses in Splitwise's. The demo
only swaps out the network call; everything after it is the production code path.

---

## How it works

```
 Plaid (Chase, BoA) ─┐                 ┌─ link transfers, splits, settle-ups
                     ├─► ingestion ────┤─ detect recurring payments          ──► Penny's database
 Splitwise ──────────┘   pipeline      └─ reconcile what you reported              (facts + their source)
                                                                                        │
 you ──► chat ──► Gemini ──► tools (14) ──► finance engine: budget, recommendations ◄───┘
          ◄── answer, with every tool call shown ◄──┘
```

Penny is a FastAPI app with a tool-calling loop over Gemini 3.5 Flash (via LiteLLM on Vertex AI), a SQLite
database per chat session, and a single-page dashboard. It's deployed on Google Cloud Run.

<details>
<summary><b>Where every piece of data comes from</b></summary>

| Kind | What | Production source | In this project |
|---|---|---|---|
| **Synced** | Accounts, balances, transactions | Plaid `/transactions/sync`, `/accounts/balance/get` | `SandboxPlaid` serves fixtures in Plaid's schema through the same method names and responses |
| **Synced** | Shared expenses, IOUs, settle-ups | Splitwise API v3 (`get_expenses`, `create_expense`) | `SandboxSplitwise` serves fixtures in Splitwise's schema; writes are kept per session |
| **Detected** | Subscriptions, bills, paychecks; links between sources | Penny's algorithms, after every sync | The same code |
| **User-provided** | Monthly limit, known bills and due windows, targets, category rules, notes | Onboarding and chat | `sandbox/data/user_profile.json`, then chat |

How the simulation stays faithful:
- Plaid reports money out as a *positive* amount; Penny flips the sign on ingest.
- Card purchases appear as `pending`, then Plaid's sync removes them and adds the posted version with
  `pending_transaction_id`. Penny updates the row in place, so splits made while pending survive.
- Sync is incremental: only changes since the last cursor, paged with `has_more`.
- Balances come from the bank (posted, and available after pending), not from summing transactions. Purchases
  you tell Penny about are reconciled with the bank's copy when it arrives, matching on account, amount,
  date and merchant or category.
- Splitwise expenses carry each member's `paid_share` and `owed_share`; settle-ups are `payment: true`.
  Splitting a bill calls `create_expense` and syncs it back.
- The sandbox is cut off at each session's demo date. **Next day** stands in for Plaid's
  `SYNC_UPDATES_AVAILABLE` webhook and runs the same pipeline.
- Every raw provider response is stored in `raw_events`, and each sync's changes in `sync_log`.
</details>

<details>
<summary><b>How recurring payments are detected</b></summary>

After every sync, `recurring.py` groups bank transactions by merchant (and the user's share of Splitwise expenses
by description). A stream is recurring when it has at least 3 occurrences with a regular rhythm: one per
calendar month in consecutive months, or 12–16 / 6–8 day gaps for biweekly / weekly. For each one it works out
the typical amount, whether it varies, the most recent price change (the latest step between fixed amounts, so
a second increase is caught too), the day-of-month window, the next expected date, and whether it has stopped
(overdue by more than half a cycle). Irregular habits, like lunch at the same place about once a month, are
filtered out.
</details>

<details>
<summary><b>Tools</b></summary>

| Tool | What it does |
|---|---|
| `get_financial_position` | Available balances, owed to you, you owe, net position, spendable cash |
| `query_transactions` | Search transactions; shows pending status, source, and how each was categorized |
| `summarize_spending` | Spending by category, merchant, week or day, net of splits; small purchases; vs last month |
| `get_budget_status` | Limit, spending, every bill, per-category target, spent and rebalanced room |
| **`recommend_spending`** | How much to spend today, this weekend or this month, with reasons and what changed since last time |
| **`evaluate_purchase`** | What-if for a planned purchase: fits, needs cuts elsewhere, or breaks the limit |
| **`get_shared_balances`** | Who owes whom, cross-checked against bank deposits |
| **`get_recurring_payments`** | Subscriptions, bills and income Penny detected |
| **`draft_monthly_plan`** | Proposes a month's plan; saves nothing |
| `update_plan` | Change the limit, bills, targets and categories |
| `recategorize_merchant` | Move a merchant to another category and remember it |
| `update_memory` | Remember or forget a goal or preference |
| `add_transaction` | Record a purchase, income, transfer or payment to a friend; can create a category once you agree |
| `split_transaction` | Split a purchase with friends through Splitwise |

Tools return errors the model can act on, e.g. *"'Bob' isn't one of your Splitwise friends. Friends: Alex,
Jordan, Priya, Sam."*
</details>

<details>
<summary><b>From demo to product</b></summary>

| Area | This project | Production |
|---|---|---|
| Bank data | Plaid-format sandbox | Plaid Link and `/transactions/sync` on webhooks; one aggregator connector covers thousands of banks |
| Splitwise | Splitwise-format sandbox | OAuth 2; poll `get_expenses?updated_after=` |
| More sources | — | Each new source is one connector writing the same schema |
| Database | SQLite per session | Postgres, `user_id` on every row, encrypted tokens |
| Reminders | `notifications.py` rules, shown in each new day's message | The same rules in a scheduled job, delivered by push or email |
| Memory | Per session | Persistent per user |
| Time | Demo date and **Next day** | Real time and provider webhooks |
</details>

<details>
<summary><b>Run it locally</b></summary>

```bash
gcloud auth application-default login
uv run app.py                 # http://localhost:8000 (also writes penny.db, an inspectable copy of the data)
uv run pytest                 # pipeline, detection, budget and tool tests
uv run generate_sandbox.py    # regenerate the demo data (deterministic)
```

| File | |
|---|---|
| `app.py` | FastAPI app, tool-calling loop, sessions, system prompt |
| `tools.py` | Tools and the schemas the model sees |
| `finance.py` | Spending, rebalancing, recommendations, shared balances |
| `planning.py` | Monthly plans, known and detected bills, drafts |
| `recurring.py` | Recurring payment detection |
| `suggestions.py` | Suggested questions from the current situation |
| `notifications.py` | Reminder rules |
| `ingest/` | Plaid and Splitwise connectors, sync pipeline |
| `sandbox/` | Fake Plaid and Splitwise APIs and their data |
| `simulation.py` | The demo clock |
| `db.py` | Schema and per-session copies |
| `index.html` | Dashboard and chat |
</details>
