"""Live lcbo.com engine tests — append-only snapshots, listing events,
latest/series endpoints, and the SOD-vs-live reconcile (2-way on this fork).

Ported from the proven Dripp Tracker engine tests (drippcan-tracker
tests/test_dripp.py TestLiveEngine). Tracking accuracy IS the product:
  - live snapshots are APPEND-ONLY (a new batch never touches old rows),
  - listing events (new/restock/delisted) are detected between batches,
  - duplicate store rows (lcbo.com renders each store block twice) collapse
    to ONE snapshot row per (sku, store) per batch,
  - a failed scrape records the error and touches nothing,
  - /api/live/latest returns ONLY the newest batch,
  - /api/reconcile flags MATCH / SOD_LAGS_LIVE / LIVE_LAGS_SOD /
    MISSING_FROM_SOD / MISSING_FROM_LIVE with rep columns null (no per-SKU
    rep unit counts exist in this fork's schema).

Run with: python3 -m pytest tests/test_live_engine.py -v
"""
import os
import sqlite3
import sys
import tempfile

import pytest

# Force SQLite for tests so we never touch production Postgres.
os.environ.pop('DATABASE_URL', None)
os.environ.pop('ADMIN_TOKEN', None)
os.environ.pop('API_KEY', None)
os.environ.pop('SOD_CRON_TOKEN', None)
_TMP = tempfile.mkdtemp(prefix='anu_imports_live_engine_test_')
os.environ['DB_DIR'] = _TMP

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

# Focus SKU for event/series assertions — Rock Paper Rum, live at LCBO.
FOCUS = '0045378'


@pytest.fixture(scope='module')
def app_module():
    """Import app.py fresh against an isolated SQLite file.

    DB_DIR is re-asserted here (not only at module import) because pytest
    imports every test module during collection and the LAST module import
    wins the env var. Restored on teardown so other test modules are
    unaffected.
    """
    prev_db_dir = os.environ.get('DB_DIR')
    os.environ['DB_DIR'] = _TMP
    for mod in list(sys.modules):
        if mod == 'app' or mod.startswith('app.'):
            del sys.modules[mod]
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        'app', os.path.join(os.path.dirname(__file__), '..', 'app.py'))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
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


def _fake_scraper(per_sku, default=((1, 5),)):
    """Build a _live_scrape_sku stand-in from {padded_sku: [(store, qty), ...]}.

    SKUs not in per_sku get `default` rows (the engine iterates ALL 9 tracked
    SKUs dynamically — every SKU must answer or the batch status degrades to
    'partial'). Pass default=None to make unknown SKUs fail like a delisting.
    """
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


BATCH1 = {FOCUS: [(1, 6), (2, 10), (3, 4)]}
BATCH2 = {FOCUS: [(1, 6), (2, 15), (4, 8)]}


class TestLiveEngine:
    def test_two_batches_are_append_only(self, app_module, client, monkeypatch):
        n_skus = len(app_module.SOD_TRACKED_SKUS)
        monkeypatch.setattr(app_module, 'LIVE_SCRAPE_GAP_SECONDS', 0)
        monkeypatch.setattr(app_module, '_live_scrape_sku', _fake_scraper(BATCH1))
        s1 = app_module.run_live_batch(triggered_by='test')
        assert s1['status'] == 'ok'
        assert s1['skus'] == sorted(app_module.SOD_TRACKED_SKUS.keys())  # dynamic, all 9
        assert s1['row_count'] == 3 + (n_skus - 1)  # FOCUS 3 rows, others 1 default row

        # Second batch via the on-demand endpoint (admin: localhost dev mode).
        # ?wait=1 = synchronous mode (the default is now fire-and-forget on a
        # background thread so the slow real scrape can't starve the web worker).
        monkeypatch.setattr(app_module, '_live_scrape_sku', _fake_scraper(BATCH2))
        resp = client.post('/api/live/refresh?wait=1')
        assert resp.status_code == 200
        s2 = resp.get_json()
        assert s2['status'] == 'ok'

        db = _db()
        total = db.execute('SELECT COUNT(*) FROM lcbo_live_snapshots').fetchone()[0]
        # batch 2 did NOT update/delete batch 1 rows
        assert total == s1['row_count'] + s2['row_count']
        batches = db.execute(
            "SELECT COUNT(DISTINCT batch_id) FROM lcbo_live_snapshots").fetchone()[0]
        assert batches == 2
        # Batch 1 rows still intact and unchanged
        old_qty = db.execute(
            "SELECT qty FROM lcbo_live_snapshots WHERE sku=? AND store_number=2 AND batch_id=?",
            (FOCUS, s1['batch_id'])).fetchone()[0]
        assert old_qty == 10
        db.close()

    def test_live_listing_events_detected_between_batches(self, app_module):
        db = _db()
        events = {
            (r['event_type'], r['store_number']): r
            for r in db.execute(
                "SELECT event_type, store_number, old_qty, new_qty "
                "FROM live_listing_events WHERE sku=?", (FOCUS,)).fetchall()
        }
        other_sku_events = db.execute(
            "SELECT COUNT(*) FROM live_listing_events WHERE sku != ?",
            (FOCUS,)).fetchone()[0]
        db.close()
        assert ('LIVE_NEW_LISTING', 4) in events        # store appeared
        assert events[('LIVE_NEW_LISTING', 4)]['new_qty'] == 8
        assert ('LIVE_RESTOCK', 2) in events            # 10 -> 15
        assert events[('LIVE_RESTOCK', 2)]['old_qty'] == 10
        assert events[('LIVE_RESTOCK', 2)]['new_qty'] == 15
        assert ('LIVE_DELISTED', 3) in events           # store disappeared
        # Unchanged store never generates noise
        assert ('LIVE_RESTOCK', 1) not in events
        # The 8 unchanged default-row SKUs generate zero events
        assert other_sku_events == 0

    def test_live_latest_returns_only_newest_batch(self, client):
        body = client.get(f'/api/live/latest?sku={FOCUS}&nocache=1').get_json()
        sku_block = body['skus'][FOCUS]
        by_store = {s['store_number']: s['qty'] for s in sku_block['stores']}
        assert by_store == {1: 6, 2: 15, 4: 8}  # batch 2 view; store 3 gone
        assert sku_block['total_units'] == 29
        assert sku_block['checked_at']
        assert sku_block['brand'] == 'Rock Paper'

    def test_live_store_time_series(self, client):
        body = client.get(f'/api/live/store/2?sku={FOCUS}&nocache=1').get_json()
        qtys = [p['qty'] for p in body['series']]
        assert qtys == [10, 15]  # both batches preserved, oldest first

    def test_failed_scrape_records_error_and_touches_nothing(self, app_module, monkeypatch):
        db = _db()
        before = db.execute('SELECT COUNT(*) FROM lcbo_live_snapshots').fetchone()[0]
        db.close()

        monkeypatch.setattr(app_module, 'LIVE_SCRAPE_GAP_SECONDS', 0)
        monkeypatch.setattr(app_module, '_live_scrape_sku',
                            lambda sku: ([], 'scrape error: boom'))
        summary = app_module.run_live_batch(triggered_by='test')
        assert summary['status'] == 'error'

        db = _db()
        after = db.execute('SELECT COUNT(*) FROM lcbo_live_snapshots').fetchone()[0]
        assert after == before  # append-only: failure adds nothing, changes nothing
        batch = db.execute(
            "SELECT status, error FROM lcbo_live_batches WHERE batch_id=?",
            (summary['batch_id'],)).fetchone()
        assert batch['status'] == 'error'
        assert 'boom' in batch['error']
        db.close()

    def test_batch_dedupes_duplicate_store_rows(self, app_module, monkeypatch):
        """lcbo.com renders each store block twice on the storeinventory page
        (verified live 2026-07-14 on the Dripp engine) — one snapshot row per
        (sku, store) per batch, or every inventory count doubles."""
        dup_rows = {FOCUS: [(901, 4), (901, 4), (902, 2)]}
        monkeypatch.setattr(app_module, 'LIVE_SCRAPE_GAP_SECONDS', 0)
        monkeypatch.setattr(app_module, '_live_scrape_sku',
                            _fake_scraper(dup_rows, default=((901, 3), (901, 3))))
        summary = app_module.run_live_batch(triggered_by='test')
        assert summary['status'] == 'ok'
        n_skus = len(app_module.SOD_TRACKED_SKUS)
        assert summary['row_count'] == 2 + (n_skus - 1)  # dupes collapsed everywhere

        db = _db()
        rows = db.execute(
            "SELECT sku, store_number, COUNT(*) c FROM lcbo_live_snapshots "
            "WHERE batch_id=? GROUP BY sku, store_number",
            (summary['batch_id'],)).fetchall()
        focus_stores = [r['store_number'] for r in rows if r['sku'] == FOCUS]
        db.close()
        assert sorted(focus_stores) == [901, 902]
        assert all(r['c'] == 1 for r in rows)


# ---------------------------------------------------------------------------
# Reconcile — 2-way (SOD vs lcbo.com). Rep columns null: this fork's schema
# records per-SKU visit OUTCOMES (activity_sku_outcomes), not unit counts.
# ---------------------------------------------------------------------------

class TestReconcile:
    @pytest.fixture(scope='class')
    def scenario(self, app_module):
        """Five stores, one per flag state, for the focus SKU."""
        stores = {'match': 5001, 'sod_lags': 5002, 'live_lags': 5003,
                  'no_sod': 5004, 'no_live': 5005}
        today = app_module._toronto_today().isoformat()
        db = _db()
        # SOD latest snapshot (no row for no_sod)
        for key, on_hand in (('match', 6), ('sod_lags', 2), ('live_lags', 9),
                             ('no_live', 7)):
            db.execute(
                "INSERT OR IGNORE INTO sod_inventory "
                "(sku, store_number, snapshot_date, status, on_hand, product_name) "
                "VALUES (?,?,?,?,?,?)",
                (FOCUS, stores[key], today, 'L', on_hand,
                 'ROCK PAPER RUM INDIAN SPICED'))
        db.commit()
        db.close()
        return stores

    def test_all_five_flags(self, scenario, app_module, client, monkeypatch):
        # Live batch covering the scenario (no row for no_live)
        live_rows = {FOCUS: [
            (scenario['match'], 6), (scenario['sod_lags'], 8),
            (scenario['live_lags'], 4), (scenario['no_sod'], 5),
        ]}
        monkeypatch.setattr(app_module, 'LIVE_SCRAPE_GAP_SECONDS', 0)
        monkeypatch.setattr(app_module, '_live_scrape_sku', _fake_scraper(live_rows))
        assert app_module.run_live_batch(triggered_by='test')['status'] == 'ok'

        body = client.get(f'/api/reconcile?days=7&sku={FOCUS}&nocache=1').get_json()
        flags = {r['store_number']: r for r in body['rows']}
        assert flags[scenario['match']]['flag'] == 'MATCH'
        assert flags[scenario['sod_lags']]['flag'] == 'SOD_LAGS_LIVE'
        assert flags[scenario['live_lags']]['flag'] == 'LIVE_LAGS_SOD'
        assert flags[scenario['no_sod']]['flag'] == 'MISSING_FROM_SOD'
        assert flags[scenario['no_live']]['flag'] == 'MISSING_FROM_LIVE'

        # A diff is never hidden: raw values + deltas + timestamps ride along
        lag_row = flags[scenario['sod_lags']]
        assert lag_row['sod_on_hand'] == 2 and lag_row['live_qty'] == 8
        assert lag_row['delta_sod_live'] == -6
        assert lag_row['live_checked_at'] and lag_row['sod_snapshot_date']

        # 2-way on this fork: rep columns ship null, REP_MISMATCH never fires
        for row in body['rows']:
            assert row['rep_units'] is None
            assert row['rep_on_shelf'] is None
            assert row['rep_observed_at'] is None
            assert row['rep'] is None
            assert row['delta_rep_live'] is None
            assert row['flag'] != 'REP_MISMATCH'
        assert '2-way' in body['mode']

        # Every source's last-checked timestamp is surfaced
        src = body['sources'][FOCUS]
        assert src['sod_latest_snapshot'] and src['live_checked_at']
        # Summary counts every flag
        assert sum(body['summary'].values()) == len(body['rows'])

    def test_reconcile_never_500_on_empty_data(self, app_module, client):
        resp = client.get('/api/reconcile?days=abc&sku=9999999&nocache=1')
        assert resp.status_code == 200


def test_partial_batch_survives_a_midbatch_kill(app_module, monkeypatch):
    """A batch torn down mid-loop (free-tier instance recycle: idle spindown, or
    a health-check restart while the single gunicorn worker is busy scraping)
    must still leave every COMPLETED SKU's 'live' ledger rows durably committed.

    Regression for the single-end-of-batch-commit bug that left the live source
    at 0 rows on prod while SOD folded fine: the instance was recycled before the
    one final commit was ever reached, so every snapshot and ledger fold was lost
    and the batch was orphaned 'running'. The fix commits each SKU as it lands.
    """
    monkeypatch.setattr(app_module, 'LIVE_SCRAPE_GAP_SECONDS', 0)

    # Start from a clean ledger so the count is unambiguous (DELETE in a TEST is
    # fine — the immutability guard only greps app.py, never the tests).
    db0 = _db()
    app_module._ensure_live_tables(db0)      # lazy DDL — tables may not exist yet
    app_module._ensure_listing_ledger(db0)
    for t in ('listing_ledger', 'store_listings', 'lcbo_live_snapshots',
              'lcbo_live_batches', 'live_listing_events'):
        db0.execute(f"DELETE FROM {t}")
    db0.commit()
    db0.close()

    KILL_AFTER = 2  # the first 2 SKUs land; the 3rd scrape "kills" the instance
    calls = {'n': 0}

    def killer_scraper(sku):
        calls['n'] += 1
        if calls['n'] > KILL_AFTER:
            # SystemExit is NOT caught by run_live_batch's `except Exception`,
            # so it propagates exactly like a real process teardown.
            raise SystemExit('simulated instance recycle mid-batch')
        sn = 100 + calls['n']  # distinct store per SKU → one 'live' RECONFIRMED
        return ([{'store_number': str(sn), 'city': 'Toronto',
                  'intersection': 'X', 'store_name': f'S{sn}',
                  'address': f'{sn} St', 'phone': '', 'quantity': 5}], None)

    monkeypatch.setattr(app_module, '_live_scrape_sku', killer_scraper)

    with pytest.raises(SystemExit):
        app_module.run_live_batch(triggered_by='test')

    db = _db()
    live_rows = db.execute(
        "SELECT COUNT(*) FROM listing_ledger WHERE source='live'").fetchone()[0]
    live_skus = db.execute(
        "SELECT COUNT(DISTINCT sku) FROM listing_ledger WHERE source='live'"
    ).fetchone()[0]
    db.close()

    # Before the fix this was 0 (nothing committed before the single end-of-batch
    # commit the kill never reached). After the fix the 2 completed SKUs persist.
    assert live_rows == KILL_AFTER, f'expected {KILL_AFTER} live rows, got {live_rows}'
    assert live_skus == KILL_AFTER


def test_refresh_default_is_async_and_folds_in_background(app_module, client, monkeypatch):
    """POST /api/live/refresh (no ?wait) returns 202 'started' immediately and runs
    the batch OFF the web worker so the slow real scrape can't starve /healthz and
    get the instance killed. The background batch still folds 'live' ledger rows."""
    import time as _t
    monkeypatch.setattr(app_module, 'LIVE_SCRAPE_GAP_SECONDS', 0)
    monkeypatch.setattr(app_module, '_live_scrape_sku', _fake_scraper({FOCUS: [(1, 6)]}))

    db0 = _db()
    app_module._ensure_live_tables(db0)      # lazy DDL — tables may not exist yet
    app_module._ensure_listing_ledger(db0)
    for t in ('listing_ledger', 'store_listings', 'lcbo_live_snapshots',
              'lcbo_live_batches', 'live_listing_events'):
        db0.execute(f"DELETE FROM {t}")
    db0.commit()
    db0.close()

    resp = client.post('/api/live/refresh')
    assert resp.status_code == 202
    assert resp.get_json()['status'] == 'started'

    # Wait for the background daemon batch to finish (lock free = done).
    for _ in range(200):
        if app_module._live_batch_lock.acquire(blocking=False):
            app_module._live_batch_lock.release()
            break
        _t.sleep(0.02)

    db = _db()
    ok = db.execute(
        "SELECT COUNT(*) FROM lcbo_live_batches WHERE status='ok'").fetchone()[0]
    live_rows = db.execute(
        "SELECT COUNT(*) FROM listing_ledger WHERE source='live'").fetchone()[0]
    db.close()
    assert ok >= 1, 'background batch should have completed with status ok'
    assert live_rows > 0, 'background batch should fold live ledger rows'
