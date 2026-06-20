#!/usr/bin/env python3
"""Seed the LOCAL anu_imports.db with synthetic demo data (smoke tests + UI preview).

Creates, idempotently:
  - 4 demo stores stamped to the live roster (Ikshit / Vaneet / Ed / Namit)
  - 28 days of synthetic SOD history for 0045378 (Rock Paper Rum Indian Spiced)
    and 0046340 (Goenchi Cashew Feni) across those stores, engineered to hit a
    mix of forecast flags: RED, YELLOW, STALL, GREEN (+ an out-of-stock RED)
  - sod_products rollup rows for both SKUs
  - 2 horeca_accounts (1 active customer, 1 prospect)

LOCAL SQLITE ONLY — hard-refuses to run when DATABASE_URL (Postgres) is set.
Production data comes from the real SOD feed + scripts/migrate_history.py.

Run from the repo root:  python3 scripts/seed_demo.py
Re-running is safe (INSERT OR REPLACE / OR IGNORE everywhere).
"""
import os
import sqlite3
import sys
from datetime import date, timedelta

if os.environ.get('DATABASE_URL', '').strip():
    sys.exit('REFUSING TO RUN: DATABASE_URL is set. This seed is for the local '
             'SQLite anu_imports.db only — never production Postgres.')

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# Importing app runs init_db(): creates anu_imports.db, seeds the 766 LCBO
# stores from data/All LCBO stores.xlsx, the 4-rep roster and TRACKED_PRODUCTS.
import app as app_module  # noqa: E402

DB = os.path.join(os.environ.get('DB_DIR', ROOT), 'anu_imports.db')
ANCHOR = date.today() - timedelta(days=1)  # latest snapshot = yesterday (typical SOD lag)
DAYS = 28

ROCK = '0045378'
ROCK_NAME = 'Rock Paper Rum Indian Spiced'
FENI = '0046340'
FENI_NAME = 'Goenchi Cashew Feni'

# (store_number, account, city, postal, rep) — store numbers in the 9xxx range
# so they never collide with real LCBO store numbers from the xlsx seed.
DEMO_STORES = [
    (9001, 'LCBO #9001 Oakville Demo',    'Oakville',    'L6H 1A1', 'Ikshit'),
    (9002, 'LCBO #9002 Toronto Demo',     'Toronto',     'M5V 1J1', 'Namit'),
    (9003, 'LCBO #9003 Mississauga Demo', 'Mississauga', 'L5B 1B8', 'Vaneet'),
    (9004, 'LCBO #9004 Hamilton Demo',    'Hamilton',    'L8P 1A1', 'Ed'),
]


def linear_series(start, end, days=DAYS):
    """Evenly declining integer on-hand series of `days` points, start → end."""
    step = (start - end) / float(days - 1)
    return [max(0, round(start - step * i)) for i in range(days)]


def flat_series(level, days=DAYS):
    return [level] * days


def stockout_series(start, out_from_day, days=DAYS):
    """Declines from `start` to 0 by `out_from_day`, then stays 0 (RED: OOS while selling)."""
    sell_days = out_from_day
    series = linear_series(start, 0, sell_days)
    return series + [0] * (days - sell_days)


# Engineered against _forecast_classify (RED ≤7d cover, YELLOW ≤21d, STALL vel=0):
#   16 → 2  over 28d = 3.5/wk velocity, 2 left → ~4d cover   → RED
#   40 → 32 over 28d = 2/wk,        32 left → ~112d cover    → GREEN
#   18 → 6  over 28d = 3/wk,         6 left → ~14d cover     → YELLOW
#   flat                                                      → STALL
#   12 → 0 by day 22, 0 since = was selling, now empty        → RED (OUT OF STOCK)
#   28 → 26 over 28d = 0.5/wk,      26 left → ~364d cover     → GREEN
SEED_PLAN = {
    9001: {ROCK: linear_series(16, 2),        FENI: linear_series(28, 26)},   # RED   + GREEN
    9002: {ROCK: linear_series(40, 32),       FENI: linear_series(18, 6)},    # GREEN + YELLOW
    9003: {ROCK: flat_series(12),             FENI: stockout_series(12, 22)}, # STALL + RED(OOS)
    9004: {ROCK: linear_series(18, 6),        FENI: flat_series(8)},          # YELLOW+ STALL
}

NAMES = {ROCK: ROCK_NAME, FENI: FENI_NAME}

conn = sqlite3.connect(DB)
cur = conn.cursor()

for num, account, city, postal, rep in DEMO_STORES:
    cur.execute(
        "INSERT OR IGNORE INTO stores (store_number, account, city, postal, rep, lat, lng) "
        "VALUES (?,?,?,?,?,?,?)",
        (num, account, city, postal, rep,
         *app_module.CITY_COORDS.get(city, (0, 0))))
    cur.execute("UPDATE stores SET rep=? WHERE store_number=?", (rep, num))

rows = 0
for store_num, by_sku in SEED_PLAN.items():
    for sku, series in by_sku.items():
        for i, on_hand in enumerate(series):
            snap = (ANCHOR - timedelta(days=DAYS - 1 - i)).isoformat()
            cur.execute(
                "INSERT OR REPLACE INTO sod_inventory "
                "(sku, store_number, snapshot_date, status, on_hand, product_name, source) "
                "VALUES (?,?,?,'L',?,?,'seed_demo')",
                (sku, store_num, snap, on_hand, NAMES[sku]))
            rows += 1

first_seen = (ANCHOR - timedelta(days=DAYS - 1)).isoformat()
for sku, name in NAMES.items():
    brand = app_module.SOD_TRACKED_SKUS[sku][0]
    store_count = sum(1 for plan in SEED_PLAN.values() if sku in plan)
    total = sum(plan[sku][-1] for plan in SEED_PLAN.values() if sku in plan)
    cur.execute(
        "INSERT OR REPLACE INTO sod_products "
        "(sku, product_name, first_seen, last_seen, current_status, store_count, "
        " total_on_hand, is_tracked, brand) VALUES (?,?,?,?, 'L', ?, ?, 1, ?)",
        (sku, name, first_seen, ANCHOR.isoformat(), store_count, total, brand))

HORECA = [
    ('Bar Goa', 'bar', '123 Queen St W', 'Toronto', 'M5H 2M9', 'Namit',
     'active', 'Goenchi Cashew Feni', 'manual',
     'Demo seed — pouring Goenchi on the back bar since May.'),
    ('The Spice Room', 'restaurant', '88 Lakeshore Rd E', 'Mississauga', 'L5G 1E1',
     'Vaneet', 'prospect', '', 'manual',
     'Demo seed — Indian fine dining, shortlist for Rock Paper + Feni tasting.'),
]
for name, atype, addr, city, postal, rep, status, carried, source, notes in HORECA:
    cur.execute("SELECT id FROM horeca_accounts WHERE name=? AND city=?", (name, city))
    if cur.fetchone() is None:
        cur.execute(
            "INSERT INTO horeca_accounts (name, account_type, address, city, postal, "
            "rep_name, status, products_carried, source, notes) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (name, atype, addr, city, postal, rep, status, carried, source, notes))

conn.commit()

n_stores = cur.execute("SELECT COUNT(*) FROM stores").fetchone()[0]
n_inv = cur.execute("SELECT COUNT(*) FROM sod_inventory WHERE source='seed_demo'").fetchone()[0]
n_horeca = cur.execute("SELECT COUNT(*) FROM horeca_accounts").fetchone()[0]
conn.close()

print(f'Seeded {DB}')
print(f'  stores total:            {n_stores} (incl. 4 demo stores 9001-9004)')
print(f'  demo sod_inventory rows: {n_inv} ({rows} written this run)')
print(f'  horeca_accounts:         {n_horeca}')
print(f'  anchor (latest snapshot): {ANCHOR.isoformat()}')
print('Expected forecast flags — 9001: RED+GREEN, 9002: GREEN+YELLOW, '
      '9003: STALL+RED(OOS), 9004: YELLOW+STALL')
