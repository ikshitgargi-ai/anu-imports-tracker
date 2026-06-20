#!/usr/bin/env python3
"""One-time history migration: NB tracker Neon → NEW Anu Imports Neon.

Copies, for the Anu Imports tracked SKUs ONLY:
  - stores                 (ALL rows — the 766 LCBO stores, brand-agnostic)
  - sod_inventory          (daily snapshots)
  - sod_products           (product rollup rows)
  - sod_store_sku_changes  (per-store listing flips)
  - sod_listing_changes    (global listing flips)

Usage (deploy day):
  export SOURCE_DATABASE_URL='postgresql://...neon.tech/lcbo_tracker?sslmode=require'   # NB — READ ONLY
  export TARGET_DATABASE_URL='postgresql://...neon.tech/anu_imports?sslmode=require'    # NEW database
  python3 scripts/migrate_history.py

Safety:
  - HARD-REFUSES to run if SOURCE == TARGET.
  - Source connection issues READ-ONLY queries (SELECT only; the transaction
    is additionally set READ ONLY as belt-and-suspenders).
  - Idempotent: every insert is ON CONFLICT DO NOTHING — re-running is safe.
  - Run the target app once first (or let Render boot it) so init_db() has
    created the tables; the script also refuses politely if tables are missing.

Note: stores.rep is copied verbatim and will carry the NB tracker's roster
(Virat/Surya/Neeraj...). Re-stamp via /api/crm/territory-plan apply on the new
app, or update stores.rep manually — the new roster is Ikshit/Vaneet/Ed/Namit.
"""
import os
import sys

try:
    import psycopg2
    import psycopg2.extras
except ImportError:
    sys.exit('psycopg2 is required: pip install psycopg2-binary')

# Mirrors SOD_TRACKED_SKUS in app.py — keep in sync if the registry changes.
TRACKED_SKUS = [
    '0045378',  # Rock Paper Rum Indian Spiced
    '0046340',  # Goenchi Cashew Feni
    '0046343',  # Goenchi Coconut Feni
    '0046282',  # Fratelli Classic Shiraz
    '0046285',  # Fratelli Chenin Blanc
    '0046286',  # Fratelli Sauvignon Blanc
    '0046287',  # Fratelli Cabernet Sauvignon
    '0047777',  # GianChand Single Malt Whisky (pending live — likely 0 rows)
    '0049902',  # Rutland Square Chai Spiced Gin (pending live — likely 0 rows)
]

BATCH = 5000


def _norm_url(u: str) -> str:
    return (u or '').strip().rstrip('/').lower()


def _table_exists(cur, table: str) -> bool:
    cur.execute("SELECT to_regclass(%s)", (f'public.{table}',))
    return cur.fetchone()[0] is not None


def _copy(src, dst, table, cols, select_sql, params, conflict_clause):
    """Stream SELECT from source in batches → INSERT ... ON CONFLICT DO NOTHING."""
    scur = src.cursor(name=f'mig_{table}')  # server-side cursor: low memory
    scur.itersize = BATCH
    scur.execute(select_sql, params)
    dcur = dst.cursor()
    col_list = ', '.join(cols)
    insert_sql = (f'INSERT INTO {table} ({col_list}) VALUES %s '
                  f'ON CONFLICT {conflict_clause} DO NOTHING')
    copied = 0
    while True:
        rows = scur.fetchmany(BATCH)
        if not rows:
            break
        psycopg2.extras.execute_values(dcur, insert_sql, rows, page_size=BATCH)
        copied += len(rows)
        print(f'  {table}: {copied} rows read...', end='\r')
    dst.commit()
    scur.close()
    dcur.close()
    print(f'  {table}: {copied} source rows processed (duplicates skipped).')
    return copied


def main():
    source_url = os.environ.get('SOURCE_DATABASE_URL', '').strip()
    target_url = os.environ.get('TARGET_DATABASE_URL', '').strip()
    if not source_url or not target_url:
        sys.exit('Set SOURCE_DATABASE_URL (NB Neon, read-only) and '
                 'TARGET_DATABASE_URL (new Anu Imports Neon).')
    if _norm_url(source_url) == _norm_url(target_url):
        sys.exit('REFUSING TO RUN: SOURCE_DATABASE_URL == TARGET_DATABASE_URL. '
                 'The target must be a NEW separate Neon database — never the NB one.')

    print(f'Tracked SKUs: {len(TRACKED_SKUS)}')
    src = psycopg2.connect(source_url)
    src.autocommit = False
    with src.cursor() as c:
        c.execute('SET TRANSACTION READ ONLY')  # belt-and-suspenders
    dst = psycopg2.connect(target_url)

    with dst.cursor() as c:
        missing = [t for t in ('stores', 'sod_inventory', 'sod_products',
                               'sod_store_sku_changes', 'sod_listing_changes')
                   if not _table_exists(c, t)]
    if missing:
        sys.exit(f'Target is missing tables {missing} — boot the Anu Imports app '
                 'once against TARGET_DATABASE_URL so init_db() creates the schema.')

    # sod_listing_changes idempotency depends on this unique index; the target
    # is a fresh DB so this succeeds (app.py also attempts it at boot).
    try:
        with dst.cursor() as c:
            c.execute('''CREATE UNIQUE INDEX IF NOT EXISTS uniq_sod_listing_changes
                         ON sod_listing_changes
                         (sku, COALESCE(store_number, -1), change_date, change_type)''')
        dst.commit()
    except Exception as e:
        dst.rollback()
        print(f'  warning: could not ensure uniq_sod_listing_changes index ({e}) '
              '— re-runs may duplicate that one table.')

    counts = {}

    print('1/5 stores (all rows)...')
    counts['stores'] = _copy(
        src, dst, 'stores',
        ['store_number', 'account', 'address', 'city', 'postal', 'phone', 'email',
         'contacts', 'priority', 'status', 'rep', 'manager_name', 'asst_manager_name',
         'manager_phone', 'store_email', 'producer', 'lat', 'lng'],
        '''SELECT store_number, account, address, city, postal,
                  COALESCE(phone,''), COALESCE(email,''), COALESCE(contacts,''),
                  COALESCE(priority,'Standard'), COALESCE(status,''), COALESCE(rep,''),
                  COALESCE(manager_name,''), COALESCE(asst_manager_name,''),
                  COALESCE(manager_phone,''), COALESCE(store_email,''),
                  COALESCE(producer,''), COALESCE(lat,0), COALESCE(lng,0)
           FROM stores''',
        None, '(store_number)')

    sku_filter = 'WHERE sku = ANY(%s)'

    print('2/5 sod_inventory (tracked SKUs only)...')
    counts['sod_inventory'] = _copy(
        src, dst, 'sod_inventory',
        ['sku', 'store_number', 'snapshot_date', 'status', 'on_hand',
         'product_name', 'source'],
        f'''SELECT sku, store_number, snapshot_date, status,
                   COALESCE(on_hand,0), COALESCE(product_name,''),
                   COALESCE(source,'daily_a')
            FROM sod_inventory {sku_filter}''',
        (TRACKED_SKUS,), '(sku, store_number, snapshot_date)')

    print('3/5 sod_products (tracked SKUs only)...')
    counts['sod_products'] = _copy(
        src, dst, 'sod_products',
        ['sku', 'product_name', 'first_seen', 'last_seen', 'current_status',
         'store_count', 'total_on_hand', 'is_tracked', 'brand'],
        f'''SELECT sku, COALESCE(product_name,''), first_seen, last_seen,
                   COALESCE(current_status,'L'), COALESCE(store_count,0),
                   COALESCE(total_on_hand,0), TRUE, COALESCE(brand,'')
            FROM sod_products {sku_filter}''',
        (TRACKED_SKUS,), '(sku)')

    print('4/5 sod_store_sku_changes (tracked SKUs only)...')
    counts['sod_store_sku_changes'] = _copy(
        src, dst, 'sod_store_sku_changes',
        ['sku', 'store_number', 'change_date', 'old_status', 'new_status',
         'change_type'],
        f'''SELECT sku, store_number, change_date, old_status, new_status,
                   change_type
            FROM sod_store_sku_changes {sku_filter}''',
        (TRACKED_SKUS,), '(sku, store_number, change_date, change_type)')

    print('5/5 sod_listing_changes (tracked SKUs only)...')
    counts['sod_listing_changes'] = _copy(
        src, dst, 'sod_listing_changes',
        ['sku', 'store_number', 'change_date', 'old_status', 'new_status',
         'change_type'],
        f'''SELECT sku, store_number, change_date, old_status, new_status,
                   change_type
            FROM sod_listing_changes {sku_filter}''',
        (TRACKED_SKUS,),
        '(sku, COALESCE(store_number, -1), change_date, change_type)')

    src.close()

    # Final target counts — the numbers that matter
    print('\nTarget row counts after migration:')
    with dst.cursor() as c:
        for t in ('stores', 'sod_inventory', 'sod_products',
                  'sod_store_sku_changes', 'sod_listing_changes'):
            c.execute(f'SELECT COUNT(*) FROM {t}')
            print(f'  {t}: {c.fetchone()[0]}')
    dst.close()
    print('\nDone. NOTE: stores.rep still carries the NB roster — re-stamp reps '
          'on the new app (new roster: Ikshit/Vaneet/Ed/Namit).')


if __name__ == '__main__':
    main()
