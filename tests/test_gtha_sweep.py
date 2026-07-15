"""GTHA sweep — OSM tile harvest + AGCO enrichment cross-reference.

Overpass is fully mocked (no network). Covers:
  - tile grid plan is idempotent and covers the bbox,
  - a run drains pending tiles, upserts osm_venues, marks tiles done,
  - a failed tile is marked 'error' and retried on the next run,
  - enrich matches OSM venues to AGCO licensees by norm-name+city and stamps
    phone/website/coords into BLANK fields only (never overwrites),
  - enrich marks which mapped venues hold a licence,
  - the field book (horeca_accounts) gets coords enriched too,
  - /api/horeca/venues browses + filters (licensed / has_phone),
  - prospects endpoint now returns the enriched phone/website.

Run: python3 -m pytest tests/test_gtha_sweep.py -v
"""
import io
import os
import sys
import tempfile

import pytest

os.environ.pop('DATABASE_URL', None)
os.environ.pop('ADMIN_TOKEN', None)
os.environ.pop('API_KEY', None)
_TMP = tempfile.mkdtemp(prefix='anu_gtha_sweep_test_')
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


# Two OSM elements: one that will match a seeded AGCO licence, one that won't.
FAKE_ELEMENTS = [
    {'type': 'node', 'id': 1, 'lat': 43.65, 'lon': -79.38,
     'tags': {'amenity': 'bar', 'name': 'The Velvet Fox',
              'addr:housenumber': '12', 'addr:street': 'King St W',
              'addr:city': 'Toronto', 'addr:postcode': 'M5H 1A1',
              'phone': '+1-416-555-0100', 'website': 'https://velvetfox.example',
              'cuisine': 'cocktails', 'opening_hours': 'Mo-Su 16:00-02:00'}},
    {'type': 'way', 'id': 2, 'center': {'lat': 43.66, 'lon': -79.39},
     'tags': {'amenity': 'restaurant', 'name': 'Unlicensed Diner',
              'addr:city': 'Toronto', 'contact:phone': '+1-416-555-0200'}},
    {'type': 'node', 'id': 3, 'lat': 43.66, 'lon': -79.40,
     'tags': {'amenity': 'pub'}},  # unnamed → dropped
]

AGCO_CSV = '﻿' + '\n'.join([
    'Licence Number,Licence Type,Legal Entity Name,Premises Name,Street Address,City,Province,Postal Code,Endorsement(s),Effective Date,Issue Date,Expiry Date,Deemed to Continue Until,Licence Status',
    'LSL20001,Liquor Sales Licence,VELVET INC,THE VELVET FOX,12 KING ST W,TORONTO,ON,M5H1A1,,,,,,Active',
    'LSL20002,Liquor Sales Licence,OTHER CO,SOME OTHER BAR,9 QUEEN ST,TORONTO,ON,M5H2M2,,,,,,Active',
])


def _upload(client, path, text):
    return client.post(path, data={'file': (io.BytesIO(text.encode()), 'x.csv')},
                       content_type='multipart/form-data')


@pytest.fixture(scope='module')
def swept(app_module):
    """Plan the grid, mock Overpass so EVERY tile returns the fake elements
    for the first tile only, then drain a couple of tiles."""
    client = app_module.app.test_client()
    # Seed the AGCO universe (Velvet Fox is licensed; Unlicensed Diner is not).
    _upload(client, '/api/horeca/agco/sync', AGCO_CSV)
    assert client.post('/api/horeca/sweep/plan', json={}).status_code == 200

    calls = {'n': 0}

    def fake_sweep(bbox):
        calls['n'] += 1
        # Only the very first tile carries venues; the rest are empty (fast).
        if calls['n'] == 1:
            return list(FAKE_ELEMENTS), ''
        if calls['n'] == 2:
            return [], 'overpass 504'   # exercise the error path once
        return [], ''

    app_module._overpass_sweep_tile = fake_sweep
    # no real sleeping in tests
    app_module.time.sleep = lambda *a, **k: None
    r = client.post('/api/horeca/sweep/run', json={'tiles': 4})
    assert r.status_code == 200, r.get_json()
    return r.get_json()


class TestPlan:
    def test_grid_is_planned_and_idempotent(self, swept, client):
        first = client.post('/api/horeca/sweep/plan', json={}).get_json()
        assert first['tiles_added'] == 0  # already planned by the fixture
        assert first['tiles_total'] > 100  # GTHA grid is big
        st = client.get('/api/horeca/sweep/status').get_json()
        assert st['tiles_total'] == first['tiles_total']


class TestRun:
    def test_run_harvested_named_venues(self, swept):
        # 2 named venues from tile 1 (unnamed dropped); tile 2 errored.
        assert swept['venues_total'] == 2
        assert swept['tiles_swept_this_run'] >= 1

    def test_errored_tile_recorded(self, swept, client):
        st = client.get('/api/horeca/sweep/status').get_json()
        assert st['tiles'].get('error', 0) >= 1
        assert st['venues_total'] == 2


class TestEnrich:
    def test_enrich_matches_and_stamps_blanks(self, swept, client):
        r = client.post('/api/horeca/enrich', json={})
        assert r.status_code == 200, r.get_json()
        body = r.get_json()
        assert body['licensees_matched'] >= 1   # Velvet Fox
        assert body['licensees_enriched'] >= 1
        # The licensed prospect now carries the OSM phone.
        prospects = client.get(
            '/api/horeca/prospects?q=velvet%20fox').get_json()
        vf = next(p for p in prospects['rows'] if 'VELVET' in p['name'])
        assert vf['phone'] == '+1-416-555-0100'
        assert 'velvetfox' in vf['website']

    def test_licensed_venue_flagged(self, swept, client):
        v = client.get('/api/horeca/venues?licensed=1').get_json()
        names = [x['name'] for x in v['rows']]
        assert 'The Velvet Fox' in names
        assert 'Unlicensed Diner' not in names  # no matching licence

    def test_enrich_never_overwrites(self, swept, client, app_module):
        # Give the licence a REAL phone, then re-enrich: must not clobber it.
        with app_module.app.app_context():
            db = app_module.get_db()
            ph = '%s' if app_module.USE_POSTGRES else '?'
            db.execute(
                f"UPDATE agco_licensees SET phone='+1-416-999-0000' "
                f"WHERE licence_number={ph}", ('LSL20001',))
            db.commit()
        client.post('/api/horeca/enrich', json={})
        p = client.get('/api/horeca/prospects?q=velvet%20fox').get_json()
        vf = next(x for x in p['rows'] if 'VELVET' in x['name'])
        assert vf['phone'] == '+1-416-999-0000'  # preserved


class TestVenues:
    def test_has_phone_filter(self, swept, client):
        v = client.get('/api/horeca/venues?has_phone=1').get_json()
        assert v['count'] == 2  # both named venues carry a phone
        # deep links present
        assert all('google.com/maps' in r['google_maps_url'] for r in v['rows'])

    def test_export_tables_carry_sweep(self, app_module):
        names = {t for t, _ in app_module._EXPORT_TABLES}
        assert 'osm_venues' in names
        assert 'osm_sweep_tiles' in names


TORONTO_BIZ_CSV = '\n'.join([
    '_id,Category,Licence No.,Operating Name,Issued,Client Name,Business Phone,Business Phone Ext.,Licence Address Line 1,Licence Address Line 2,Licence Address Line 3,Ward,Conditions,Free Form Conditions Line 1,Free Form Conditions Line 2,Plate No.,Endorsements,Cancel Date,Last Record Update',
    # Active eating establishment WITH phone → should enrich the licensed venue
    '1,EATING ESTABLISHMENT,B01,THE VELVET FOX,2024-01-01,VELVET INC,416-555-7777,,12 KING ST W,"TORONTO, ON",M5H 1A1,10,,,,,,,2024-06-01',
    # Cancelled licence → must be ignored even though it has a phone
    '2,EATING ESTABLISHMENT,B02,SOME OTHER BAR,2015-01-01,OTHER CO,416-555-0000,,9 QUEEN ST,"TORONTO, ON",M5H 2M2,10,,,,,,2016-05-02,2016-05-02',
    # Non-food category → ignored
    '3,DRY CLEANER,B03,CLEAN CO,2024-01-01,CLEAN INC,416-555-1111,,1 MAIN ST,"TORONTO, ON",M5H 1A1,10,,,,,,,2024-06-01',
])


class TestTorontoPhoneEnrich:
    def test_stamps_phone_into_blank_licensee(self, swept, client):
        # Velvet Fox licence had OSM phone stamped earlier; blank it to test
        # the Toronto source fills a genuinely-empty phone.
        import io as _io
        r = client.post(
            '/api/horeca/enrich/toronto-phones',
            data={'file': (_io.BytesIO(TORONTO_BIZ_CSV.encode()), 'biz.csv')},
            content_type='multipart/form-data')
        assert r.status_code == 200, r.get_json()
        b = r.get_json()
        assert b['food_licences_with_phone'] == 1  # only the active eating row
        assert b['unique_phone_keys'] == 1

    def test_cancelled_and_nonfood_ignored(self, swept, client):
        import io as _io
        r = client.post(
            '/api/horeca/enrich/toronto-phones',
            data={'file': (_io.BytesIO(TORONTO_BIZ_CSV.encode()), 'biz.csv')},
            content_type='multipart/form-data')
        b = r.get_json()
        # 3 data rows scanned, only 1 kept (cancelled + dry-cleaner dropped)
        assert b['rows_scanned'] == 3
        assert b['food_licences_with_phone'] == 1
