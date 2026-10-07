"""Which provider clients Penny talks to.

PENNY_DATA_MODE=sandbox (the default, and the only mode in this project) returns the
fake clients in sandbox/providers.py. They expose the same methods as the real ones:

    production Plaid       plaid-python: PlaidApi.transactions_sync(TransactionsSyncRequest(access_token, cursor))
                           and PlaidApi.accounts_balance_get, with access tokens from Plaid Link's
                           /item/public_token/exchange, kept encrypted per user.
    production Splitwise   REST https://secure.splitwise.com/api/v3.0/ with the user's OAuth 2 token:
                           GET get_current_user, get_friends, get_groups, get_expenses?updated_after=...,
                           POST create_expense.

Swapping in the real clients changes nothing downstream: ingest/plaid.py and
ingest/splitwise.py only see the response dictionaries.
"""

import os

import db

MODE = os.environ.get("PENNY_DATA_MODE", "sandbox")


def plaid(conn):
    if MODE != "sandbox":
        raise NotImplementedError("Production Plaid client not configured; see the docstring above.")
    from sandbox.providers import SandboxPlaid
    return SandboxPlaid(clock=lambda: db.today(conn))


def splitwise(conn):
    if MODE != "sandbox":
        raise NotImplementedError("Production Splitwise client not configured; see the docstring above.")
    from sandbox.providers import SandboxSplitwise
    return SandboxSplitwise(clock=lambda: db.today(conn), conn=conn)
