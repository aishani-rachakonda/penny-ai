"""Generate statement files for Maya, Penny's fictional demo user.

Maya is a grad student in NYC with a spending account at Chase, a salary account
at Bank of America, two roommates and a few friends on Splitwise. The script
writes the files a real user would download from their banks and Splitwise:

    statements/chase_spending.csv   Chase-style checking export
    statements/boa_salary.csv       Bank of America-style checking export
    statements/splitwise.csv        Splitwise-style export (one column per person)

It is deterministic (fixed seed), so every run produces identical files. The app
never imports this script; it only reads the CSVs it writes.

    uv run generate_demo_data.py
"""

import csv
import random
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

START = date(2026, 3, 1)
END = date(2026, 9, 30)
OUT = Path(__file__).parent / "statements"

rng = random.Random(42)

# Each bank transaction: (date, account, description, amount). Negative = money out.
bank: list[tuple[date, str, str, float]] = []
# Each Splitwise row: (date, description, category, cost, paid_by, shares{person: owed})
# or a payment: (date, description, "Payment", amount, from_person, {to_person: amount})
splitwise: list[tuple] = []

ROOMMATES = ["Sam", "Priya"]
FRIENDS = ["Jordan", "Alex"]
PEOPLE = ["Maya"] + ROOMMATES + FRIENDS


def days(start: date, end: date):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


def money(lo: float, hi: float) -> float:
    return round(rng.uniform(lo, hi), 2)


def spend(d: date, description: str, amount: float, account: str = "chase"):
    bank.append((d, account, description, -round(amount, 2)))


def split_equal(cost: float, people: list[str]) -> dict[str, float]:
    """Equal shares in cents, remainder to the first person, like Splitwise."""
    cents = round(cost * 100)
    base, extra = divmod(cents, len(people))
    return {p: (base + (1 if i < extra else 0)) / 100 for i, p in enumerate(people)}


# --- Income, rent, transfers, subscriptions -------------------------------------

d = date(2026, 3, 6)  # biweekly Friday paychecks into the salary account
while d <= END:
    bank.append((d, "boa", "COLUMBIA UNIV DES:PAYROLL ID:10309516 PPD", 1385.40))
    d += timedelta(days=14)

for month in range(3, 10):
    first = date(2026, month, 1)
    bank.append((first, "boa", "HARLEM HEIGHTS MGMT DES:WEB PMTS RENT", -1250.00))
    t = date(2026, month, 3)
    bank.append((t, "boa", "Online Transfer to CHK ...4417 Conf# T" + str(rng.randint(10**7, 10**8)), -700.00))
    bank.append((t, "chase", "Online Transfer From BOA Checking ...8820", 700.00))
    spend(date(2026, month, 7), "SPOTIFY USA 877-7781161 NY", 11.99)
    spend(date(2026, month, 21), "MINT MOBILE 888-588-6468 CA", 30.00)

# --- Day-to-day spending on the Chase card -------------------------------------
# Rates are per-day probabilities, tuned so a typical month lands a little above
# a $1,800 cap (rent included). That tension is what the agent has to manage.

GROCERY = ["TRADER JOE S #558 NEW YORK NY", "KEY FOOD #1123 NEW YORK NY", "WHOLE FOODS MKT HARLEM NY"]
LUNCH = ["SWEETGREEN COLUMBIA NEW YORK NY", "CHIPOTLE 0921 NEW YORK NY", "JUNZI KITCHEN NEW YORK NY"]
COFFEE = ["BLUE BOTTLE COFFEE NEW YORK NY", "DUNKIN #3392 NEW YORK NY", "SQ *HUNGARIAN PASTRY SHOP NY"]
DINNER = ["THE GRAND TIER NEW YORK NY", "SQ *TOMO SUSHI NEW YORK NY", "PATIALA GRILL NEW YORK NY"]
BARS = ["TST* AMSTERDAM TAVERN NEW YORK NY", "1020 BAR NEW YORK NY", "SQ *THE HEIGHTS BAR NY"]
DELIVERY = ["UBER EATS SAN FRANCISCO CA", "DOORDASH*THAI MARKET SAN FRANCISCO CA"]
SHOP = ["AMAZON MKTPL*5O7T232 AMZN.COM/BILL WA", "UNIQLO 5TH AVE NEW YORK NY", "H&M 0098 NEW YORK NY", "TARGET 00031336 NEW YORK NY"]

for d in days(START, END):
    wd = d.weekday()  # 0 = Monday
    weekend = wd >= 5
    friday_or_sat = wd in (4, 5)

    # Subway rides on weekdays, the odd Uber late on weekends.
    if not weekend:
        for _ in range(rng.choice([0, 1, 1, 2])):
            spend(d, "MTA*NYCT PAYGO NEW YORK NY", 2.90)
    if friday_or_sat and rng.random() < 0.12:
        spend(d, "UBER *TRIP HELP.UBER.COM CA", money(11, 24))

    # Coffee is the classic small purchase that adds up.
    if rng.random() < (0.22 if not weekend else 0.12):
        spend(d, rng.choice(COFFEE), money(4.25, 7.50))
    if not weekend and rng.random() < 0.07:
        spend(d, rng.choice(LUNCH), money(11.5, 16.5))
    if rng.random() < 0.13:
        spend(d, rng.choice(GROCERY), money(14, 34))
    if friday_or_sat and rng.random() < 0.15:
        spend(d, rng.choice(DINNER), money(22, 46))
    if friday_or_sat and rng.random() < 0.25:
        spend(d, rng.choice(BARS), money(14, 38))
    if rng.random() < 0.03:
        spend(d, rng.choice(DELIVERY), money(17, 31))
    if rng.random() < 0.035:
        spend(d, rng.choice(SHOP), money(12, 48))
    if d.day == 9 and d.month % 2:
        spend(d, "SQ *HARLEM THREADING NEW YORK NY", 22.00)
    if d.day == 24 and d.month in (4, 6, 8):
        spend(d, "AMC 84TH ST 6 NEW YORK NY", 17.49)

# --- Shared expenses on Splitwise ---------------------------------------------
# Household bills: Sam pays ConEd and Spectrum and splits them three ways.
# Maya pays for household groceries now and then. Group dinners with friends.

for month in range(3, 10):
    coned = money(78, 118) if month not in (7, 8) else money(120, 150)  # A/C summer
    splitwise.append((date(2026, month, 14), "ConEd electricity", "Utilities", coned,
                      "Sam", split_equal(coned, ["Maya", "Sam", "Priya"])))
    splitwise.append((date(2026, month, 16), "Spectrum internet", "Utilities", 65.00,
                      "Sam", split_equal(65.00, ["Maya", "Sam", "Priya"])))
    shop_day = date(2026, month, 10)
    cost = money(36, 58)
    spend(shop_day, "TARGET 00031336 NEW YORK NY", cost)
    splitwise.append((shop_day, "Household supplies", "Household", cost,
                      "Maya", split_equal(cost, ["Maya", "Sam", "Priya"])))

group_dinners = [
    (date(2026, 3, 21), "Birthday dinner at Tomo", "SQ *TOMO SUSHI NEW YORK NY", 142.80, ["Maya", "Jordan", "Alex"]),
    (date(2026, 4, 18), "Dinner at The Grand Tier", "THE GRAND TIER NEW YORK NY", 118.35, ["Maya", "Jordan", "Alex", "Priya"]),
    (date(2026, 5, 30), "Dim sum", "JING FONG NEW YORK NY", 96.40, ["Maya", "Alex"]),
    (date(2026, 6, 27), "Rooftop drinks", "SQ *THE HEIGHTS BAR NY", 84.00, ["Maya", "Jordan"]),
    (date(2026, 7, 25), "Patiala Grill dinner", "PATIALA GRILL NEW YORK NY", 126.90, ["Maya", "Jordan", "Alex"]),
    (date(2026, 8, 29), "Tomo sushi night", "SQ *TOMO SUSHI NEW YORK NY", 144.00, ["Maya", "Jordan", "Alex"]),
    (date(2026, 9, 12), "Brunch at Jacob's Pickles", "JACOBS PICKLES NEW YORK NY", 108.60, ["Maya", "Priya", "Alex"]),
]
for d, desc, merchant, cost, people in group_dinners:
    spend(d, merchant, cost)
    splitwise.append((d, desc, "Dining out", cost, "Maya", split_equal(cost, people)))

# A movie night Priya paid for (Maya owes her).
splitwise.append((date(2026, 9, 6), "Movie tickets", "Entertainment", 52.47,
                  "Priya", split_equal(52.47, ["Maya", "Priya", "Sam"])))

# --- Settle-ups -----------------------------------------------------------------
# Compute what each person owes Maya after each month, then settle most of it by
# Zelle a few days into the next month. September is left deliberately messy.

pairwise: dict[str, float] = defaultdict(float)  # positive = person owes Maya


def apply_expense(paid_by: str, shares: dict[str, float]):
    for person, owed in shares.items():
        if paid_by == "Maya" and person != "Maya":
            pairwise[person] += owed
        elif person == "Maya" and paid_by != "Maya":
            pairwise[paid_by] -= owed


def settle(d: date, person: str, amount: float, zelle: bool = True):
    """Record a Splitwise payment and (usually) the matching Zelle.

    amount > 0 means the person pays Maya; amount < 0 means Maya pays them.
    """
    amount = round(amount, 2)
    ref = rng.randint(10**10, 10**11)
    if amount > 0:
        splitwise.append((d, f"{person} paid Maya", "Payment", amount, person, {"Maya": amount}))
        if zelle:
            bank.append((d + timedelta(days=rng.choice([0, 1])), "chase",
                         f"Zelle Payment From {person} {FULL_NAMES[person]} {ref}", amount))
    else:
        splitwise.append((d, f"Maya paid {person}", "Payment", -amount, "Maya", {person: -amount}))
        if zelle:
            bank.append((d, "chase", f"Zelle Payment To {person} {FULL_NAMES[person]} {ref}", amount))
    pairwise[person] -= amount


FULL_NAMES = {"Sam": "Okafor", "Priya": "Raman", "Jordan": "Lee", "Alex": "Moreno"}

for month in range(3, 10):
    month_start = date(2026, month, 1)
    month_end = (date(2026, month + 1, 1) - timedelta(days=1)) if month < 12 else date(2026, 12, 31)
    for row in splitwise:
        if row[2] != "Payment" and month_start <= row[0] <= month_end:
            apply_expense(row[4], row[5])
    if month == 9:
        break
    settle_day = date(2026, month + 1, 4)
    for person in ROOMMATES + FRIENDS:
        if month == 8 and person == "Jordan":
            continue  # Jordan never pays back the August 29 sushi night
        if abs(pairwise[person]) >= 1:
            settle(settle_day + timedelta(days=rng.randint(0, 3)), person, pairwise[person])

# September, the month the demo plays out in:
#  - Sam settles utilities with Maya normally (bank and Splitwise agree).
#  - Priya marks her brunch share as paid in Splitwise, but no Zelle ever arrives.
#  - Alex sends a Zelle for brunch that nobody recorded in Splitwise.
#  - Jordan still owes for the August 29 sushi night.
sep_dinner = split_equal(108.60, ["Maya", "Priya", "Alex"])
settle(date(2026, 9, 17), "Sam", pairwise["Sam"])
settle(date(2026, 9, 14), "Priya", sep_dinner["Priya"], zelle=False)
bank.append((date(2026, 9, 15), "chase", f"Zelle Payment From Alex Moreno {rng.randint(10**10, 10**11)}", sep_dinner["Alex"]))

# Demo month storyline: a shopping splurge early in September puts that category
# over plan, so the agent has to pull back elsewhere.
spend(date(2026, 9, 5), "UNIQLO 5TH AVE NEW YORK NY", 64.90)
spend(date(2026, 9, 8), "SEPHORA 34TH ST NEW YORK NY", 38.50)

# --- Write the statements ------------------------------------------------------

OUT.mkdir(exist_ok=True)
bank.sort(key=lambda r: (r[0], r[2]))


def write_chase(rows):
    """Chase checking export: Details, Posting Date, Description, Amount, Type, Balance."""
    balance = 640.00
    with open(OUT / "chase_spending.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Details", "Posting Date", "Description", "Amount", "Type", "Balance", "Check or Slip #"])
        for d, _, desc, amt in rows:
            balance = round(balance + amt, 2)
            kind = ("ACH_CREDIT" if "Transfer" in desc else "QUICKPAY_CREDIT" if "Zelle" in desc else "MISC_CREDIT") \
                if amt > 0 else ("QUICKPAY_DEBIT" if "Zelle" in desc else "DEBIT_CARD")
            w.writerow(["CREDIT" if amt > 0 else "DEBIT", d.strftime("%m/%d/%Y"), desc, f"{amt:.2f}", kind, f"{balance:.2f}", ""])


def write_boa(rows):
    """Bank of America checking export: a short summary block, then Date, Description, Amount, Running Bal."""
    balance = 2850.00
    with open(OUT / "boa_salary.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Description", "", "Summary Amt."])
        w.writerow([f"Beginning balance as of {START.strftime('%m/%d/%Y')}", "", f"{balance:.2f}"])
        w.writerow([])
        w.writerow(["Date", "Description", "Amount", "Running Bal."])
        w.writerow([START.strftime("%m/%d/%Y"), f"Beginning balance as of {START.strftime('%m/%d/%Y')}", "", f"{balance:.2f}"])
        for d, _, desc, amt in rows:
            balance = round(balance + amt, 2)
            w.writerow([d.strftime("%m/%d/%Y"), desc, f"{amt:.2f}", f"{balance:.2f}"])


def write_splitwise(rows):
    """Splitwise export: Date, Description, Category, Cost, Currency, then each person's net balance change."""
    rows = sorted(rows, key=lambda r: r[0])
    with open(OUT / "splitwise.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Date", "Description", "Category", "Cost", "Currency"] + PEOPLE)
        for d, desc, category, cost, paid_by, shares in rows:
            net = {p: 0.0 for p in PEOPLE}
            net[paid_by] += cost
            for person, owed in shares.items():
                net[person] -= owed
            w.writerow([d.isoformat(), desc, category, f"{cost:.2f}", "USD"] + [f"{net[p]:.2f}" for p in PEOPLE])


write_chase([r for r in bank if r[1] == "chase"])
write_boa([r for r in bank if r[1] == "boa"])
write_splitwise(splitwise)
print(f"Wrote {sum(1 for r in bank if r[1] == 'chase')} Chase, {sum(1 for r in bank if r[1] == 'boa')} BoA "
      f"and {len(splitwise)} Splitwise rows to {OUT}/")
