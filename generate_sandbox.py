"""Generate the provider sandbox for Maya, Penny's fictional demo user.

In production Penny gets data from three kinds of source. This script writes a
fixture for each, in the format that source really uses:

    sandbox/data/plaid_items.json   What Plaid knows about Maya's two bank connections
                                    (Chase and Bank of America): accounts and transactions
                                    in Plaid's schema, with authorized vs posted dates so
                                    purchases show up as pending first, then post.
    sandbox/data/splitwise.json     What the Splitwise API knows: Maya, her friends, groups,
                                    and every expense and payment in Splitwise's schema.
    sandbox/data/user_profile.json  What Maya told Penny when she signed up: her monthly
                                    cap and the bills she knows about (rent and its due window).

Nothing here is read directly by the agent. sandbox/providers.py serves these
fixtures through fake Plaid and Splitwise clients that only reveal what would
exist on the session's simulated date, and Penny's ingestion pipeline pulls
from those clients exactly as it would pull from the real APIs.

Recurring payments are deliberately NOT listed anywhere: Penny has to detect
them from the transactions.

The script is deterministic (fixed seed).

    uv run generate_sandbox.py
"""

import json
import random
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

START = date(2026, 3, 1)
END = date(2026, 10, 6)
OUT = Path(__file__).parent / "sandbox" / "data"

rng = random.Random(42)


def rid(n: int = 37) -> str:
    """A Plaid-style opaque id."""
    return "".join(rng.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789") for _ in range(n))


def days(start: date, end: date):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


def money(lo: float, hi: float) -> float:
    return round(rng.uniform(lo, hi), 2)


def next_business_day(d: date) -> date:
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


# --- Plaid: two Items (bank connections), one checking account each ------------

CHASE_ACCT, BOA_ACCT = rid(), rid()
items = {
    "chase": {"item_id": rid(), "institution_id": "ins_56", "institution_name": "Chase",
              "access_token": "access-sandbox-chase-" + rid(8).lower(),
              "accounts": [{"account_id": CHASE_ACCT, "name": "TOTAL CHECKING", "official_name": "Chase Total Checking",
                            "mask": "4417", "type": "depository", "subtype": "checking",
                            "_sandbox_opening_balance": 640.00}],
              "transactions": []},
    "boa": {"item_id": rid(), "institution_id": "ins_127989", "institution_name": "Bank of America",
            "access_token": "access-sandbox-boa-" + rid(8).lower(),
            "accounts": [{"account_id": BOA_ACCT, "name": "Adv Plus Banking", "official_name": "Bank of America Advantage Plus Banking",
                          "mask": "8820", "type": "depository", "subtype": "checking",
                          "_sandbox_opening_balance": 2850.00}],
            "transactions": []},
}
ACCOUNT = {"chase": CHASE_ACCT, "boa": BOA_ACCT}


PFC_PRIMARIES = ["FOOD_AND_DRINK", "GENERAL_MERCHANDISE", "RENT_AND_UTILITIES", "TRANSFER_IN", "TRANSFER_OUT",
                 "PERSONAL_CARE", "GENERAL_SERVICES", "ENTERTAINMENT", "TRANSPORTATION", "INCOME"]


def txn(bank: str, authorized: date, amount_out: float, name: str, merchant: str | None, pfc: str,
        channel: str = "in store", counterparty: tuple[str, str] | None = None, card: bool = True):
    """Add one Plaid transaction. Plaid's sign convention: positive amount = money OUT.

    Card purchases authorize today and post 1-3 days later (next business day),
    so they appear as pending first. ACH and Zelle post the same day.
    """
    posted = next_business_day(authorized + timedelta(days=1)) if card else authorized
    primary = next(p for p in PFC_PRIMARIES if pfc.startswith(p))
    items[bank]["transactions"].append({
        "transaction_id": rid(),
        "account_id": ACCOUNT[bank],
        "amount": round(amount_out, 2),
        "iso_currency_code": "USD",
        "authorized_date": authorized.isoformat(),
        "date": posted.isoformat(),
        "name": name,
        "merchant_name": merchant,
        "payment_channel": channel,
        "personal_finance_category": {"primary": primary, "detailed": pfc,
                                      "confidence_level": "VERY_HIGH" if merchant else "MEDIUM"},
        "counterparties": [{"name": counterparty[0], "type": counterparty[1]}] if counterparty else [],
        "_sandbox_has_pending_phase": posted > authorized,
    })


def buy(d, name, merchant, amount, pfc, channel="in store", bank="chase"):
    txn(bank, d, amount, name, merchant, pfc, channel)


# --- Income, rent, transfers ------------------------------------------------------

d = date(2026, 3, 6)  # biweekly Friday paychecks
while d <= END:
    txn("boa", d, -1385.40, "COLUMBIA UNIV DES:PAYROLL ID:10309516 PPD", "Columbia University", "INCOME_WAGES",
        "other", card=False)
    d += timedelta(days=14)

for month in range(3, 11):
    # Rent is due between the 1st and the 10th; Maya pays on a different day each month.
    rent_day = date(2026, month, rng.choice([1, 2, 3, 5, 6, 8]))
    if rent_day <= END:
        txn("boa", rent_day, 1250.00, "HARLEM HEIGHTS MGMT DES:WEB PMTS ID:RENT4B", "Harlem Heights Management",
            "RENT_AND_UTILITIES_RENT", "online", card=False)
    t = date(2026, month, 3)
    if t <= END:
        conf = str(rng.randint(10**7, 10**8))
        txn("boa", t, 700.00, f"Online Transfer to CHK ...4417 Conf# T{conf}", None,
            "TRANSFER_OUT_ACCOUNT_TRANSFER", "online", card=False)
        txn("chase", t, -700.00, "Online Transfer From BOA Checking ...8820", None,
            "TRANSFER_IN_ACCOUNT_TRANSFER", "online", card=False)

# --- Subscriptions (never declared anywhere: Penny must detect them) --------------

for month in range(3, 11):
    def on(day):
        x = date(2026, month, day)
        return x if x <= END else None
    if (x := on(7)):  # Spotify raised its price in July
        buy(x, "SPOTIFY USA 877-7781161 NY", "Spotify", 10.99 if month < 7 else 11.99,
            "ENTERTAINMENT_MUSIC_AND_AUDIO", "online")
    if (x := on(21)):
        buy(x, "MINT MOBILE 888-588-6468 CA", "Mint Mobile", 30.00, "RENT_AND_UTILITIES_TELEPHONE", "online")
    if (x := on(12)):  # a streaming subscription Maya forgot to tell Penny about
        buy(x, "HULU 877-8244858 CA", "Hulu", 9.99, "ENTERTAINMENT_TV_AND_MOVIES", "online")
    if (x := on(26)):
        buy(x, "APPLE.COM/BILL 866-712-7753 CA", "iCloud", 2.99, "GENERAL_SERVICES_OTHER_GENERAL_SERVICES", "online")
    if month <= 6 and (x := on(2)):  # gym membership, cancelled after June
        buy(x, "BLINK FITNESS HARLEM NY", "Blink Fitness", 29.99, "PERSONAL_CARE_GYMS_AND_FITNESS_CENTERS")

# --- Day-to-day spending on the Chase card ---------------------------------------

GROCERY = [("TRADER JOE S #558 NEW YORK NY", "Trader Joe's"), ("KEY FOOD #1123 NEW YORK NY", "Key Food"),
           ("WHOLE FOODS MKT HARLEM NY", "Whole Foods Market")]
LUNCH = [("SWEETGREEN COLUMBIA NEW YORK NY", "Sweetgreen"), ("CHIPOTLE 0921 NEW YORK NY", "Chipotle"),
         ("JUNZI KITCHEN NEW YORK NY", "Junzi Kitchen")]
COFFEE = [("BLUE BOTTLE COFFEE NEW YORK NY", "Blue Bottle Coffee"), ("DUNKIN #3392 NEW YORK NY", "Dunkin'"),
          ("SQ *HUNGARIAN PASTRY SHOP NY", "Hungarian Pastry Shop")]
DINNER = [("THE GRAND TIER NEW YORK NY", "The Grand Tier"), ("SQ *TOMO SUSHI NEW YORK NY", "Tomo Sushi"),
          ("PATIALA GRILL NEW YORK NY", "Patiala Grill")]
BARS = [("TST* AMSTERDAM TAVERN NEW YORK NY", "Amsterdam Tavern"), ("1020 BAR NEW YORK NY", "1020 Bar"),
        ("SQ *THE HEIGHTS BAR NY", "The Heights Bar")]
DELIVERY = [("UBER EATS SAN FRANCISCO CA", "Uber Eats"), ("DOORDASH*THAI MARKET SAN FRANCISCO CA", "DoorDash")]
SHOP = [("AMAZON MKTPL*5O7T232 AMZN.COM/BILL WA", "Amazon", "GENERAL_MERCHANDISE_ONLINE_MARKETPLACES", "online"),
        ("UNIQLO 5TH AVE NEW YORK NY", "Uniqlo", "GENERAL_MERCHANDISE_CLOTHING_AND_ACCESSORIES", "in store"),
        ("H&M 0098 NEW YORK NY", "H&M", "GENERAL_MERCHANDISE_CLOTHING_AND_ACCESSORIES", "in store"),
        ("TARGET 00031336 NEW YORK NY", "Target", "GENERAL_MERCHANDISE_SUPERSTORES", "in store")]

for d in days(START, END):
    wd = d.weekday()
    weekend = wd >= 5
    out_night = wd in (4, 5)
    if not weekend:
        for _ in range(rng.choice([0, 1, 1, 2])):
            buy(d, "MTA*NYCT PAYGO NEW YORK NY", "MTA", 2.90, "TRANSPORTATION_PUBLIC_TRANSIT")
    if out_night and rng.random() < 0.12:
        buy(d, "UBER *TRIP HELP.UBER.COM CA", "Uber", money(11, 24), "TRANSPORTATION_TAXIS_AND_RIDE_SHARES", "online")
    if rng.random() < (0.22 if not weekend else 0.12):
        n, m = rng.choice(COFFEE); buy(d, n, m, money(4.25, 7.50), "FOOD_AND_DRINK_COFFEE")
    if not weekend and rng.random() < 0.07:
        n, m = rng.choice(LUNCH); buy(d, n, m, money(11.5, 16.5), "FOOD_AND_DRINK_FAST_FOOD")
    if rng.random() < 0.13:
        n, m = rng.choice(GROCERY); buy(d, n, m, money(14, 34), "FOOD_AND_DRINK_GROCERIES")
    if out_night and rng.random() < 0.15:
        n, m = rng.choice(DINNER); buy(d, n, m, money(22, 46), "FOOD_AND_DRINK_RESTAURANT")
    if out_night and rng.random() < 0.25:
        n, m = rng.choice(BARS); buy(d, n, m, money(14, 38), "FOOD_AND_DRINK_BEER_WINE_AND_LIQUOR")
    if rng.random() < 0.03:
        n, m = rng.choice(DELIVERY); buy(d, n, m, money(17, 31), "FOOD_AND_DRINK_RESTAURANT", "online")
    if rng.random() < 0.035:
        n, m, pfc, ch = rng.choice(SHOP); buy(d, n, m, money(12, 48), pfc, ch)
    if d.day == 9 and d.month % 2:
        buy(d, "SQ *HARLEM THREADING NEW YORK NY", "Harlem Threading", 22.00, "PERSONAL_CARE_HAIR_AND_BEAUTY")
    if d.day == 24 and d.month in (4, 6, 8):
        buy(d, "AMC 84TH ST 6 NEW YORK NY", "AMC Theatres", 17.49, "ENTERTAINMENT_TV_AND_MOVIES")

# Demo storyline: a shopping splurge in early September.
buy(date(2026, 9, 5), "UNIQLO 5TH AVE NEW YORK NY", "Uniqlo", 64.90, "GENERAL_MERCHANDISE_CLOTHING_AND_ACCESSORIES")
buy(date(2026, 9, 8), "SEPHORA 34TH ST NEW YORK NY", "Sephora", 38.50, "GENERAL_MERCHANDISE_CLOTHING_AND_ACCESSORIES")
# ...and a normal weekend after the demo's start date, so "Next day" always has something to show.
buy(date(2026, 9, 19), "SQ *THE HEIGHTS BAR NY", "The Heights Bar", 26.00, "FOOD_AND_DRINK_BEER_WINE_AND_LIQUOR")
buy(date(2026, 9, 19), "BLUE BOTTLE COFFEE NEW YORK NY", "Blue Bottle Coffee", 5.75, "FOOD_AND_DRINK_COFFEE")
buy(date(2026, 9, 20), "TRADER JOE S #558 NEW YORK NY", "Trader Joe's", 27.40, "FOOD_AND_DRINK_GROCERIES")

# --- Splitwise -----------------------------------------------------------------------

PEOPLE = {  # Splitwise user ids
    "Maya": (2210001, "Maya", "Chen"), "Sam": (2210002, "Sam", "Okafor"), "Priya": (2210003, "Priya", "Raman"),
    "Jordan": (2210004, "Jordan", "Lee"), "Alex": (2210005, "Alex", "Moreno"),
}
GROUPS = {"Apartment 4B": (55120001, ["Maya", "Sam", "Priya"]),
          "Friends": (55120002, ["Maya", "Jordan", "Alex", "Priya"])}
CATEGORIES = {"Electricity": 5, "TV/Phone/Internet": 8, "Household supplies": 14, "Dining out": 13,
              "Movies": 21, "General": 18}

expenses = []
pairwise: dict[str, float] = defaultdict(float)  # positive = person owes Maya


def split_equal(cost: float, people: list[str]) -> dict[str, float]:
    cents = round(cost * 100)
    base, extra = divmod(cents, len(people))
    return {p: (base + (1 if i < extra else 0)) / 100 for i, p in enumerate(people)}


def sw_user(name, paid, owed):
    uid, first, last = PEOPLE[name]
    return {"user_id": uid, "user": {"id": uid, "first_name": first, "last_name": last},
            "paid_share": f"{paid:.2f}", "owed_share": f"{owed:.2f}", "net_balance": f"{paid - owed:.2f}"}


def sw_time(d: date, lag_days: int = 0) -> str:
    return datetime(d.year, d.month, d.day, 12, 0, 0).isoformat() + "Z" if not lag_days else \
        datetime((d + timedelta(days=lag_days)).year, (d + timedelta(days=lag_days)).month,
                 (d + timedelta(days=lag_days)).day, 20, 0, 0).isoformat() + "Z"


def expense(d: date, group: str | None, description: str, category: str, cost: float, paid_by: str,
            shares: dict[str, float], lag: int = 0):
    """A Splitwise expense. lag = days before someone actually entered it in the app."""
    users = {p: [0.0, 0.0] for p in shares}
    users.setdefault(paid_by, [0.0, 0.0])
    users[paid_by][0] = cost
    for p, owed in shares.items():
        users[p][1] = owed
    created = sw_time(d, lag) if lag else sw_time(d)
    expenses.append({
        "id": 3400000000 + len(expenses), "group_id": GROUPS[group][0] if group else None,
        "description": description, "payment": False, "cost": f"{cost:.2f}", "currency_code": "USD",
        "date": sw_time(d), "created_at": created, "updated_at": created, "deleted_at": None,
        "category": {"id": CATEGORIES[category], "name": category},
        "users": [sw_user(p, paid, owed) for p, (paid, owed) in users.items()],
    })
    for p, owed in shares.items():
        if paid_by == "Maya" and p != "Maya":
            pairwise[p] += owed
        elif p == "Maya" and paid_by != "Maya":
            pairwise[paid_by] -= owed


def payment(d: date, frm: str, to: str, amount: float, group: str | None, zelle_lag: int | None = 0):
    """A Splitwise 'settle up', plus (optionally) the Zelle that actually moved the money."""
    created = sw_time(d)
    expenses.append({
        "id": 3400000000 + len(expenses), "group_id": GROUPS[group][0] if group else None,
        "description": "Payment", "payment": True, "cost": f"{amount:.2f}", "currency_code": "USD",
        "date": created, "created_at": created, "updated_at": created, "deleted_at": None,
        "category": {"id": CATEGORIES["General"], "name": "General"},
        "users": [sw_user(frm, amount, 0), sw_user(to, 0, amount)],
    })
    if frm == "Maya":
        pairwise[to] += amount
    else:
        pairwise[frm] -= amount
    if zelle_lag is not None:
        other = to if frm == "Maya" else frm
        full = f"{PEOPLE[other][1]} {PEOPLE[other][2]}"
        when = d + timedelta(days=zelle_lag)
        if frm == "Maya":
            txn("chase", when, amount, f"Zelle payment to {full} JPM{rng.randint(10**8, 10**9)}", None,
                "TRANSFER_OUT_ACCOUNT_TRANSFER", "online", (full, "individual"), card=False)
        else:
            txn("chase", when, -amount, f"Zelle payment from {full} {rng.randint(10**10, 10**11)}", None,
                "TRANSFER_IN_ACCOUNT_TRANSFER", "online", (full, "individual"), card=False)


def group_of(people):
    return "Apartment 4B" if set(people) <= {"Maya", "Sam", "Priya"} else "Friends"


timeline = []  # (date, callable) so Splitwise events and settle-ups interleave in date order
for month in range(3, 11):
    coned_day, spectrum_day, supplies_day = date(2026, month, 14), date(2026, month, 16), date(2026, month, 10)
    coned = money(78, 118) if month not in (7, 8) else money(120, 150)
    if coned_day <= END:
        timeline.append((coned_day, lambda c=coned, d=coned_day: expense(
            d, "Apartment 4B", "ConEd electricity", "Electricity", c, "Sam", split_equal(c, ["Maya", "Sam", "Priya"]), lag=1)))
    if spectrum_day <= END:
        timeline.append((spectrum_day, lambda d=spectrum_day: expense(
            d, "Apartment 4B", "Spectrum internet", "TV/Phone/Internet", 65.00, "Sam", split_equal(65.0, ["Maya", "Sam", "Priya"]))))
    if supplies_day <= END:
        cost = money(36, 58)
        buy(supplies_day, "TARGET 00031336 NEW YORK NY", "Target", cost, "GENERAL_MERCHANDISE_SUPERSTORES")
        timeline.append((supplies_day, lambda c=cost, d=supplies_day: expense(
            d, "Apartment 4B", "Household supplies", "Household supplies", c, "Maya", split_equal(c, ["Maya", "Sam", "Priya"]))))

for d, desc, name, merchant, cost, people in [
    (date(2026, 3, 21), "Birthday dinner at Tomo", "SQ *TOMO SUSHI NEW YORK NY", "Tomo Sushi", 142.80, ["Maya", "Jordan", "Alex"]),
    (date(2026, 4, 18), "Dinner at The Grand Tier", "THE GRAND TIER NEW YORK NY", "The Grand Tier", 118.35, ["Maya", "Jordan", "Alex", "Priya"]),
    (date(2026, 5, 30), "Dim sum", "JING FONG NEW YORK NY", "Jing Fong", 96.40, ["Maya", "Alex"]),
    (date(2026, 6, 27), "Rooftop drinks", "SQ *THE HEIGHTS BAR NY", "The Heights Bar", 84.00, ["Maya", "Jordan"]),
    (date(2026, 7, 25), "Patiala Grill dinner", "PATIALA GRILL NEW YORK NY", "Patiala Grill", 126.90, ["Maya", "Jordan", "Alex"]),
    (date(2026, 8, 29), "Tomo sushi night", "SQ *TOMO SUSHI NEW YORK NY", "Tomo Sushi", 144.00, ["Maya", "Jordan", "Alex"]),
    (date(2026, 9, 12), "Brunch at Jacob's Pickles", "JACOBS PICKLES NEW YORK NY", "Jacob's Pickles", 108.60, ["Maya", "Priya", "Alex"]),
]:
    buy(d, name, merchant, cost, "FOOD_AND_DRINK_RESTAURANT")
    timeline.append((d, lambda d=d, desc=desc, cost=cost, people=people: expense(
        d, group_of(people), desc, "Dining out", cost, "Maya", split_equal(cost, people))))

timeline.append((date(2026, 9, 6), lambda: expense(date(2026, 9, 6), "Apartment 4B", "Movie tickets", "Movies", 52.47,
                                                   "Priya", split_equal(52.47, ["Maya", "Priya", "Sam"]))))

# Monthly settle-ups on the 4th-7th: everyone squares up for the previous month.
# Exceptions build September's story: Jordan never pays for the August 29 dinner,
# Priya marks a payment in Splitwise that never reaches the bank, and Alex sends
# a Zelle that nobody records in Splitwise.
timeline.sort(key=lambda e: e[0])
done = 0
for month in range(4, 11):
    settle_day = date(2026, month, 4)
    while done < len(timeline) and timeline[done][0] < settle_day.replace(day=1):
        timeline[done][1](); done += 1
    if settle_day > date(2026, 9, 30):
        break
    for person in ["Sam", "Priya", "Jordan", "Alex"]:
        if month == 9 and person == "Jordan":
            continue
        owed = round(pairwise[person], 2)
        if abs(owed) >= 1:
            day = settle_day + timedelta(days=rng.randint(0, 3))
            group = "Apartment 4B" if person in ("Sam", "Priya") else "Friends"
            if owed > 0:
                payment(day, person, "Maya", owed, group, zelle_lag=rng.choice([0, 1]))
            else:
                payment(day, "Maya", person, -owed, group)
while done < len(timeline) and timeline[done][0] <= date(2026, 9, 13):
    timeline[done][1](); done += 1
sep_brunch = split_equal(108.60, ["Maya", "Priya", "Alex"])
payment(date(2026, 9, 14), "Priya", "Maya", sep_brunch["Priya"], "Friends", zelle_lag=None)  # never arrives
txn("chase", date(2026, 9, 15), -sep_brunch["Alex"], f"Zelle payment from Alex Moreno {rng.randint(10**10, 10**11)}",
    None, "TRANSFER_IN_ACCOUNT_TRANSFER", "online", ("Alex Moreno", "individual"), card=False)
while done < len(timeline) and timeline[done][0] <= date(2026, 9, 16):
    timeline[done][1](); done += 1
payment(date(2026, 9, 17), "Maya", "Sam", -round(pairwise["Sam"], 2), "Apartment 4B") if pairwise["Sam"] < 0 else None
while done < len(timeline):
    timeline[done][1](); done += 1

# --- Write the fixtures --------------------------------------------------------------

OUT.mkdir(parents=True, exist_ok=True)
for item in items.values():
    item["transactions"].sort(key=lambda t: (t["authorized_date"], t["name"]))
json.dump({"_about": "Sandbox fixture in Plaid's schema. Served by sandbox/providers.py as /transactions/sync and "
                     "/accounts/balance/get responses. Fields starting with _sandbox_ are simulation-only.",
           "items": list(items.values())}, open(OUT / "plaid_items.json", "w"), indent=1)

json.dump({"_about": "Sandbox fixture in the Splitwise API v3.0 schema (get_current_user, get_friends, get_groups, "
                     "get_expenses). Served by sandbox/providers.py.",
           "current_user": {"id": PEOPLE["Maya"][0], "first_name": "Maya", "last_name": "Chen",
                            "email": "maya.chen@example.com"},
           "friends": [{"id": uid, "first_name": f, "last_name": l} for n, (uid, f, l) in PEOPLE.items() if n != "Maya"],
           "groups": [{"id": gid, "name": g, "members": [{"id": PEOPLE[m][0]} for m in members]}
                      for g, (gid, members) in GROUPS.items()],
           "expenses": sorted(expenses, key=lambda e: e["created_at"])},
          open(OUT / "splitwise.json", "w"), indent=1)

json.dump({"_about": "What Maya told Penny during onboarding. In production this comes from Penny's own onboarding "
                     "chat or form and is stored as user-provided data.",
           "name": "Maya",
           "monthly_cap": 1800.00,
           "cap_includes_bills": True,
           "known_bills": [
               {"name": "Rent", "amount": 1250.00, "due_day_start": 1, "due_day_end": 10,
                "pay_from": "Bank of America", "how_paid": "bank", "match": "Harlem Heights"},
               {"name": "Electricity (ConEd), my share", "amount": None, "due_day_start": 14, "due_day_end": 17,
                "how_paid": "splitwise", "match": "ConEd", "note": "Sam pays it and splits it three ways; it varies."},
               {"name": "Internet (Spectrum), my share", "amount": 21.67, "due_day_start": 16, "due_day_end": 18,
                "how_paid": "splitwise", "match": "Spectrum"},
           ]},
          open(OUT / "user_profile.json", "w"), indent=1)

n_plaid = sum(len(i["transactions"]) for i in items.values())
print(f"Wrote {n_plaid} Plaid transactions across {len(items)} Items, {len(expenses)} Splitwise expenses, "
      f"and the onboarding profile to {OUT}/")
