"""The route planner and walk-in brief endpoints, over the real app on SQLite.
OSRM is unreachable in tests, so these exercise the straight-line fallback,
which is the path that must always work."""
import os
import sys
import tempfile

os.environ.pop('DATABASE_URL', None)
_TMP = tempfile.mkdtemp(prefix='route_ep_')
os.environ['DB_DIR'] = _TMP
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest  # noqa: E402
import importlib.util  # noqa: E402

ORIGIN_HDR = {'Origin': 'https://anu-imports-web.vercel.app'}


@pytest.fixture(scope='module')
def app_module():
    for m in list(sys.modules):
        if m == 'app' or m.startswith('app.'):
            del sys.modules[m]
    os.environ['DB_DIR'] = _TMP
    spec = importlib.util.spec_from_file_location(
        'app', os.path.join(os.path.dirname(__file__), '..', 'app.py'))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope='module')
def seeded(app_module):
    with app_module.app.app_context():
        db = app_module.get_db()
        app_module._ensure_geo_columns()
        stores = [
            (9001, '55 Bloor St W', 'Toronto', 43.6699, -79.3873),
            (9002, '300 Borough Dr', 'Scarborough', 43.7760, -79.2580),
            (9003, '900 Dundas St', 'Whitby', 43.8975, -78.9420),
            (9004, '2 County Court', 'Brampton', 43.6900, -79.7600),
            (9005, 'Wrongway', 'London', 42.9849, -81.2453),
        ]
        for sn, addr, city, lat, lng in stores:
            db.execute("INSERT OR REPLACE INTO stores (store_number, account, "
                       "address, city, lat, lng, geo_precision) VALUES "
                       "(?,?,?,?,?,?, 'address')",
                       (sn, f'LCBO #{sn}', addr, city, lat, lng))
        sku = list(app_module.SOD_TRACKED_SKUS)[0]
        # 9001 listed and healthy; 9002 listed but 1 unit (reorder)
        db.execute("INSERT INTO sod_inventory (sku, store_number, snapshot_date, "
                   "status, on_hand, product_name) VALUES (?,9001,'2026-08-16','L',20,'x')", (sku,))
        db.execute("INSERT INTO sod_inventory (sku, store_number, snapshot_date, "
                   "status, on_hand, product_name) VALUES (?,9002,'2026-08-16','L',1,'x')", (sku,))
        db.commit()
    return True


@pytest.fixture
def client(app_module):
    return app_module.app.test_client()


class TestRoutePlan:
    def test_needs_an_origin(self, client, seeded):
        r = client.post('/api/route/plan', json={'dest_city': 'Whitby'}, headers=ORIGIN_HDR)
        assert r.status_code == 400
        assert 'origin' in r.get_json()['error'].lower()

    def test_needs_a_destination(self, client, seeded):
        r = client.post('/api/route/plan',
                        json={'origin_lat': 43.65, 'origin_lng': -79.38}, headers=ORIGIN_HDR)
        assert r.status_code == 400

    def test_gps_origin_plans_a_feasible_day(self, client, seeded):
        r = client.post('/api/route/plan', json={
            'origin_lat': 43.6532, 'origin_lng': -79.3832,
            'dest_city': 'Whitby', 'day_hours': 10, 'max_stops': 10},
            headers=ORIGIN_HDR)
        assert r.status_code == 200, r.get_data(as_text=True)[:200]
        d = r.get_json()
        assert d['day_feasible'] is True
        assert d['stop_count'] >= 1
        nums = {s['store_number'] for s in d['stops']}
        assert 9005 not in nums, 'London is the wrong direction and must be dropped'
        # stops carry the sales signal
        assert all('gap' in s and 'leg_km' in s and 'seq' in s for s in d['stops'])
        assert d['totals']['drive_km'] > 0 and d['totals']['cost'] > 0

    def test_far_city_is_flagged_not_silently_broken(self, client, seeded):
        r = client.post('/api/route/plan', json={
            'origin_lat': 43.6532, 'origin_lng': -79.3832,
            'dest_city': 'Ottawa', 'day_hours': 8}, headers=ORIGIN_HDR)
        d = r.get_json()
        assert d['day_feasible'] is False
        assert 'overnight' in d['advice'].lower() or 'one-way' in d['advice'].lower()

    def test_horeca_toggle_weaves_in_on_trade(self, client, seeded, app_module):
        with app_module.app.app_context():
            db = app_module.get_db()
            db.execute("INSERT INTO horeca_accounts (name, account_type, city, "
                       "lat, lng, status, geo_precision) VALUES "
                       "('Test Bar','bar','Toronto',43.66,-79.40,'prospect','address')")
            db.commit()
        r = client.post('/api/route/plan', json={
            'origin_lat': 43.6532, 'origin_lng': -79.3832, 'dest_city': 'Whitby',
            'include_horeca': True, 'day_hours': 10}, headers=ORIGIN_HDR)
        d = r.get_json()
        assert any(h['name'] == 'Test Bar' for h in d['horeca_stops'])



class TestWalkInBrief:
    def test_brief_leads_with_a_reorder_when_stock_is_low(self, client, seeded):
        b = client.get('/api/store/9002/brief').get_json()
        assert 'Reorder' in b['lead_with']
        assert b['summary']['low_or_out'] >= 1
        rp = [x for x in b['skus'] if x['brand'] == 'Rock Paper'][0]
        assert rp['flag'] == 'LOW' and rp['on_hand'] == 1

    def test_brief_leads_with_a_pitch_when_there_are_gaps(self, client, seeded):
        b = client.get('/api/store/9004/brief').get_json()
        # 9004 has no SOD rows at all, so every SKU is a gap
        assert 'Pitch a listing' in b['lead_with']
        assert b['summary']['gaps'] >= 1

    def test_brief_404s_for_an_unknown_store(self, client, seeded):
        assert client.get('/api/store/99999/brief').status_code == 404

    def test_brief_lists_every_tracked_sku(self, client, seeded, app_module):
        b = client.get('/api/store/9001/brief').get_json()
        assert len(b['skus']) == len(app_module.SOD_TRACKED_SKUS)


class TestGeoStatus:
    def test_geo_status_reports_coverage(self, client, seeded):
        j = client.get('/api/geo/status').get_json()
        assert 'coverage' in j and 'job' in j
        assert j['coverage']['precise'] >= 5
