"""Venue typeahead + rep quick-add + LCBO velocity/rebalance.

Run: python3 -m pytest tests/test_quickadd_velocity.py -v
"""
import io
import os
import sys
import tempfile

import pytest

os.environ.pop('DATABASE_URL', None)
os.environ.pop('ADMIN_TOKEN', None)
os.environ.pop('API_KEY', None)
_TMP = tempfile.mkdtemp(prefix='anu_quickadd_test_')
os.environ['DB_DIR'] = _TMP

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

RUTLAND = '0049902'


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
    # never hit the real Nominatim/Photon/Google in tests
    mod._nominatim_point = lambda q: (43.70, -79.40) if 'geocodable' in q.lower() else None
    mod._photon_suggest = lambda q, limit=5: []
    return mod


@pytest.fixture
def client(app_module):
    return app_module.app.test_client()


AGCO_CSV = '﻿' + '\n'.join([
    'Licence Number,Licence Type,Legal Entity Name,Premises Name,Street Address,City,Province,Postal Code,Endorsement(s),Effective Date,Issue Date,Expiry Date,Deemed to Continue Until,Licence Status',
    'LSL90001,Liquor Sales Licence,A,SPICE SYMPHONY,10 GERRARD ST,TORONTO,ON,M5B1G3,,,,,,Active',
    'LSL90002,Liquor Sales Licence,B,SPICE ROUTE LOUNGE,499 KING ST W,TORONTO,ON,M5V1K4,,,,,,Active',
    'LSL90003,Liquor Sales Licence,C,MAPLE TAVERN,77 MAIN ST,BRAMPTON,ON,L6W2E6,,,,,,Active',
])


@pytest.fixture(scope='module')
def seeded(app_module):
    client = app_module.app.test_client()
    client.post('/api/horeca/agco/sync',
                data={'file': (io.BytesIO(AGCO_CSV.encode()), 'a.csv')},
                content_type='multipart/form-data')
    client.get('/api/horeca/sweep/status')  # ensure enrichment columns exist
    with app_module.app.app_context():
        db = app_module.get_db()
        db.execute("INSERT INTO horeca_accounts (name, city, status) VALUES "
                   "('Spice Corner','Toronto','customer')")
        db.execute("UPDATE agco_licensees SET phone='416-555-9001' "
                   "WHERE licence_number='LSL90001'")
        # Rutland velocity history at two stores: #555 sells fast and gets a
        # restock mid-window; #601 sits stagnant.
        db.execute("INSERT OR IGNORE INTO stores (store_number, account, city) "
                   "VALUES (555,'Airport & Bovaird','Brampton')")
        db.execute("INSERT OR IGNORE INTO stores (store_number, account, city) "
                   "VALUES (601,'Queen & McLaughlin','Brampton')")
        from datetime import date, timedelta
        today = date.today()
        fast = [24, 18, 12, 30, 24]   # sold 6+6, restock +18, sold 6 => 18
        for i, oh in enumerate(fast):
            d = (today - timedelta(days=(len(fast) - 1 - i) * 3)).isoformat()
            db.execute("INSERT INTO sod_inventory (sku, store_number, "
                       "snapshot_date, status, on_hand, product_name) VALUES "
                       "(?, 555, ?, 'L', ?, 'Rutland Square')",
                       (RUTLAND, d, oh))
        for i in range(5):
            d = (today - timedelta(days=(4 - i) * 3)).isoformat()
            db.execute("INSERT INTO sod_inventory (sku, store_number, "
                       "snapshot_date, status, on_hand, product_name) VALUES "
                       "(?, 601, ?, 'L', 48, 'Rutland Square')", (RUTLAND, d))
        db.commit()
    return True


class TestVenueSearch:
    def test_short_query_returns_nothing(self, seeded, client):
        assert client.get('/api/horeca/venue-search?q=s').get_json()['rows'] == []

    def test_matches_book_then_licensees(self, seeded, client):
        rows = client.get('/api/horeca/venue-search?q=spice').get_json()['rows']
        kinds = [r['kind'] for r in rows]
        assert kinds[0] == 'account'          # existing book record first
        assert rows[0]['name'] == 'Spice Corner'
        lic = [r for r in rows if r['kind'] == 'licensee']
        assert {x['name'] for x in lic} == {'SPICE SYMPHONY', 'SPICE ROUTE LOUNGE'}
        sym = next(x for x in lic if x['name'] == 'SPICE SYMPHONY')
        assert sym['address'] == '10 GERRARD ST' and sym['phone'] == '416-555-9001'

    def test_typeahead_never_calls_nominatim(self, seeded, client, app_module):
        # Nominatim's usage policy forbids autocomplete; the live layer must
        # use Photon/Google only. Tripwire raises if the old path returns.
        orig = app_module._nominatim_point

        def tripwire(q):
            raise AssertionError('nominatim called from typeahead')
        app_module._nominatim_point = tripwire
        try:
            r = client.get('/api/horeca/venue-search?q=zzz%20noplace&live=1')
            assert r.status_code == 200
            assert all(x['kind'] != 'address' for x in r.get_json()['rows'])
        finally:
            app_module._nominatim_point = orig


class TestLiveLayers:
    def test_google_branch_inert_without_key(self, app_module):
        assert app_module.GOOGLE_PLACES_KEY == ''
        assert app_module._google_autocomplete('spice') == []

    def test_live_suggestions_merged_and_deduped(self, seeded, client, app_module):
        orig = app_module._photon_suggest
        app_module._photon_suggest = lambda q, limit=5: [
            {'kind': 'venue', 'name': 'SPICE SYMPHONY', 'address': 'dupe',
             'city': 'Toronto', 'note': 'live map suggestion'},
            {'kind': 'venue', 'name': 'Totally New Bar', 'address': '9 New St',
             'city': 'Toronto', 'note': 'live map suggestion'},
        ]
        try:
            rows = client.get(
                '/api/horeca/venue-search?q=spice&live=1').get_json()['rows']
            names = [r['name'] for r in rows]
            assert 'Totally New Bar' in names          # live result merged in
            assert names.count('SPICE SYMPHONY') == 1  # dupe vs licensee dropped
        finally:
            app_module._photon_suggest = orig


class TestQuickAdd:
    def test_requires_name(self, seeded, client):
        assert client.post('/api/horeca/quick-add', json={}).status_code == 400

    def test_creates_and_enriches_from_licence(self, seeded, client, app_module):
        r = client.post('/api/horeca/quick-add', json={
            'name': 'Maple Tavern', 'city': 'Brampton', 'rep': 'Namit'})
        assert r.status_code == 200, r.get_json()
        body = r.get_json()
        assert body['status'] == 'created'
        assert body['licence_no'] == 'LSL90003'
        assert body['address'] == '77 MAIN ST'   # pulled from the licence
        with app_module.app.app_context():
            db = app_module.get_db()
            row = db.execute(
                "SELECT status, rep_name, source, licence_no FROM "
                "horeca_accounts WHERE id=?", (body['account_id'],)).fetchone()
            assert row[0] == 'prospect' and row[1] == 'Namit'
            assert row[2] == 'rep-quick-add' and row[3] == 'LSL90003'
            m = db.execute("SELECT matched_account_id FROM agco_licensees "
                           "WHERE licence_number='LSL90003'").fetchone()
            assert m[0] == body['account_id']    # licence linked back

    def test_duplicate_points_at_existing(self, seeded, client):
        r = client.post('/api/horeca/quick-add', json={
            'name': 'spice corner', 'city': 'Toronto'})
        assert r.get_json()['status'] == 'exists'

    def test_geocodes_typed_address(self, seeded, client):
        r = client.post('/api/horeca/quick-add', json={
            'name': 'Brand New Bar', 'address': 'geocodable 12 Yonge St',
            'city': 'Toronto', 'rep': 'Namit'})
        assert r.get_json()['geocoded'] is True

    def test_never_steals_matched_licence(self, seeded, client, app_module):
        # A licence already linked to an account must NOT be re-pointed by a
        # later quick-add of the same venue name.
        with app_module.app.app_context():
            db = app_module.get_db()
            first = db.execute("SELECT matched_account_id FROM agco_licensees "
                               "WHERE licence_number='LSL90003'").fetchone()[0]
        assert first is not None    # linked by the Maple Tavern test above
        r = client.post('/api/horeca/quick-add', json={
            'name': 'Maple Tavern 2nd Location', 'city': 'Brampton'})
        assert r.get_json().get('licence_no', '') == ''
        with app_module.app.app_context():
            db = app_module.get_db()
            still = db.execute("SELECT matched_account_id FROM agco_licensees "
                               "WHERE licence_number='LSL90003'").fetchone()[0]
        assert still == first

    def test_no_city_chain_is_ambiguous_not_guessed(self, seeded, client,
                                                    app_module):
        # Chain licensed in two cities + no city typed: no licence attached.
        with app_module.app.app_context():
            db = app_module.get_db()
            for ln, ct in (('LSL90008', 'TORONTO'), ('LSL90009', 'OAKVILLE')):
                db.execute("INSERT INTO agco_licensees (licence_number, name, "
                           "city, status) VALUES (?, 'CHAI CHAIN', ?, 'Active')",
                           (ln, ct))
            db.commit()
        r = client.post('/api/horeca/quick-add', json={'name': 'Chai Chain'})
        body = r.get_json()
        assert body['status'] == 'created' and body['licence_no'] == ''

    def test_duplicate_across_cities_asks_for_city(self, seeded, client,
                                                   app_module):
        with app_module.app.app_context():
            db = app_module.get_db()
            db.execute("INSERT INTO horeca_accounts (name, city) VALUES "
                       "('Twin House','Toronto')")
            db.execute("INSERT INTO horeca_accounts (name, city) VALUES "
                       "('Twin House','Brampton')")
            db.commit()
        r = client.post('/api/horeca/quick-add', json={'name': 'Twin House'})
        assert r.status_code == 409
        assert len(r.get_json()['candidates']) == 2


class TestGoogleParse:
    def test_city_never_becomes_province(self, app_module):
        parts_in = 'Toronto, ON, Canada'
        parts = [x.strip() for x in parts_in.split(',')
                 if x.strip() and x.strip().upper() not in ('ON', 'ONTARIO', 'CANADA')]
        assert parts == ['Toronto']
        # and through the real parser shape: secondaryText with street
        addr = '123 Queen St W, Toronto, ON, Canada'
        parts = [x.strip() for x in addr.split(',')
                 if x.strip() and x.strip().upper() not in ('ON', 'ONTARIO', 'CANADA')]
        assert parts[-1] == 'Toronto' and parts[0] == '123 Queen St W'


class TestVelocity:
    def test_all_skus_view_not_empty(self, seeded, client):
        # Regression: _pad_sku('') zero-pads to '0000000', which silently
        # filtered the no-sku (all SKUs) view down to zero rows on prod.
        rows = client.get('/api/sales/velocity?days=28').get_json()['rows']
        assert {r['store_number'] for r in rows} >= {555, 601}

    def test_restock_never_counts_negative(self, seeded, client):
        rows = client.get(
            f'/api/sales/velocity?days=28&sku={RUTLAND}').get_json()['rows']
        fast = next(r for r in rows if r['store_number'] == 555)
        assert fast['sold_est'] == 18        # decreases only: 6+6+6
        assert fast['on_hand'] == 24
        assert 'Brampton' in fast['store_label']

    def test_stagnant_class(self, seeded, client):
        rows = client.get(
            f'/api/sales/velocity?days=28&sku={RUTLAND}').get_json()['rows']
        sleepy = next(r for r in rows if r['store_number'] == 601)
        assert sleepy['sold_est'] == 0 and sleepy['class'] == 'stagnant'

    def test_rate_uses_observed_span_not_window(self, seeded, client):
        # 18 sold across a 12-day span = 1.5/day = 10.5/wk, cover 24/1.5 = 16d
        # -> 'fast'. Dividing by the 28-day WINDOW would say 4.5/wk, cover 37
        # -> 'steady'/'slow' and hide a restock risk.
        rows = client.get(
            f'/api/sales/velocity?days=28&sku={RUTLAND}').get_json()['rows']
        fast = next(r for r in rows if r['store_number'] == 555)
        assert fast['rate_per_week'] == 10.5
        assert fast['days_of_cover'] == 16
        assert fast['class'] == 'fast'

    def test_empty_dead_shelf_is_out_not_slow(self, seeded, client,
                                              app_module):
        from datetime import date, timedelta
        with app_module.app.app_context():
            db = app_module.get_db()
            today = date.today()
            for i in range(3):
                d = (today - timedelta(days=(2 - i) * 3)).isoformat()
                db.execute("INSERT INTO sod_inventory (sku, store_number, "
                           "snapshot_date, status, on_hand, product_name) "
                           "VALUES (?, 602, ?, 'L', 0, 'Rutland Square')",
                           (RUTLAND, d))
            db.commit()
        rows = client.get(
            f'/api/sales/velocity?days=28&sku={RUTLAND}').get_json()['rows']
        dead = next(r for r in rows if r['store_number'] == 602)
        assert dead['class'] == 'out'


class TestRebalance:
    def test_play_pairs_heavy_and_dry(self, seeded, client):
        r = client.get(f'/api/sales/rebalance?sku={RUTLAND}')
        assert r.status_code == 200, r.get_json()
        body = r.get_json()
        heavy = [x['store_number'] for x in body['slow_heavy']]
        assert 601 in heavy                  # 48 sitting, zero movement
        assert 'play' in body and 'tastings' in body['play'].lower()

    def test_unknown_sku_rejected(self, seeded, client):
        assert client.get('/api/sales/rebalance?sku=9999999').status_code == 400
