"""AI SALES ENGINE — auto-hunt pipeline, geocoded day routes, brief, pipeline.

Covers:
  - hunt promotes untouched licensed targets into the pipeline (prospect +
    deals row + licence linked), ranked independent-first, idempotent,
  - hunt never re-promotes a licence already in the book,
  - day-plan orders geocoded targets nearest-neighbour, packs into days with a
    Maps directions link, and reports km saved vs unordered,
  - brief returns a useful line even with no API key (free fallback),
  - pipeline board counts by stage + surfaces due accounts.

Run: python3 -m pytest tests/test_sales_engine.py -v
"""
import io
import os
import sys
import tempfile

import pytest

os.environ.pop('DATABASE_URL', None)
os.environ.pop('ADMIN_TOKEN', None)
os.environ.pop('API_KEY', None)
os.environ.pop('ANTHROPIC_API_KEY', None)  # force the free fallback path
_TMP = tempfile.mkdtemp(prefix='anu_sales_engine_test_')
os.environ['DB_DIR'] = _TMP

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))


@pytest.fixture(scope='module')
def app_module():
    for mod in list(sys.modules):
        if mod == 'app' or mod.startswith('app.'):
            del sys.modules[mod]
    os.environ['DB_DIR'] = _TMP
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        'app', os.path.join(os.path.dirname(__file__), '..', 'app.py'))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.fixture
def client(app_module):
    return app_module.app.test_client()


# 4 licensees: 2 independent Toronto (core), 1 chain, 1 gtha — all geocoded.
AGCO_CSV = '﻿' + '\n'.join([
    'Licence Number,Licence Type,Legal Entity Name,Premises Name,Street Address,City,Province,Postal Code,Endorsement(s),Effective Date,Issue Date,Expiry Date,Deemed to Continue Until,Licence Status',
    'LSL40001,Liquor Sales Licence,A INC, AAA INDIE BAR,10 KING ST W,TORONTO,ON,M5H1A1,,,,,,Active',
    'LSL40002,Liquor Sales Licence,B INC,BBB INDIE RESTO,20 QUEEN ST W,TORONTO,ON,M5H2M2,,,,,,Active',
    'LSL40003,Liquor Sales Licence,CHAIN CO,CHAIN GRILL,1 MAIN ST,TORONTO,ON,M5H3A3,,,,,,Active',
    'LSL40004,Liquor Sales Licence,CHAIN CO,CHAIN GRILL,9 HURONTARIO,MISSISSAUGA,ON,L5B1B1,,,,,,Active',
])


@pytest.fixture(scope='module')
def seeded(app_module):
    """Sync AGCO + stamp coordinates onto the licences so day-plan has pins."""
    client = app_module.app.test_client()
    client.post('/api/horeca/agco/sync',
                data={'file': (io.BytesIO(AGCO_CSV.encode()), 'a.csv')},
                content_type='multipart/form-data')
    client.get('/api/horeca/sweep/status')  # ensures the enrichment columns
    # Write coords on a dedicated committed connection so every later
    # request-scoped connection sees them (avoids g.db teardown timing).
    coords = {'LSL40001': (43.650, -79.383), 'LSL40002': (43.652, -79.381),
              'LSL40003': (43.700, -79.400), 'LSL40004': (43.590, -79.640)}
    conn = app_module._sod_get_conn()
    cur = conn.cursor()
    ph = '%s' if app_module.USE_POSTGRES else '?'
    for lic, (la, ln) in coords.items():
        cur.execute(f"UPDATE agco_licensees SET lat={ph}, lng={ph}, "
                    f"phone='416-000-0000' WHERE licence_number={ph}",
                    (la, ln, lic))
    conn.commit()
    conn.close()
    return True


class TestHunt:
    def test_hunt_promotes_independents_first(self, seeded, client):
        r = client.post('/api/sales/hunt', json={'region': 'core', 'limit': 10, 'rep': 'Ikshit'})
        assert r.status_code == 200, r.get_json()
        body = r.get_json()
        # 3 core (2 indie + 1 chain-Toronto); Mississauga excluded by region=core
        assert body['promoted'] == 3
        names = [t['name'] for t in body['targets']]
        # independents ranked ahead of the chain
        assert names.index('AAA INDIE BAR') < names.index('CHAIN GRILL')
        # each promoted target carries a lead SKU + priority
        assert all(t['lead_sku'] and t['priority'] for t in body['targets'])

    def test_hunt_is_idempotent(self, seeded, client):
        r = client.post('/api/sales/hunt', json={'region': 'core', 'limit': 10})
        assert r.get_json()['promoted'] == 0  # already promoted

    def test_promoted_created_deals_and_linked_licence(self, seeded, client, app_module):
        with app_module.app.app_context():
            db = app_module.get_db()
            n_deals = db.execute(
                "SELECT COUNT(*) FROM deals WHERE source='auto-hunt'").fetchone()[0]
            n_linked = db.execute(
                "SELECT COUNT(*) FROM agco_licensees "
                "WHERE matched_account_id IS NOT NULL AND region='core'").fetchone()[0]
        assert n_deals >= 3
        assert n_linked >= 3


class TestDayPlan:
    def test_day_plan_orders_and_saves_distance(self, seeded, client):
        # Start far NW so nearest-neighbour ordering clearly differs from raw.
        r = client.get('/api/sales/day-plan?region=core&days=2&stops_per_day=2'
                       '&start_lat=43.66&start_lng=-79.40')
        assert r.status_code == 200, r.get_json()
        body = r.get_json()
        assert body['total_targets'] == 3
        assert len(body['days']) >= 1
        d1 = body['days'][0]
        assert d1['stop_count'] == 2
        assert d1['directions_url'].startswith('https://www.google.com/maps/dir/')
        assert d1['est_total_min'] > 0
        # NN route is never meaningfully longer than the raw order (allow a
        # little slack for per-leg rounding of the planned distance).
        assert body['planned_km'] <= body['unordered_km'] + 1.0
        assert body['km_saved'] >= 0

    def test_day_plan_empty_when_no_geocodes(self, client):
        r = client.get('/api/sales/day-plan?city=nowhereville')
        assert r.status_code == 200
        assert r.get_json()['total_targets'] == 0


class TestBriefAndPipeline:
    def test_brief_free_fallback(self, seeded, client):
        acct = client.get('/api/crm/horeca').get_json()[0]['id']
        r = client.post('/api/sales/brief', json={'account_id': acct})
        assert r.status_code == 200
        body = r.get_json()
        assert body['ai'] is False              # no key in test env
        assert body['source'] == 'free rule-based'
        assert len(body['brief']) > 20
        assert 'Rock Paper Rum' in body['brief'] or 'Feni' in body['brief'] or 'Fratelli' in body['brief']

    def test_pipeline_board(self, seeded, client):
        r = client.get('/api/sales/pipeline')
        assert r.status_code == 200
        body = r.get_json()
        assert body['auto_hunted'] >= 3
        assert body['by_status'].get('prospect', 0) >= 3
        assert 'due_count' in body

    def test_scheduler_core_standalone(self, seeded, app_module):
        # The daily job path: promote from a dedicated connection (Postgres-safe
        # cursor writes). Mississauga is still unpromoted → exactly 1 left.
        conn = app_module._sod_get_conn()
        try:
            promoted, _ = app_module._auto_hunt(conn, region='gtha', limit=5)
        finally:
            conn.close()
        assert len(promoted) == 1
        assert promoted[0]['city'].lower() == 'mississauga'
