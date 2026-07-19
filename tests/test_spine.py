"""ALWAYS-ON SPINE — permanent store coverage + job heartbeat.

Two promises, both learned the hard way:
  1. Every store touch is recorded FOREVER and folding twice cannot
     double-count it.
  2. A background job that dies is LOUD, not silent. The daily backup in this
     codebase once failed for weeks because nothing checked that it ran.

Run: python3 -m pytest tests/test_spine.py -v
"""
import os
import sys
import tempfile

import pytest

os.environ.pop('DATABASE_URL', None)
os.environ.pop('ADMIN_TOKEN', None)
os.environ.pop('API_KEY', None)
_TMP = tempfile.mkdtemp(prefix='anu_spine_test_')
os.environ['DB_DIR'] = _TMP

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))


@pytest.fixture(scope='module')
def app_module():
    for m in list(sys.modules):
        if m == 'app' or m.startswith('app.'):
            del sys.modules[m]
    os.environ['DB_DIR'] = _TMP
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        'app', os.path.join(os.path.dirname(__file__), '..', 'app.py'))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def client(app_module):
    app_module._rate_buckets.clear()
    return app_module.app.test_client()


@pytest.fixture(scope='module')
def seeded(app_module):
    with app_module.app.app_context():
        db = app_module.get_db()
        app_module._ensure_spine(db)
        db.execute("INSERT OR IGNORE INTO stores (store_number, account, city) "
                   "VALUES (555,'Airport & Bovaird','Brampton')")
        db.execute("INSERT OR IGNORE INTO stores (store_number, account, city) "
                   "VALUES (753,'Simcoe North','Oshawa')")
        db.commit()
        # activities keys on stores.id and reps.id, NOT store_number/rep_name
        for sn, rep, t in ((555, 'Namit', 'visit'), (555, 'Namit', 'tasting'),
                           (753, 'Ikshit', 'visit')):
            sid = db.execute("SELECT id FROM stores WHERE store_number=?",
                             (sn,)).fetchone()[0]
            rr = db.execute("SELECT id FROM reps WHERE name=?", (rep,)).fetchone()
            rid = rr[0] if rr else 1
            db.execute("INSERT INTO activities (store_id, rep_id, "
                       "activity_type, created_at) VALUES (?,?,?,'2026-07-18')",
                       (sid, rid, t))
        db.commit()
    return True


class TestPermanentCoverage:
    def test_fold_records_every_touch(self, seeded, client, app_module):
        with app_module.app.app_context():
            db = app_module.get_db()
            added = app_module._fold_store_coverage(db)
        assert added >= 3
        cov = client.get('/api/system/coverage').get_json()
        assert cov['stores_touched'] == 2
        nums = {r['store_number'] for r in cov['rows']}
        assert nums == {555, 753}
        assert all('Brampton' in r['store_label'] or 'Oshawa' in r['store_label']
                   for r in cov['rows'])

    def test_folding_twice_cannot_double_count(self, seeded, client,
                                               app_module):
        before = client.get('/api/system/coverage').get_json()
        with app_module.app.app_context():
            db = app_module.get_db()
            app_module._fold_store_coverage(db)
            app_module._fold_store_coverage(db)
        after = client.get('/api/system/coverage').get_json()
        b = {r['store_number']: r['touches'] for r in before['rows']}
        a = {r['store_number']: r['touches'] for r in after['rows']}
        assert a == b, 'refolding created duplicate coverage rows'

    def test_coverage_is_append_only_in_practice(self, app_module):
        # No code path may DELETE from the permanent ledgers.
        src = open(os.path.join(os.path.dirname(__file__), '..',
                                'app.py')).read().lower()
        for table in ('store_coverage', 'outreach_suppression',
                      'listing_ledger'):
            assert f'delete from {table}' not in src, \
                f'something deletes from {table}, which must be permanent'

    def test_scheduler_safe_fold_works_without_app_context(self, seeded,
                                                           app_module):
        # The bare-thread path: no Flask context at all. This is the exact
        # shape that silently killed the backup job.
        conn = app_module._sod_get_conn()
        try:
            app_module._fold_store_coverage_conn(conn)   # must not raise
        finally:
            conn.close()


class TestHeartbeat:
    def test_never_run_job_is_reported_loudly(self, client):
        hb = client.get('/api/system/heartbeat').get_json()
        assert hb['overall'].startswith('ATTENTION')
        states = {j['job']: j['state'] for j in hb['jobs']}
        assert states['backup'] == 'NEVER RAN'

    def test_job_checkin_turns_it_green(self, client, app_module):
        with app_module.app.app_context():
            db = app_module.get_db()
            app_module._heartbeat(db, 'backup', True, 'test run')
        hb = client.get('/api/system/heartbeat').get_json()
        states = {j['job']: j['state'] for j in hb['jobs']}
        assert states['backup'] == 'OK'

    def test_failed_job_is_flagged_not_hidden(self, client, app_module):
        with app_module.app.app_context():
            db = app_module.get_db()
            app_module._heartbeat(db, 'live_scrape', False, 'scrape blew up')
        hb = client.get('/api/system/heartbeat').get_json()
        states = {j['job']: j['state'] for j in hb['jobs']}
        assert states['live_scrape'] == 'FAILED'
        assert hb['overall'].startswith('ATTENTION')

    def test_permanent_counts_are_reported(self, seeded, client, app_module):
        # Self-sufficient: fold here rather than leaning on another test's
        # side effect, so this passes in any run order.
        with app_module.app.app_context():
            app_module._fold_store_coverage(app_module.get_db())
        hb = client.get('/api/system/heartbeat').get_json()
        p = hb['permanent_records']
        assert p['stores_ever_touched'] >= 2
        assert p['store_touches_logged'] >= 3
        # -1 means the table does not exist in this environment, which is a
        # real signal, not a silent zero. It must never be reported as 0.
        assert all(v == -1 or v >= 0 for v in p.values())

    def test_heartbeat_survives_a_bare_thread(self, app_module):
        # Recording a heartbeat must work from a scheduler thread, or the
        # whole watchdog is theatre.
        import threading
        errors = []

        def worker():
            try:
                conn = app_module._sod_get_conn()
                app_module._ensure_spine(conn)
                app_module._heartbeat(conn, 'autopilot', True, 'from thread')
                conn.close()
            except Exception as e:      # pragma: no cover
                errors.append(e)

        t = threading.Thread(target=worker)
        t.start()
        t.join()
        assert not errors, f'heartbeat failed in a bare thread: {errors}'


class TestBackupCoverageCannotDrift:
    """The audit found 14 tables silently absent from the backup, including
    the do-not-contact list and the reps' commission credits. The old test
    checked an allowlist, so it could only catch REMOVALS, never a new table
    that nobody added. This one enumerates the real schema instead."""

    ALLOWED_UNBACKED = {
        # pure derived caches, rebuilt on demand
        'inventory_cache', 'daily_plan_cache',
        # sqlite/pg internals
        'sqlite_sequence',
    }

    def test_every_table_is_either_backed_up_or_explicitly_excused(
            self, seeded, app_module):
        with app_module.app.app_context():
            db = app_module.get_db()
            app_module._ensure_spine(db)
            app_module._ensure_outreach(db)
            app_module._ensure_sales_cmd(db)
            app_module._ensure_horeca_v2(db)
            app_module._ensure_listing_ledger(db)
            live = {r[0] for r in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        exported = {t for t, _pk in app_module._EXPORT_TABLES}
        missing = live - exported - self.ALLOWED_UNBACKED
        assert not missing, (
            'these tables exist but are backed up NOWHERE: '
            + ', '.join(sorted(missing)))

    def test_irreplaceable_tables_survive_a_replace_restore(self, app_module):
        prot = app_module._RETENTION_PROTECTED_TABLES
        for t in ('outreach_suppression', 'outreach_consent', 'outreach_log',
                  'commission_credits', 'store_coverage', 'listing_ledger'):
            assert t in prot, f'{t} could be truncated by a replace restore'

    def test_partial_backup_is_reported_as_a_failure(self, seeded, app_module):
        with app_module.app.app_context():
            db = app_module.get_db()
            app_module._ensure_spine(db)
            app_module._ensure_outreach(db)
            app_module._ensure_sales_cmd(db)
            app_module._ensure_listing_ledger(db)
            payload = app_module._build_essential_backup()
        assert 'errors' in payload, 'backup has no failure channel'
        # every crown-jewel table must be present and error-free
        for critical in ('listing_ledger', 'activities',
                         'outreach_suppression', 'commission_credits'):
            t = payload['tables'].get(critical)
            assert t is not None, f'{critical} absent from backup payload'
            assert 'error' not in t, f'{critical} failed to export'
        # The channel must actually capture failures rather than swallow
        # them: any table that could not be read has to appear here, and a
        # crown-jewel failure must be called out by name.
        for e in payload['errors']:
            assert ':' in e or 'CRITICAL' in e
        assert not [e for e in payload['errors'] if 'CRITICAL' in e], \
            f'a crown-jewel table failed to back up: {payload["errors"]}'
