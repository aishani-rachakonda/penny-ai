"""Tests for the data pipeline and the engine: the numbers must be right before any model touches them."""

import json
from datetime import date

import pytest

import db
import finance
import planning
import recurring
import simulation
import tools
from ingest import pipeline
from sandbox.providers import SandboxPlaid

SEED = db.build_seed()


@pytest.fixture
def conn():
    return db.session_copy(SEED)


def call(conn, name, **args):
    return json.loads(tools.run_tool(conn, name, args))


# --- Ingestion ---------------------------------------------------------------------


def test_plaid_sign_convention_and_pending(conn):
    """Plaid reports money out as positive; Penny stores it negative. Friday's card purchases are still pending."""
    rent = conn.execute("SELECT amount_cents FROM transactions WHERE merchant LIKE 'Harlem Heights%' LIMIT 1").fetchone()
    assert rent[0] == -220000
    assert conn.execute("SELECT COUNT(*) FROM transactions WHERE pending = 1 AND source = 'plaid'").fetchone()[0] > 0


def test_balances_match_what_the_bank_reports(conn):
    plaid = SandboxPlaid(clock=lambda: db.today(conn))
    for (token,) in conn.execute("SELECT access_token FROM connections WHERE provider = 'plaid'").fetchall():
        for a in plaid.accounts_balance_get(token)["accounts"]:
            mine = conn.execute("SELECT balance_current_cents, balance_available_cents FROM accounts WHERE external_id = ?",
                                (a["account_id"],)).fetchone()
            assert mine == (round(a["balances"]["current"] * 100), round(a["balances"]["available"] * 100))


def test_pending_becomes_posted_in_place(conn):
    pending = conn.execute("SELECT txn_id FROM transactions WHERE pending = 1 AND source = 'plaid'").fetchall()
    for _ in range(4):
        simulation.advance_day(conn)
    for (txn_id,) in pending:
        row = conn.execute("SELECT pending, posted_date FROM transactions WHERE txn_id = ?", (txn_id,)).fetchone()
        assert row is not None and row[0] == 0 and row[1]  # same row, now posted


def test_sync_is_idempotent(conn):
    before = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    report = pipeline.sync_all(conn)
    assert all(s["added"] == 0 for s in report["sources"])
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == before


def test_transfers_are_linked_and_not_spending(conn):
    assert conn.execute("SELECT COUNT(*) FROM transactions WHERE kind = 'transfer' AND transfer_id IS NULL").fetchone()[0] == 0
    spent = finance.spending_by_category(conn, date(2026, 3, 1), db.today(conn))
    assert not {"Transfer", "Income", "Payments"} & set(spent)


def test_mismatches_between_splitwise_and_bank(conn):
    m = {(x["person"], x["type"]) for x in finance.shared_balances(conn)["mismatches"]}
    assert ("Priya", "recorded_but_not_received") in m
    assert ("Alex", "received_but_not_recorded") in m


# --- Detection ---------------------------------------------------------------------


def test_detects_subscriptions_nobody_entered(conn):
    found = {s["label"]: s for s in recurring.series(conn)}
    assert {"Spotify", "Hulu", "Mint Mobile", "iCloud"} <= set(found)
    assert found["Spotify"]["price_change"] == {"from": 10.99, "to": 11.99, "on": "2026-07-07"}
    assert found["Blink Fitness"]["status"] == "stopped"
    assert found["Columbia University"]["cadence"] == "biweekly" and found["Columbia University"]["direction"] == "in"
    assert found["ConEd electricity (your share)"]["variable"] == 1
    assert not {"MTA", "Uber", "Sweetgreen"} & set(found)  # habits aren't bills


def test_known_bill_matches_detected_rent(conn):
    bills = {b["name"]: b for b in planning.bills_for_month(conn, db.today(conn))}
    assert bills["Rent"]["source"] == "you told Penny" and bills["Rent"]["detected_match"] == "Harlem Heights Management"
    assert bills["Hulu"]["source"] == "detected by Penny" and not bills["Hulu"]["in_plan"]


# --- Budget and tools ----------------------------------------------------------------


def test_split_counts_only_my_share_and_reaches_splitwise(conn):
    before = finance.spending_by_category(conn, date(2026, 9, 1), db.today(conn)).get("Dining", 0)
    txn = call(conn, "add_transaction", account="chase", amount=90, direction="spent", description="Dinner at Tomo",
               category="Dining")
    call(conn, "split_transaction", transaction_id=txn["transaction_id"], people=["Sam", "Priya"])
    assert finance.spending_by_category(conn, date(2026, 9, 1), db.today(conn))["Dining"] - before == 3000
    assert conn.execute("SELECT COUNT(*) FROM sandbox_splitwise_writes").fetchone()[0] == 1
    assert call(conn, "get_shared_balances", person="Sam")["people"][0]["direction"] == "owes you"


def test_overspending_lowers_other_recommendations(conn):
    first = call(conn, "recommend_spending", category="Groceries", horizon="rest_of_month")
    call(conn, "add_transaction", account="chase", amount=30, direction="spent", description="Zara", category="Shopping")
    second = call(conn, "recommend_spending", category="Groceries", horizon="rest_of_month")
    assert second["recommended"] < first["recommended"]
    assert "Shopping" in " ".join(second["change_since_last_recommendation"]["what_changed"])


def test_manual_entry_not_confused_with_same_amount_bill(conn):
    call(conn, "add_transaction", account="chase", amount=30, direction="spent", description="Zara", category="Shopping")
    for _ in range(4):  # Mint Mobile ($30) posts in this window
        simulation.advance_day(conn)
    assert conn.execute("SELECT COUNT(*) FROM transactions WHERE merchant = 'Zara'").fetchone()[0] == 1


def test_plan_changes_in_chat(conn):
    call(conn, "update_plan", categories_to_remove=[{"category": "Coffee", "move_spending_to": "Dining"}])
    assert "Coffee" not in {c["category"] for c in finance.budget_status(conn)["categories"]}
    call(conn, "update_plan", month="2026-10", bills_to_set=[{"name": "Hulu"}])
    assert conn.execute("SELECT source FROM plan_bills WHERE month = '2026-10' AND name = 'Hulu'").fetchone()[0] == "detected"


def test_next_month_carries_plan_forward(conn):
    while db.today(conn) < date(2026, 10, 1):
        simulation.advance_day(conn)
    status = finance.budget_status(conn)
    assert status["month"] == "October 2026" and status["plan"].startswith("carried")
    assert any(b["name"] == "Rent" for b in status["bills"])


def test_sessions_are_isolated():
    a, b = db.session_copy(SEED), db.session_copy(SEED)
    call(a, "add_transaction", account="chase", amount=500, direction="spent", description="Laptop", category="Shopping")
    assert finance.balances(a)[0]["balance"] != finance.balances(b)[0]["balance"]


def test_tools_return_actionable_errors(conn):
    assert "Categories:" in call(conn, "recommend_spending", category="drinks")["error"]
    assert "Accounts:" in call(conn, "add_transaction", account="savings", amount=5, direction="spent",
                                description="x")["error"]
    assert "future" in call(conn, "query_transactions", start_date="2026-12-01")["error"]
    assert "Splitwise friends" in call(conn, "get_shared_balances", person="Bob")["error"]
    assert "Unknown tool" in call(conn, "not_a_tool")["error"]


def test_unfamiliar_merchant_needs_a_category(conn):
    err = call(conn, "add_transaction", account="chase", amount=30, direction="spent", description="Zara")
    assert "category" in err["error"] and "Shopping" in err["error"]
    assert not conn.execute("SELECT 1 FROM transactions WHERE merchant = 'Zara'").fetchone()
    ok = call(conn, "add_transaction", account="chase", amount=12, direction="spent", description="Trader Joe's")
    assert "Groceries" in ok["recorded"]  # a familiar merchant still works without one


def test_familiar_merchant_keeps_its_usual_category(conn):
    r = call(conn, "add_transaction", account="chase", amount=6, direction="spent", description="Blue Bottle",
             category="Dining")
    assert "Blue Bottle Coffee" in r["recorded"] and "Coffee" in r["recorded"] and "note" in r


def test_new_category_only_when_asked_for(conn):
    err = call(conn, "add_transaction", account="chase", amount=85, direction="spent", description="Riverside Vet",
               category="Pets")
    assert "new_category" in err["error"] and "Pets" not in [r[0] for r in conn.execute("SELECT name FROM categories")]
    r = call(conn, "add_transaction", account="chase", amount=85, direction="spent", description="Riverside Vet",
             new_category="Pets")
    assert r["new_category_created"] == "Pets" and "budget_note" in r
    again = call(conn, "add_transaction", account="chase", amount=20, direction="spent", description="Riverside Vet")
    assert "category Pets" in again["recorded"]  # remembered for this merchant
    dupe = call(conn, "add_transaction", account="chase", amount=5, direction="spent", description="Cafe X",
                new_category="Coffe")
    assert "very close" in dupe["error"]


def test_price_change_tag_follows_the_data(conn):
    spotify = next(b for b in planning.bills_for_month(conn, db.today(conn)) if b["name"] == "Spotify")
    assert spotify["price_change"] == {"from": 10.99, "to": 11.99, "on": "2026-07-07"}
    # Spotify raises its price again: the next sync's detection picks up the new step.
    conn.execute("UPDATE transactions SET amount_cents = -1299 WHERE merchant = 'Spotify' AND date = '2026-09-07'")
    recurring.detect(conn)
    spotify = next(b for b in planning.bills_for_month(conn, db.today(conn)) if b["name"] == "Spotify")
    assert spotify["price_change"] == {"from": 11.99, "to": 12.99, "on": "2026-09-07"}
