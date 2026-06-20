"""RPR Tasting Blitz + schedule + HORECA radius finder + report snapshots.

Covers the /api/rpr/* contract the shipped frontend calls, plus the new
sales tools. Network (Nominatim/Overpass/Resend) is never hit in tests.

Run with: pytest tests/test_rpr_blitz.py -v
"""
import os
import sqlite3
import sys
import tempfile

import pytest

os.environ.pop('DATABASE_URL', None)
_TMP = tempfile.mkdtemp(prefix='lcbo_rpr_test_')
os.environ['DB_DIR'] = _TMP
TEST_DB = os.path.join(_TMP, 'anu_imports.db')

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))


@pytest.fixture(scope='module')
def app_module():
    # Re-assert our isolated DB dir right before import — sibling test modules
    # also set DB_DIR at collection time, so the env value is non-deterministic
    # by the time this fixture runs. app.py reads DB_DIR at import.
    os.environ['DB_DIR'] = _TMP
    os.environ.pop('DATABASE_URL', None)
    for mod in list(sys.modules):
        if mod == 'app' or mod.startswith('app.'):
            del sys.modules[mod]
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        'app', os.path.join(os.path.dirname(__file__), '..', 'app.py'))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.fixture(scope='module')
def seeded(app_module):
    """A handful of stores with coords so ingest can geocode from stores_table,
    plus HORECA accounts for the radius finder. Writes to the path the app
    actually resolved (app_module.DB_PATH), not an assumed one."""
    conn = sqlite3.connect(app_module.DB_PATH)
    # give a couple of seed stores real coords (rest fall back to centroid)
    coords = {201: (43.8505, -79.0209), 191: (43.8800, -79.0200),
              390: (43.857, -79.337), 773: (43.6858, -79.7599)}
    for sn, (lat, lng) in coords.items():
        conn.execute("INSERT OR IGNORE INTO stores (store_number, account, city, lat, lng) "
                     "VALUES (?,?,?,?,?)", (sn, f'LCBO #{sn}', 'Test', lat, lng))
        conn.execute("UPDATE stores SET lat=?, lng=? WHERE store_number=?", (lat, lng, sn))
    conn.execute("INSERT INTO horeca_accounts (name, account_type, status, address, city, "
                 "postal, phone, lat, lng, source) VALUES "
                 "('Drake Hotel','restaurant','active','1150 Queen St W','Toronto','M6J1J3','416','%s','%s','manual')"
                 % (43.6432, -79.4256))
    conn.execute("INSERT INTO horeca_accounts (name, account_type, status, address, city, "
                 "postal, phone, lat, lng, source) VALUES "
                 "('Far Bar','bar','prospect','1 North St','Barrie','L4M','705','%s','%s','manual')"
                 % (44.389, -79.690))
    conn.commit()
    conn.close()
    return True


@pytest.fixture
def client(app_module):
    return app_module.app.test_client()


# ---- clustering (pure) ----
class TestClustering:
    def test_every_store_clustered_sizes_bounded(self, app_module):
        pts = [{'store_number': i, 'lat': 43.6 + (i % 10) * 0.01,
                'lng': -79.4 + (i // 10) * 0.01, 'city': 'Toronto'} for i in range(23)]
        clusters = app_module._rpr_cluster(pts, max_km=18, target=5)
        assert all(p.get('cluster_id') for p in pts)
        assert all(1 <= len(c['members']) <= 6 for c in clusters)
        # seq_in_cluster is 1-based contiguous within each cluster
        for c in clusters:
            seqs = sorted(m['seq_in_cluster'] for m in c['members'])
            assert seqs == list(range(1, len(seqs) + 1))

    def test_unmapped_stores_go_to_trailing_cluster(self, app_module):
        pts = [{'store_number': 1, 'lat': 43.6, 'lng': -79.4, 'city': 'A'},
               {'store_number': 2, 'lat': 0, 'lng': 0, 'city': 'B'}]
        clusters = app_module._rpr_cluster(pts)
        assert any(c['name'].startswith('Unmapped') for c in clusters)


# ---- ingest + blitz ----
class TestIngestBlitz:
    def test_ingest_loads_148_and_clusters(self, client, seeded):
        r = client.post('/api/rpr/ingest', json={})
        assert r.status_code == 200, r.get_data(as_text=True)
        d = r.get_json()
        assert d['ok'] is True
        assert d['stores'] == 148
        assert d['clustered'] == 148
        assert d['geocoded_from_stores_table'] >= 4  # our seeded coords

    def test_blitz_payload_shape(self, client, seeded):
        client.post('/api/rpr/ingest', json={})
        d = client.get('/api/rpr/blitz?nocache=1').get_json()
        assert d['ingested'] is True
        assert d['campaign'] == 'rpr_45378' and d['sku'] == '0045378'
        assert d['totals']['stores'] == 148
        assert d['clusters']
        c = d['clusters'][0]
        for k in ('cluster_id', 'name', 'store_count', 'suggested_rep', 'stores'):
            assert k in c
        s = c['stores'][0]
        for k in ('store_number', 'account_label', 'status', 'seq_in_cluster', 'photo_count'):
            assert k in s

    def test_ingest_idempotent_keeps_logs(self, client, seeded):
        client.post('/api/rpr/ingest', json={})
        client.post('/api/rpr/tastings', json={'store_number': 201, 'rep': 'Ed',
                                               'status': 'done', 'display_secured': True})
        before = len(client.get('/api/rpr/tastings?store_number=201').get_json())
        client.post('/api/rpr/ingest', json={})  # re-ingest must not wipe logs
        after = len(client.get('/api/rpr/tastings?store_number=201').get_json())
        assert after >= before >= 1


# ---- tastings + photos + displays ----
class TestTastingsPhotos:
    JPEG = ('/9j/4AAQSkZJRgABAQEAYABgAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRof'
            'Hh0aHBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/wAALCAABAAEBAREA/8QAFAAB'
            'AAAAAAAAAAAAAAAAAAAAA//EABQQAQAAAAAAAAAAAAAAAAAAAAD/2gAIAQEAAD8AfwD/2Q==')

    def test_log_tasting_with_photo_and_retrieve(self, client, seeded):
        client.post('/api/rpr/ingest', json={})
        r = client.post('/api/rpr/tastings', json={
            'store_number': 390, 'rep': 'Vaneet', 'status': 'done', 'staff_count': 4,
            'display_secured': True, 'shelf_position': 'eye-level', 'photo_b64': self.JPEG})
        assert r.status_code == 200
        body = r.get_json()
        assert body['ok'] and body['log_id'] and body['photo_id']
        photos = client.get('/api/rpr/photos?store_number=390').get_json()
        assert photos and photos[0]['store_number'] == 390
        img = client.get(f"/api/rpr/photo/{photos[0]['id']}")
        assert img.status_code == 200
        assert img.data[:3] == b'\xff\xd8\xff'  # JPEG magic

    def test_oversized_photo_413(self, client, seeded):
        big = 'A' * 800_001
        r = client.post('/api/rpr/tastings', json={
            'store_number': 201, 'rep': 'Ed', 'display_secured': None, 'photo_b64': big})
        assert r.status_code == 413

    def test_displays_secured_and_missing(self, client, seeded):
        client.post('/api/rpr/ingest', json={})
        client.post('/api/rpr/tastings', json={'store_number': 191, 'rep': 'Ed',
                                               'status': 'done', 'display_secured': True})
        client.post('/api/rpr/tastings', json={'store_number': 773, 'rep': 'Ed',
                                               'status': 'done', 'display_secured': False})
        sec = [x['store_number'] for x in client.get('/api/rpr/displays?secured=1').get_json()]
        miss = [x['store_number'] for x in client.get('/api/rpr/displays?secured=0').get_json()]
        assert 191 in sec and 191 not in miss
        assert 773 in miss and 773 not in sec


# ---- schedule + calendar ----
class TestSchedule:
    def test_schedule_upsert_list_and_autodone(self, client, seeded):
        client.post('/api/rpr/ingest', json={})
        r = client.post('/api/rpr/schedule', json={'store_number': 201, 'rep': 'Vaneet',
                                                   'planned_date': '2026-06-22', 'notes': 'FIFA'})
        assert r.status_code == 200 and r.get_json()['ok']
        rows = client.get('/api/rpr/schedule?rep=Vaneet').get_json()
        assert any(x['store_number'] == 201 and x['planned_date'] == '2026-06-22' for x in rows)
        # logging a tasting auto-marks the planned stop done
        client.post('/api/rpr/tastings', json={'store_number': 201, 'rep': 'Vaneet',
                                               'status': 'done', 'display_secured': True})
        rows2 = client.get('/api/rpr/schedule?rep=Vaneet').get_json()
        sched = [x for x in rows2 if x['store_number'] == 201][0]
        assert sched['status'] == 'done'

    def test_calendar_ics_has_event(self, client, seeded):
        client.post('/api/rpr/ingest', json={})
        client.post('/api/rpr/schedule', json={'store_number': 390, 'rep': 'Ed',
                                               'planned_date': '2026-06-25'})
        ics = client.get('/api/rpr/calendar/Ed.ics').get_data(as_text=True)
        assert 'BEGIN:VCALENDAR' in ics and 'BEGIN:VEVENT' in ics
        assert 'DTSTART;VALUE=DATE:20260625' in ics


# ---- HORECA radius finder ----
class TestHorecaNearby:
    def test_radius_orders_and_bounds(self, client, seeded):
        # Drake (Toronto) ~near; Far Bar (Barrie) ~90km from downtown TO
        d = client.get('/api/horeca/nearby?lat=43.65&lng=-79.40&radius_km=20').get_json()
        names = [r['name'] for r in d['results']]
        assert 'Drake Hotel' in names
        assert 'Far Bar' not in names  # outside 20km
        # distances ascending
        dists = [r['distance_km'] for r in d['results']]
        assert dists == sorted(dists)

    def test_100plus_no_bound_includes_far(self, client, seeded):
        d = client.get('/api/horeca/nearby?lat=43.65&lng=-79.40&radius_km=999').get_json()
        names = [r['name'] for r in d['results']]
        assert 'Far Bar' in names
        assert d['radius_km'] == '100+'

    def test_maps_url_present(self, client, seeded):
        d = client.get('/api/horeca/nearby?lat=43.65&lng=-79.40&radius_km=50').get_json()
        assert all('google.com/maps/dir' in r['maps_url'] for r in d['results'])

    def test_missing_origin_400(self, client, seeded):
        assert client.get('/api/horeca/nearby').status_code == 400


# ---- report snapshots ----
class TestReports:
    def test_weekly_snapshots_and_saved_and_compare(self, client, seeded):
        client.get('/api/reports/weekly?end=2026-06-01')
        client.get('/api/reports/weekly?end=2026-06-08')
        saved = client.get('/api/reports/saved').get_json()
        assert len(saved) >= 2
        assert all('headline_metrics' in s for s in saved)
        cmp = client.get('/api/reports/compare?a=2026-06-01&b=2026-06-08')
        assert cmp.status_code == 200
        body = cmp.get_json()
        assert 'deltas' in body and body['a'] and body['b']

    def test_compare_missing_404(self, client, seeded):
        assert client.get('/api/reports/compare?a=1900-01-01&b=1900-01-08').status_code == 404
