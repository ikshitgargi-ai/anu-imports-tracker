"""Canonical listing ledger — ported from the proven Dripp Tracker engine.

Tracking accuracy IS the product. These tests assert that:
  - listing_ledger + store_listings exist (lazy _ensure_listing_ledger DDL),
  - _ledger_record folds LISTED / RECONFIRMED / DELISTED correctly, including
    the DELISTED-only-if-observed>=last-LISTED guard,
  - the ledger insert is idempotent (UNIQUE guard, one row/day/source),
  - store_listings is a PURE fold of the ledger (rebuild == incremental),
  - /api/listings/record (manual), /api/listings/backfill and
    /api/listings/rebuild behave and are idempotent,
  - the SOD-loss guarantee holds: wipe every SOD table, rebuild from the
    ledger alone, and store_listings is byte-for-byte the same,
  - the REP fold: POST /api/crm/activities with a sku_outcomes outcome of
    'listed' lands a LISTED ledger event (and nothing else maps),
  - the LIVE fold: two fake-scraper batches produce LISTED / DELISTED /
    RECONFIRMED ledger rows (deduped per day),
  - /api/reconcile carries the rep outcome overlay (informational only),
  - the immutable ledger is never UPDATE-d or DELETE-d anywhere in app.py,
  - SAVEPOINT-scoped recovery exists in the ingest paths (July-4 fix),
  - _RETENTION_PROTECTED_TABLES guards the restore path and the backup set
    carries the ledger.

Run with: python3 -m pytest tests/test_ledger.py -v
"""
import os
import re
import sqlite3
import sys
import tempfile

import pytest

# Force SQLite for tests so we never touch production Postgres.
os.environ.pop('DATABASE_URL', None)
os.environ.pop('ADMIN_TOKEN', None)
os.environ.pop('API_KEY', None)
os.environ.pop('SOD_CRON_TOKEN', None)
_TMP = tempfile.mkdtemp(prefix='anu_imports_ledger_test_')
os.environ['DB_DIR'] = _TMP

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

FOCUS = '0045378'   # Rock Paper Rum Indian Spiced — live at LCBO
SECOND = '0046340'  # Goenchi Cashew Feni
APP_PY = os.path.join(os.path.dirname(__file__), '..', 'app.py')


@pytest.fixture(scope='module')
def app_module():
    """Import app.py fresh against an isolated SQLite file (same pattern as
    tests/test_live_engine.py). DB_DIR is re-asserted here because pytest
    imports every test module during collection and the LAST module import
    wins the env var."""
    prev_db_dir = os.environ.get('DB_DIR')
    os.environ['DB_DIR'] = _TMP
    for mod in list(sys.modules):
        if mod == 'app' or mod.startswith('app.'):
            del sys.modules[mod]
    import importlib.util
    spec = importlib.util.spec_from_file_location('app', APP_PY)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    # Lazy DDL for the ledger + live-engine tables (prod does this on first use).
    conn = sqlite3.connect(os.path.join(_TMP, 'anu_imports.db'))
    m._ensure_listing_ledger(conn)
    m._ensure_live_tables(conn)
    conn.close()
    yield m
    if prev_db_dir is None:
        os.environ.pop('DB_DIR', None)
    else:
        os.environ['DB_DIR'] = prev_db_dir


@pytest.fixture
def client(app_module):
    app_module.app.config['TESTING'] = True
    app_module._rate_buckets.clear()  # tests fire faster than 50 req/s
    with app_module.app.test_client() as c:
        yield c


def _db():
    conn = sqlite3.connect(os.path.join(_TMP, 'anu_imports.db'))
    conn.row_factory = sqlite3.Row
    return conn


def _reset_ledger():
    """Clear ledger + source tables between tests (test scratch DB only —
    nothing in prod ever deletes these)."""
    conn = _db()
    for t in ('listing_ledger', 'store_listings', 'sod_store_sku_changes',
              'live_listing_events', 'sod_inventory', 'sod_products', 'event_log'):
        try:
            conn.execute(f"DELETE FROM {t}")
        except Exception:
            pass
    conn.commit()
    conn.close()


def _record(app_module, sku, store, event, source, detail, observed, note=''):
    conn = _db()
    cur = conn.cursor()
    ok = app_module._ledger_record(cur, sku, store, event, source, detail, observed, note=note)
    conn.commit()
    conn.close()
    return ok


def _listing(sku, store):
    conn = _db()
    row = conn.execute(
        "SELECT status, first_listed_date, last_confirmed_date, delisted_date, "
        "sources_seen, confirm_count FROM store_listings WHERE sku=? AND store_number=?",
        (sku, store)).fetchone()
    conn.close()
    return row


def _snapshot_store_listings():
    conn = _db()
    rows = conn.execute(
        "SELECT sku, store_number, status, first_listed_date, last_confirmed_date, "
        "delisted_date, sources_seen, confirm_count FROM store_listings "
        "ORDER BY sku, store_number").fetchall()
    conn.close()
    return [tuple(r) for r in rows]


def _json(client, path, **headers):
    r = client.get(path, headers=headers or None)
    return r.status_code, (r.get_json() if r.is_json else None), r


# ── A. Schema exists (lazy DDL) ────────────────────────────────────────────
def test_tables_exist(app_module):
    conn = _db()
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    conn.close()
    assert 'listing_ledger' in tables
    assert 'store_listings' in tables


def test_ledger_unique_guard(app_module):
    _reset_ledger()
    conn = _db()
    conn.execute(
        "INSERT INTO listing_ledger (sku, store_number, event, source, observed_date) "
        "VALUES (?,?,?,?,?)", (FOCUS, 100, 'LISTED', 'sod', '2026-07-01'))
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO listing_ledger (sku, store_number, event, source, observed_date) "
            "VALUES (?,?,?,?,?)", (FOCUS, 100, 'LISTED', 'sod', '2026-07-01'))
        conn.commit()
    conn.close()


# ── B. Fold semantics ──────────────────────────────────────────────────────
def test_fold_listed_sets_first_and_status(app_module):
    _reset_ledger()
    assert _record(app_module, FOCUS, 201, 'LISTED', 'sod', '2026-07-02', '2026-07-02')
    row = _listing(FOCUS, 201)
    assert row['status'] == 'LISTED'
    assert row['first_listed_date'] == '2026-07-02'
    assert row['last_confirmed_date'] == '2026-07-02'
    assert row['delisted_date'] is None
    assert row['confirm_count'] == 1
    assert row['sources_seen'] == 'sod'


def test_fold_reconfirm_bumps_and_first_listed_is_min(app_module):
    _reset_ledger()
    _record(app_module, FOCUS, 202, 'LISTED', 'sod', '2026-07-05', '2026-07-05')
    _record(app_module, FOCUS, 202, 'LISTED', 'live', 'b1', '2026-07-02')  # earlier
    _record(app_module, FOCUS, 202, 'RECONFIRMED', 'rep', 'Namit', '2026-07-09')
    row = _listing(FOCUS, 202)
    assert row['first_listed_date'] == '2026-07-02'      # min over LISTED
    assert row['last_confirmed_date'] == '2026-07-09'     # max over LISTED+RECONFIRM
    assert row['confirm_count'] == 3
    assert row['sources_seen'] == 'live,rep,sod'          # sorted, deduped
    assert row['status'] == 'LISTED'


def test_fold_delisted_guard_ignores_stale(app_module):
    _reset_ledger()
    _record(app_module, FOCUS, 203, 'LISTED', 'sod', '2026-07-01', '2026-07-01')
    _record(app_module, FOCUS, 203, 'RECONFIRMED', 'sod', '2026-07-05', '2026-07-05')
    # Stale delist observed BEFORE the latest presence proof → ignored.
    _record(app_module, FOCUS, 203, 'DELISTED', 'sod', '2026-07-03', '2026-07-03')
    row = _listing(FOCUS, 203)
    assert row['status'] == 'LISTED'
    assert row['delisted_date'] is None
    # A delist on/after the latest confirmation wins.
    _record(app_module, FOCUS, 203, 'DELISTED', 'sod', '2026-07-10', '2026-07-10')
    row = _listing(FOCUS, 203)
    assert row['status'] == 'DELISTED'
    assert row['delisted_date'] == '2026-07-10'
    assert row['confirm_count'] == 2  # DELISTED never counts as a confirmation


def test_ledger_record_is_idempotent(app_module):
    _reset_ledger()
    assert _record(app_module, FOCUS, 204, 'LISTED', 'sod', '2026-07-01', '2026-07-01') is True
    assert _record(app_module, FOCUS, 204, 'LISTED', 'sod', '2026-07-01', '2026-07-01') is False
    conn = _db()
    n = conn.execute("SELECT COUNT(*) FROM listing_ledger WHERE sku=? AND store_number=?",
                     (FOCUS, 204)).fetchone()[0]
    conn.close()
    assert n == 1
    assert _listing(FOCUS, 204)['confirm_count'] == 1


def test_ledger_record_audits_event_log(app_module):
    _reset_ledger()
    _record(app_module, FOCUS, 205, 'LISTED', 'manual', 'test', '2026-07-01')
    conn = _db()
    n = conn.execute("SELECT COUNT(*) FROM event_log WHERE event_type='listing_listed'").fetchone()[0]
    conn.close()
    assert n >= 1


# ── Manual record endpoint ─────────────────────────────────────────────────
def test_manual_record_endpoint(app_module, client):
    _reset_ledger()
    resp = client.post('/api/listings/record', json={
        'sku': FOCUS, 'store_number': 300, 'event': 'LISTED',
        'observed_date': '2026-07-01', 'note': 'known placement'})
    assert resp.status_code == 201, resp.get_data(as_text=True)
    body = resp.get_json()
    assert body['inserted'] is True
    assert body['listing']['status'] == 'LISTED'
    assert body['listing']['first_listed_date'] == '2026-07-01'
    # Bad event rejected.
    bad = client.post('/api/listings/record', json={
        'sku': FOCUS, 'store_number': 300, 'event': 'NONSENSE'})
    assert bad.status_code == 400


# ── REP fold: /api/crm/activities with sku_outcomes ────────────────────────
def _seed_store(store_number, city='Toronto'):
    conn = _db()
    conn.execute(
        "INSERT OR IGNORE INTO stores (store_number, account, address, city, priority) "
        "VALUES (?,?,?,?,?)",
        (store_number, f'LCBO #{store_number}', '', city, 'Standard'))
    conn.commit()
    conn.close()


def test_rep_fold_from_activities_post(app_module, client):
    _reset_ledger()
    _seed_store(888)
    resp = client.post('/api/crm/activities', json={
        'activity_type': 'visit',
        'store_number': 888,
        'rep': 'Namit',
        'visit_date': '2026-07-01',
        'sku_outcomes': [
            {'sku': FOCUS, 'outcome': 'Listed'},     # case-insensitive → folds
            {'sku': SECOND, 'outcome': 'declined'},  # NOT a listing event
        ],
    })
    assert resp.status_code == 200, resp.get_data(as_text=True)
    body = resp.get_json()
    assert body['status'] == 'ok'
    assert body.get('ledger_events') == 1

    conn = _db()
    rows = conn.execute(
        "SELECT sku, event, source, source_detail, observed_date "
        "FROM listing_ledger WHERE store_number=888").fetchall()
    conn.close()
    assert len(rows) == 1
    r = rows[0]
    assert r['sku'] == FOCUS
    assert r['event'] == 'LISTED'
    assert r['source'] == 'rep'
    assert r['source_detail'] == 'Namit'
    assert r['observed_date'] == '2026-07-01'  # backdated visit_date honoured
    # Fold materialized; the declined SKU never entered the ledger.
    assert _listing(FOCUS, 888)['status'] == 'LISTED'
    assert _listing(SECOND, 888) is None


def test_rep_fold_other_outcomes_never_map(app_module, client):
    _reset_ledger()
    _seed_store(889)
    resp = client.post('/api/crm/activities', json={
        'activity_type': 'visit', 'store_number': 889, 'rep': 'Ikshit',
        'sku_outcomes': [
            {'sku': FOCUS, 'outcome': 'discussed'},
            {'sku': FOCUS, 'outcome': 'sampled'},
            {'sku': SECOND, 'outcome': 'tasting booked'},
        ],
    })
    assert resp.status_code == 200
    assert resp.get_json().get('ledger_events') == 0
    conn = _db()
    n = conn.execute(
        "SELECT COUNT(*) FROM listing_ledger WHERE store_number=889").fetchone()[0]
    conn.close()
    assert n == 0


# ── LIVE fold: two fake-scraper batches ────────────────────────────────────
def _fake_scraper(per_sku, default=((1, 5),)):
    """Build a _live_scrape_sku stand-in (same shape as test_live_engine.py)."""
    def fake(sku):
        rows = per_sku.get(str(sku).zfill(7))
        if rows is None:
            if default is None:
                return [], ('no store rows parsed — product may be delisted '
                            'or page layout changed')
            rows = list(default)
        return ([
            {'store_number': str(sn), 'city': 'Toronto', 'intersection': 'Test & Test',
             'store_name': f'Test Store {sn}', 'address': f'{sn} Test St', 'phone': '',
             'quantity': qty}
            for sn, qty in rows
        ], None)
    return fake


def test_live_fold_two_batches(app_module, monkeypatch):
    _reset_ledger()
    # Clean live-engine state so batch 1 really is the baseline (test DB only).
    conn = _db()
    for t in ('lcbo_live_snapshots', 'lcbo_live_batches', 'live_listing_events'):
        conn.execute(f"DELETE FROM {t}")
    conn.commit()
    conn.close()

    today = app_module._toronto_today().isoformat()
    monkeypatch.setattr(app_module, 'LIVE_SCRAPE_GAP_SECONDS', 0)

    # Batch 1 — no baseline: every store seen folds as RECONFIRMED.
    monkeypatch.setattr(app_module, '_live_scrape_sku',
                        _fake_scraper({FOCUS: [(11, 6), (12, 10), (13, 4)]}))
    s1 = app_module.run_live_batch(triggered_by='test')
    assert s1['status'] == 'ok', s1

    conn = _db()
    n_reconf = conn.execute(
        "SELECT COUNT(*) FROM listing_ledger WHERE sku=? AND source='live' "
        "AND event='RECONFIRMED'", (FOCUS,)).fetchone()[0]
    conn.close()
    assert n_reconf == 3  # stores 11, 12, 13

    # Batch 2 — store 14 appears (LISTED), store 13 disappears (DELISTED),
    # stores 11/12 reconfirm but dedupe to the one-per-day row from batch 1.
    monkeypatch.setattr(app_module, '_live_scrape_sku',
                        _fake_scraper({FOCUS: [(11, 6), (12, 15), (14, 8)]}))
    s2 = app_module.run_live_batch(triggered_by='test')
    assert s2['status'] == 'ok', s2

    conn = _db()
    events = {
        (r['event'], r['store_number']): r
        for r in conn.execute(
            "SELECT event, store_number, source_detail, observed_date "
            "FROM listing_ledger WHERE sku=? AND source='live' "
            "AND event IN ('LISTED','DELISTED')", (FOCUS,)).fetchall()
    }
    reconf_11 = conn.execute(
        "SELECT COUNT(*) FROM listing_ledger WHERE sku=? AND store_number=11 "
        "AND event='RECONFIRMED' AND source='live'", (FOCUS,)).fetchone()[0]
    conn.close()

    assert ('LISTED', 14) in events
    assert events[('LISTED', 14)]['source_detail'] == s2['batch_id']
    assert events[('LISTED', 14)]['observed_date'] == today
    assert ('DELISTED', 13) in events
    assert reconf_11 == 1  # deduped to one RECONFIRMED per day/source

    # Materialized fold agrees with the live signal.
    assert _listing(FOCUS, 14)['status'] == 'LISTED'
    assert _listing(FOCUS, 14)['first_listed_date'] == today
    assert _listing(FOCUS, 13)['status'] == 'DELISTED'
    assert _listing(FOCUS, 11)['status'] == 'LISTED'


# ── Reconcile: rep outcome overlay (informational only) ────────────────────
def test_reconcile_rep_outcome_overlay(app_module, client):
    _reset_ledger()
    _seed_store(777)
    today = app_module._toronto_today().isoformat()
    conn = _db()
    conn.execute(
        "INSERT OR IGNORE INTO sod_inventory "
        "(sku, store_number, snapshot_date, status, on_hand, product_name) "
        "VALUES (?,?,?,?,?,?)",
        (FOCUS, 777, today, 'L', 6, 'ROCK PAPER RUM INDIAN SPICED'))
    conn.commit()
    conn.close()
    resp = client.post('/api/crm/activities', json={
        'activity_type': 'visit', 'store_number': 777, 'rep': 'Namit',
        'visit_date': today,
        'sku_outcomes': [{'sku': FOCUS, 'outcome': 'listed'}],
    })
    assert resp.status_code == 200

    code, body, _ = _json(client, f'/api/reconcile?sku={FOCUS}&nocache=1')
    assert code == 200
    assert body['mode'] == '2-way + rep outcome overlay'
    rows = {r['store_number']: r for r in body['rows']}
    assert 777 in rows
    assert rows[777]['rep_outcome'] == 'listed'
    assert rows[777]['rep_observed_at']
    # No units are invented and no new flag semantics appear.
    assert rows[777]['rep_units'] is None
    assert rows[777]['rep_on_shelf'] is None
    assert rows[777]['delta_rep_live'] is None
    assert rows[777]['flag'] != 'REP_MISMATCH'


# ── C. Backfill + rebuild ──────────────────────────────────────────────────
def _seed_sources():
    conn = _db()
    conn.executemany(
        "INSERT INTO sod_store_sku_changes "
        "(sku, store_number, change_date, old_status, new_status, change_type) "
        "VALUES (?,?,?,?,?,?)",
        [
            (FOCUS, 501, '2026-07-01', None, 'L', 'NEW_LISTING'),
            (FOCUS, 502, '2026-07-02', None, 'L', 'NEW_LISTING'),
            (SECOND, 503, '2026-07-03', None, 'L', 'NEW_LISTING'),
            (FOCUS, 502, '2026-07-06', 'L', None, 'DROPPED'),   # 502 later dropped
            (FOCUS, 501, '2026-07-04', 'L', 'F', 'STATUS_FLIP'),  # ignored (not L/D)
        ])
    conn.executemany(
        "INSERT INTO live_listing_events "
        "(sku, store_number, event_type, old_qty, new_qty, batch_id, prev_batch_id, event_date) "
        "VALUES (?,?,?,?,?,?,?,?)",
        [
            (SECOND, 504, 'LIVE_NEW_LISTING', None, 6, 'bat2', 'bat1', '2026-07-05'),
            (FOCUS, 501, 'LIVE_RESTOCK', 2, 8, 'bat2', 'bat1', '2026-07-05'),
        ])
    conn.commit()
    conn.close()


def test_backfill_folds_and_is_idempotent(app_module, client):
    _reset_ledger()
    _seed_sources()
    r1 = client.post('/api/listings/backfill')
    assert r1.status_code == 200, r1.get_data(as_text=True)
    b1 = r1.get_json()
    # 3 NEW_LISTING + 1 DROPPED (sod) + 1 LIVE_NEW_LISTING + 1 LIVE_RESTOCK (live)
    assert b1['by_source'].get('sod') == 4
    assert b1['by_source'].get('live') == 2
    ledger1 = b1['ledger_rows']

    # 502 was listed then dropped → DELISTED; 501/503 LISTED; 504 LISTED.
    assert _listing(FOCUS, 501)['status'] == 'LISTED'
    assert _listing(FOCUS, 502)['status'] == 'DELISTED'
    assert _listing(SECOND, 503)['status'] == 'LISTED'
    assert _listing(SECOND, 504)['status'] == 'LISTED'

    # Re-running is a no-op (UNIQUE guard) — same totals, no duplicate rows.
    r2 = client.post('/api/listings/backfill')
    assert r2.get_json()['ledger_rows'] == ledger1


def test_backfill_covers_pre_tracking_snapshot(app_module, client):
    """A store listed BEFORE per-store change tracking existed (present in the
    latest SOD snapshot but with no sod_store_sku_changes row) still enters
    the ledger on backfill."""
    _reset_ledger()
    conn = _db()
    conn.execute(
        "INSERT OR IGNORE INTO sod_inventory "
        "(sku, store_number, snapshot_date, status, on_hand, product_name) "
        "VALUES (?,?,?,?,?,?)",
        (FOCUS, 601, '2026-07-08', 'L', 12, 'ROCK PAPER RUM INDIAN SPICED'))
    conn.commit()
    conn.close()
    r = client.post('/api/listings/backfill')
    assert r.status_code == 200
    row = _listing(FOCUS, 601)
    assert row is not None
    assert row['status'] == 'LISTED'
    assert row['first_listed_date'] == '2026-07-08'
    # Idempotent.
    ledger1 = r.get_json()['ledger_rows']
    assert client.post('/api/listings/backfill').get_json()['ledger_rows'] == ledger1


def test_rebuild_is_pure_fold(app_module, client):
    _reset_ledger()
    _seed_sources()
    client.post('/api/listings/backfill')
    before = _snapshot_store_listings()
    r = client.post('/api/listings/rebuild')
    assert r.status_code == 200, r.get_data(as_text=True)
    after = _snapshot_store_listings()
    assert before == after
    # Rebuild touches only the derived cache, never the ledger.
    conn = _db()
    assert conn.execute("SELECT COUNT(*) FROM listing_ledger").fetchone()[0] == r.get_json()['ledger_rows']
    conn.close()


# ── E. THE SOD-LOSS GUARANTEE — the whole point ────────────────────────────
def test_sod_loss_guarantee(app_module, client):
    """Seed the ledger, snapshot store_listings, then wipe every SOD table and
    rebuild from the ledger alone — the materialized state must be identical."""
    _reset_ledger()
    _seed_sources()
    client.post('/api/listings/backfill')
    before = _snapshot_store_listings()
    assert before  # non-empty

    # Simulate TOTAL SOD loss.
    conn = _db()
    for t in ('sod_inventory', 'sod_store_sku_changes', 'sod_products',
              'sod_listing_changes'):
        try:
            conn.execute(f"DELETE FROM {t}")
        except Exception:
            pass
    conn.commit()
    conn.close()

    r = client.post('/api/listings/rebuild')
    assert r.status_code == 200
    after = _snapshot_store_listings()
    assert after == before  # every listing still known, first_listed_date intact


def test_sod_loss_guarantee_full_including_read_endpoints(app_module, client):
    _reset_ledger()
    _seed_sources()
    # add a rep + manual proof so the guarantee covers non-SOD sources too
    _record(app_module, FOCUS, 810, 'LISTED', 'rep', 'Namit', '2026-07-04')
    _record(app_module, SECOND, 811, 'LISTED', 'manual', 'known placement', '2026-07-02')
    client.post('/api/listings/backfill')

    before_sl = _snapshot_store_listings()
    before_listings = client.get('/api/listings?nocache=1').get_json()['rows']
    before_added = client.get(
        '/api/listings/added?since=2026-06-01&nocache=1').get_json()['rows']
    assert before_sl and before_listings and before_added

    # Simulate TOTAL SOD loss — delete every sod_* row in the scratch DB.
    conn = _db()
    for t in ('sod_inventory', 'sod_store_sku_changes', 'sod_products',
              'sod_listing_changes'):
        try:
            conn.execute(f"DELETE FROM {t}")
        except Exception:
            pass
    conn.commit()
    conn.close()

    r = client.post('/api/listings/rebuild')
    assert r.status_code == 200

    after_sl = _snapshot_store_listings()
    after_listings = client.get('/api/listings?nocache=1').get_json()['rows']
    after_added = client.get(
        '/api/listings/added?since=2026-06-01&nocache=1').get_json()['rows']

    # Every listing still known, first_listed_date intact, both read views equal.
    assert after_sl == before_sl
    assert after_listings == before_listings
    assert after_added == before_added
    # The rep + manual sources survived the SOD wipe (they were never in SOD).
    conn = _db()
    n = conn.execute(
        "SELECT COUNT(*) FROM store_listings WHERE store_number IN (810, 811)"
    ).fetchone()[0]
    conn.close()
    assert n == 2


# ── The immutability + recovery-scoping invariants (grep app.py) ───────────
def test_ledger_never_updated_or_deleted():
    src = open(APP_PY, encoding='utf-8').read()
    assert not re.search(r'UPDATE\s+listing_ledger', src, re.IGNORECASE)
    assert not re.search(r'DELETE\s+FROM\s+listing_ledger', src, re.IGNORECASE)


def test_savepoint_scoped_recovery_present():
    """The July-4 data-loss class: mid-ingest failures must roll back to a
    SAVEPOINT, never the whole transaction. All three folds + the listing-
    changes fallback are SAVEPOINT-scoped."""
    src = open(APP_PY, encoding='utf-8').read()
    assert src.count('SAVEPOINT') >= 3
    assert 'sp_listing_changes' in src
    assert 'sp_ledger' in src
    assert 'sp_ledger_live' in src


def test_no_coalesce_date_to_empty_string():
    src = open(APP_PY, encoding='utf-8').read()
    hits = re.findall(
        r"COALESCE\([^)]*(?:observed_date|change_date|event_date|snapshot_date|"
        r"first_listed_date|last_confirmed_date|delisted_date|recorded_at|"
        r"detected_at|created_at|updated_at)[^)]*,\s*''\s*\)",
        src, re.IGNORECASE)
    assert hits == [], f'COALESCE(date, \'\') crash class found: {hits}'


# ── Backup + retention completeness ────────────────────────────────────────
def test_export_tables_include_ledger_and_backup_set(app_module):
    names = {t for t, _ in app_module._EXPORT_TABLES}
    required = {
        'listing_ledger', 'store_listings', 'agco_licensees',
        'horeca_activities', 'lcbo_live_batches', 'lcbo_live_snapshots',
        'live_listing_events', 'activities', 'deals', 'horeca_accounts',
    }
    missing = required - names
    assert not missing, f'_EXPORT_TABLES missing: {sorted(missing)}'
    # PK sanity for the newly added tables.
    pks = dict(app_module._EXPORT_TABLES)
    assert pks['agco_licensees'] == 'licence_number'
    assert pks['listing_ledger'] == 'id'
    assert pks['store_listings'] == 'id'
    assert pks['lcbo_live_snapshots'] == 'id'
    assert pks['horeca_activities'] == 'id'


def test_retention_protected_membership(app_module):
    prot = set(app_module._RETENTION_PROTECTED_TABLES)
    for t in ('sod_inventory', 'lcbo_live_snapshots', 'activities',
              'listing_ledger', 'store_listings', 'agco_licensees',
              'horeca_accounts', 'deals', 'horeca_activities'):
        assert t in prot, f'{t} missing from _RETENTION_PROTECTED_TABLES'


def test_import_replace_never_truncates_protected_table(app_module, client):
    """?mode=replace&confirm=YES must fall back to merge for protected tables —
    the pre-existing ledger row survives the restore."""
    _reset_ledger()
    _record(app_module, FOCUS, 901, 'LISTED', 'manual', 'pre-restore', '2026-07-01')
    payload = {'tables': {'listing_ledger': {
        'columns': ['sku', 'store_number', 'event', 'source',
                    'source_detail', 'observed_date', 'note'],
        'rows': [{'sku': SECOND, 'store_number': 902, 'event': 'LISTED',
                  'source': 'manual', 'source_detail': 'restore',
                  'observed_date': '2026-07-02', 'note': ''}],
    }}}
    r = client.post('/api/admin/import?mode=replace&confirm=YES', json=payload)
    assert r.status_code == 200, r.get_data(as_text=True)
    res = r.get_json()['tables']['listing_ledger']
    assert res.get('note') == 'retention guard: protected table is never truncated'
    conn = _db()
    pre = conn.execute(
        "SELECT COUNT(*) FROM listing_ledger WHERE store_number=901").fetchone()[0]
    post = conn.execute(
        "SELECT COUNT(*) FROM listing_ledger WHERE store_number=902").fetchone()[0]
    conn.close()
    assert pre == 1   # the guard kept the existing history
    assert post == 1  # and the imported row still landed (merge)


def test_essential_email_backup_carries_ledger(app_module):
    _reset_ledger()
    _record(app_module, FOCUS, 900, 'LISTED', 'manual', 'x', '2026-07-01')
    with app_module.app.test_request_context('/'):
        payload = app_module._build_essential_backup()
    assert 'listing_ledger' in payload['tables']
    assert 'store_listings' in payload['tables']
    assert 'error' not in payload['tables']['listing_ledger']
    assert payload['tables']['listing_ledger']['row_count'] >= 1
    # the giant snapshot table stays out of the email (size), as designed
    assert 'sod_inventory' not in payload['tables']


# ── D. READ ENDPOINTS ──────────────────────────────────────────────────────
def test_api_listings_current_state(app_module, client):
    _reset_ledger()
    _seed_sources()
    client.post('/api/listings/backfill')
    code, body, _ = _json(client, '/api/listings?nocache=1')
    assert code == 200
    by = {(r['sku'], r['store_number']): r for r in body['rows']}
    assert by[(FOCUS, 501)]['status'] == 'LISTED'
    assert by[(FOCUS, 501)]['first_listed_date'] == '2026-07-01'
    assert by[(FOCUS, 502)]['status'] == 'DELISTED'
    # brand + product_name resolved, days_since_confirmed present (int or None)
    assert by[(FOCUS, 501)]['brand'] == 'Rock Paper'
    assert 'days_since_confirmed' in by[(FOCUS, 501)]
    # summary
    s = body['summary']
    assert s['listed'] >= 1 and s['delisted'] >= 1
    assert s['first_ever'] == '2026-07-01'
    assert 'sod' in s['by_source']


def test_api_listings_status_filter(app_module, client):
    _reset_ledger()
    _seed_sources()
    client.post('/api/listings/backfill')
    code, body, _ = _json(client, '/api/listings?status=DELISTED&nocache=1')
    assert code == 200
    assert body['count'] >= 1
    assert all(r['status'] == 'DELISTED' for r in body['rows'])


def test_api_listings_added_window(app_module, client):
    _reset_ledger()
    _record(app_module, FOCUS, 611, 'LISTED', 'sod', '2026-07-10', '2026-07-10')
    _record(app_module, SECOND, 612, 'LISTED', 'live', 'b9', '2026-07-20')
    code, body, _ = _json(client, '/api/listings/added?since=2026-07-01&nocache=1')
    assert code == 200
    stores = {r['store_number'] for r in body['rows']}
    assert {611, 612} <= stores
    # newest first
    assert body['rows'][0]['observed_date'] >= body['rows'][-1]['observed_date']
    assert body['summary']['by_source'].get('sod', 0) >= 1
    assert body['summary']['by_source'].get('live', 0) >= 1


def test_api_listings_added_reads_ledger_not_sod(app_module, client):
    """/added must answer from the ledger even with SOD wiped."""
    _reset_ledger()
    _record(app_module, FOCUS, 610, 'LISTED', 'manual', 'known', '2026-07-05')
    conn = _db()
    for t in ('sod_inventory', 'sod_store_sku_changes', 'sod_products'):
        try:
            conn.execute(f"DELETE FROM {t}")
        except Exception:
            pass
    conn.commit()
    conn.close()
    code, body, _ = _json(client, '/api/listings/added?since=2026-07-01&nocache=1')
    assert code == 200
    assert any(r['store_number'] == 610 for r in body['rows'])


def test_api_listings_store_timeline(app_module, client):
    _reset_ledger()
    _record(app_module, FOCUS, 700, 'LISTED', 'sod', '2026-07-01', '2026-07-01')
    _record(app_module, FOCUS, 700, 'RECONFIRMED', 'live', 'b1', '2026-07-05')
    code, body, _ = _json(client, '/api/listings/store/700')
    assert code == 200
    assert body['store_number'] == 700
    assert body['event_count'] == 2
    assert len(body['current']) == 1
    assert body['current'][0]['status'] == 'LISTED'
    events = {(e['event'], e['source']) for e in body['events']}
    assert ('LISTED', 'sod') in events
    assert ('RECONFIRMED', 'live') in events


def test_api_listings_ledger_stream(app_module, client):
    _reset_ledger()
    _record(app_module, FOCUS, 710, 'LISTED', 'sod', '2026-07-01', '2026-07-01')
    _record(app_module, FOCUS, 710, 'DELISTED', 'sod', '2026-07-10', '2026-07-10')
    code, body, _ = _json(client, '/api/listings/ledger?days=3650&nocache=1')
    assert code == 200
    assert body['by_event'].get('LISTED', 0) >= 1
    assert body['by_event'].get('DELISTED', 0) >= 1
    # sku filter
    code2, body2, _ = _json(client, f'/api/listings/ledger?sku={FOCUS}&days=3650')
    assert code2 == 200
    assert all(r['sku'] == FOCUS for r in body2['rows'])


def test_api_source_health_staleness(app_module, client):
    _reset_ledger()
    today = app_module._toronto_today()
    from datetime import timedelta
    old = (today - timedelta(days=10)).isoformat()
    fresh = (today - timedelta(days=1)).isoformat()
    # sod last seen 10 days ago → stale; live seen yesterday → fresh
    _record(app_module, FOCUS, 720, 'LISTED', 'sod', old, old)
    _record(app_module, FOCUS, 721, 'LISTED', 'live', 'b1', fresh)
    code, body, _ = _json(client, '/api/listings/source-health')
    assert code == 200
    by = {s['source']: s for s in body['sources']}
    assert by['sod']['last_observed_date'] == old
    assert by['sod']['is_stale'] is True
    assert by['live']['is_stale'] is False
    # rep/manual are ad-hoc — never flagged stale even with no rows
    assert by['rep']['is_stale'] is False
    assert body['any_stale'] is True and 'sod' in body['stale_sources']


def test_export_listings_xlsx(app_module, client):
    _reset_ledger()
    _seed_sources()
    client.post('/api/listings/backfill')
    r = client.get('/api/export/listings.xlsx')
    assert r.status_code == 200
    assert r.data[:2] == b'PK'  # xlsx is a zip container
    assert 'attachment' in r.headers.get('Content-Disposition', '')
    assert 'anu_listings_' in r.headers.get('Content-Disposition', '')


# ── Caching: heavy reads cache + invalidate on write ───────────────────────
def test_api_listings_is_cached_and_invalidated(app_module, client):
    _reset_ledger()
    app_module._cache_store.clear()
    _record(app_module, FOCUS, 730, 'LISTED', 'sod', '2026-07-01', '2026-07-01')
    r1 = client.get('/api/listings')
    assert r1.headers.get('X-Cache') == 'MISS'
    r2 = client.get('/api/listings')
    assert r2.headers.get('X-Cache') == 'HIT'
    # A manual record must invalidate the cache (next read reflects it).
    client.post('/api/listings/record', json={
        'sku': FOCUS, 'store_number': 731, 'event': 'LISTED',
        'observed_date': '2026-07-02'})
    r3 = client.get('/api/listings')
    assert r3.headers.get('X-Cache') == 'MISS'
    assert any(row['store_number'] == 731 for row in r3.get_json()['rows'])


# ── Empty-data safety: no endpoint 500s on an empty ledger ─────────────────
def test_read_endpoints_empty_data_no_500(app_module, client):
    _reset_ledger()
    for path in ('/api/listings?nocache=1', '/api/listings/added?nocache=1',
                 '/api/listings/store/99999', '/api/listings/ledger?nocache=1',
                 '/api/listings/source-health'):
        code, body, _ = _json(client, path)
        assert code == 200, f'{path} -> {code}'
    r = client.get('/api/export/listings.xlsx')
    assert r.status_code == 200 and r.data[:2] == b'PK'
