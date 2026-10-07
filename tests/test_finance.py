"""Engine tests: the numbers Penny reports must be right before any model touches them."""

import csv
import json
from datetime import date
from pathlib import Path

import pytest

import db
import finance
import tools

SEED = db.build_seed()


@pytest.fixture
def conn():
    return db.session_copy(SEED)


def call(conn, name, **args):
    return json.loads(tools.run_tool(conn, name, args))


def test_balances_match_statement_running_balances(conn):
    """Computed balances equal the running balance printed on the last statement row on or before today."""
    today = db.today(conn).isoformat()
    chase = [r for r in csv.DictReader(open(db.STATEMENTS / "chase_spending.csv"))
             if db.iso(r["Posting Date"]) <= today]
    boa_lines = list(csv.reader(open(db.STATEMENTS / "boa_salary.csv")))
    boa = [r for r in boa_lines[5:] if r and r[2] and db.iso(r[0]) <= today]
    balances = {b["account"]: b["balance"] for b in finance.balances(conn, db.today(conn))}
    assert balances["chase"] == float(chase[-1]["Balance"])
    assert balances["boa"] == float(boa[-1][3])


def test_transfers_are_linked_and_not_spending(conn):
    unlinked = conn.execute("SELECT COUNT(*) FROM transactions WHERE kind = 'transfer' AND transfer_id IS NULL").fetchone()[0]
    assert unlinked == 0
    spent = finance.spending_by_category(conn, date(2026, 3, 1), db.today(conn))
    assert "Transfer" not in spent and "Income" not in spent and "Payments" not in spent


def test_budget_targets_plus_bills_equal_cap(conn):
    cap = conn.execute("SELECT value FROM budget WHERE key = 'monthly_cap_cents'").fetchone()[0]
    bills = conn.execute("SELECT SUM(amount_cents) FROM obligations").fetchone()[0]
    targets = conn.execute("SELECT SUM(target_cents) FROM budget_categories").fetchone()[0]
    assert bills + targets == cap


def test_split_counts_only_my_share(conn):
    before = finance.spending_by_category(conn, date(2026, 9, 1), db.today(conn)).get("Dining", 0)
    txn = call(conn, "add_transaction", account="chase", amount=90, direction="spent",
               description="Dinner at Tomo", category="Dining")
    call(conn, "split_transaction", transaction_id=txn["transaction_id"], people=["Sam", "Priya"])
    after = finance.spending_by_category(conn, date(2026, 9, 1), db.today(conn))["Dining"]
    assert after - before == 3000
    sam = call(conn, "get_shared_balances", person="Sam")["people"][0]
    assert sam["direction"] == "owes you"


def test_overspending_lowers_other_recommendations(conn):
    first = call(conn, "recommend_spending", category="Nightlife", horizon="today")
    call(conn, "add_transaction", account="chase", amount=30, direction="spent", description="Zara", category="Shopping")
    second = call(conn, "recommend_spending", category="Nightlife", horizon="today")
    assert second["recommended"] < first["recommended"]
    changes = " ".join(second["change_since_last_recommendation"]["what_changed"])
    assert "Shopping" in changes


def test_recommendation_never_exceeds_flexible_money_left(conn):
    status = finance.budget_status(conn)
    for c in status["categories"]:
        r = finance.recommend(conn, c["category"], "rest_of_month", log=False)
        assert r["recommended"] <= status["flexible_left"] + 0.01


def test_mismatches_between_splitwise_and_bank(conn):
    m = {(x["person"], x["type"]) for x in finance.shared_balances(conn)["mismatches"]}
    assert ("Priya", "recorded_but_not_received") in m
    assert ("Alex", "received_but_not_recorded") in m


def test_sessions_are_isolated():
    a, b = db.session_copy(SEED), db.session_copy(SEED)
    call(a, "add_transaction", account="chase", amount=500, direction="spent", description="Laptop", category="Shopping")
    assert finance.balances(a, db.today(a))[0]["balance"] != finance.balances(b, db.today(b))[0]["balance"]


def test_tools_return_actionable_errors(conn):
    assert "Valid categories" in call(conn, "recommend_spending", category="drinks")["error"]
    assert "Available accounts" in call(conn, "add_transaction", account="savings", amount=5,
                                        direction="spent", description="x")["error"]
    assert "future" in call(conn, "query_transactions", start_date="2026-12-01")["error"]
    assert "Unknown tool" in call(conn, "not_a_tool")["error"]


def test_hidden_future_until_next_day(conn):
    today = db.today(conn)
    n_before = call(conn, "query_transactions", start_date="2026-09-01")["count"]
    while finance.advance_day(conn)["new_transactions"] == [] and db.today(conn).isoformat() < "2026-09-29":
        pass
    assert db.today(conn) > today
    assert call(conn, "query_transactions", start_date="2026-09-01")["count"] > n_before
