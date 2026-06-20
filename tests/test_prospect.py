"""HORECA prospecting tests — Nominatim/Overpass/AGCO flow, fully mocked.

No live network calls: Nominatim geocode and Overpass POI queries are
monkeypatched. Covers:
  - Overpass element normalization (nodes vs ways/relations, tag mapping)
  - dedupe by exact normalized name + city
  - dedupe by ~100m proximity + fuzzy name
  - import idempotency (re-import skips, double-submit within one batch skips)
  - territory auto-create by city slug

Run with: python3 -m pytest tests/test_prospect.py -v
"""
import os
import sqlite3
import sys
import tempfile

import pytest

# Force SQLite in an isolated temp dir so we never touch production Postgres
# (app.py reads DB_DIR, not DB_PATH).
os.environ.pop('DATABASE_URL', None)
# POST endpoints here use @require_app_origin — with ADMIN_TOKEN unset the
# decorator falls back to allowing localhost, which the Flask test client is.
# API_KEY unset = no X-API-Key gate. Pop both so a dev shell can't break tests.
os.environ.pop('ADMIN_TOKEN', None)
os.environ.pop('API_KEY', None)
_TMP = tempfile.mkdtemp(prefix='anu_imports_prospect_test_')
os.environ['DB_DIR'] = _TMP
TEST_DB = os.path.join(_TMP, 'anu_imports.db')

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))


@pytest.fixture(scope='module')
def app_module():
    for mod in list(sys.modules):
        if mod == 'app' or mod.startswith('app.'):
            del sys.modules[mod]
    # Re-assert DB_DIR: other test modules set it at collection time, and the
    # last-collected module would otherwise win for everyone.
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


# Toronto-ish bbox: (south, north, west, east)
FAKE_BBOX = (43.58, 43.86, -79.64, -79.12)

FAKE_OVERPASS_ELEMENTS = [
    {   # node with full tags
        'type': 'node', 'id': 111, 'lat': 43.6510, 'lon': -79.3800,
        'tags': {'amenity': 'bar', 'name': 'The Velvet Fox',
                 'addr:housenumber': '12', 'addr:street': 'King St W',
                 'addr:city': 'Toronto', 'addr:postcode': 'M5H 1A1',
                 'phone': '+1-416-555-0100', 'website': 'https://velvetfox.example',
                 'cuisine': 'cocktails'},
    },
    {   # way — coordinates come from 'center'
        'type': 'way', 'id': 222, 'center': {'lat': 43.6600, 'lon': -79.3900},
        'tags': {'amenity': 'restaurant', 'name': 'Casa Bella',
                 'contact:phone': '+1-416-555-0200'},
    },
    {   # unnamed → must be dropped by normalize
        'type': 'node', 'id': 333, 'lat': 43.66, 'lon': -79.40,
        'tags': {'amenity': 'pub'},
    },
]


@pytest.fixture
def mocked_osm(app_module, monkeypatch):
    """Mock the two network calls; everything downstream is real code."""
    monkeypatch.setattr(app_module, '_nominatim_city_bbox', lambda city: FAKE_BBOX)
    monkeypatch.setattr(app_module, '_overpass_query_pois',
                        lambda bbox, categories, limit=300: list(FAKE_OVERPASS_ELEMENTS))
    # Each test starts with a cold cache
    app_module._prospect_cache.clear()
    return app_module


def _clear_horeca(app_module):
    conn = sqlite3.connect(TEST_DB)
    conn.execute('DELETE FROM horeca_accounts')
    conn.execute('DELETE FROM territories')
    conn.commit()
    conn.close()


# ========================================================================
# Normalization
# ========================================================================

class TestNormalize:
    def test_node_with_full_tags(self, app_module):
        cand = app_module._normalize_overpass_element(FAKE_OVERPASS_ELEMENTS[0],
                                                      fallback_city='Toronto')
        assert cand['name'] == 'The Velvet Fox'
        assert cand['address'] == '12 King St W'
        assert cand['city'] == 'Toronto'
        assert cand['postal'] == 'M5H 1A1'
        assert cand['lat'] == pytest.approx(43.6510)
        assert cand['account_type'] == 'bar'
        assert cand['phone'] == '+1-416-555-0100'
        assert cand['osm_id'] == 'node/111'
        assert cand['source'] == 'overpass'

    def test_way_uses_center_and_fallback_city(self, app_module):
        cand = app_module._normalize_overpass_element(FAKE_OVERPASS_ELEMENTS[1],
                                                      fallback_city='Toronto')
        assert cand['lat'] == pytest.approx(43.66)
        assert cand['lng'] == pytest.approx(-79.39)
        assert cand['city'] == 'Toronto'  # no addr:city tag → fallback
        assert cand['phone'] == '+1-416-555-0200'  # contact:phone fallback
        assert cand['osm_id'] == 'way/222'

    def test_unnamed_element_dropped(self, app_module):
        assert app_module._normalize_overpass_element(FAKE_OVERPASS_ELEMENTS[2]) is None


# ========================================================================
# Search endpoint + dedupe
# ========================================================================

class TestProspectSearch:
    def test_search_returns_candidates_no_autoinsert(self, client, mocked_osm):
        _clear_horeca(mocked_osm)
        r = client.post('/api/horeca/prospect/search', json={'city': 'Toronto'})
        assert r.status_code == 200
        data = r.get_json()
        assert data['count'] == 2  # unnamed element dropped
        assert all(c['duplicate'] is False for c in data['candidates'])
        # NEVER auto-inserts
        conn = sqlite3.connect(TEST_DB)
        n = conn.execute('SELECT COUNT(*) FROM horeca_accounts').fetchone()[0]
        conn.close()
        assert n == 0

    def test_search_requires_city(self, client, mocked_osm):
        r = client.post('/api/horeca/prospect/search', json={})
        assert r.status_code == 400

    def test_dedupe_exact_name_and_city(self, client, mocked_osm):
        _clear_horeca(mocked_osm)
        conn = sqlite3.connect(TEST_DB)
        conn.execute(
            "INSERT INTO horeca_accounts (name, city, lat, lng) "
            "VALUES ('the velvet  fox', 'Toronto', 0, 0)")  # case/space variant
        conn.commit()
        conn.close()
        r = client.post('/api/horeca/prospect/search', json={'city': 'Toronto'})
        by_name = {c['name']: c for c in r.get_json()['candidates']}
        assert by_name['The Velvet Fox']['duplicate'] is True
        assert by_name['Casa Bella']['duplicate'] is False

    def test_dedupe_proximity_plus_fuzzy_name(self, client, mocked_osm):
        _clear_horeca(mocked_osm)
        conn = sqlite3.connect(TEST_DB)
        # ~30m away, slightly different name, DIFFERENT city string —
        # only the proximity+fuzzy rule can catch this.
        conn.execute(
            "INSERT INTO horeca_accounts (name, city, lat, lng) "
            "VALUES ('Velvet Fox Bar', 'toronto downtown', 43.65103, -79.38003)")
        conn.commit()
        conn.close()
        r = client.post('/api/horeca/prospect/search', json={'city': 'Toronto'})
        by_name = {c['name']: c for c in r.get_json()['candidates']}
        assert by_name['The Velvet Fox']['duplicate'] is True

    def test_far_away_same_name_not_duplicate(self, client, mocked_osm):
        _clear_horeca(mocked_osm)
        conn = sqlite3.connect(TEST_DB)
        # Same-ish name but 50km away and different city → NOT a duplicate
        conn.execute(
            "INSERT INTO horeca_accounts (name, city, lat, lng) "
            "VALUES ('Velvet Fox Bar', 'Hamilton', 43.2557, -79.8711)")
        conn.commit()
        conn.close()
        r = client.post('/api/horeca/prospect/search', json={'city': 'Toronto'})
        by_name = {c['name']: c for c in r.get_json()['candidates']}
        assert by_name['The Velvet Fox']['duplicate'] is False


# ========================================================================
# Import endpoint — idempotency + territory auto-create
# ========================================================================

class TestProspectImport:
    def _candidates(self):
        return [
            {'name': 'The Velvet Fox', 'address': '12 King St W', 'city': 'Toronto',
             'postal': 'M5H 1A1', 'lat': 43.6510, 'lng': -79.3800,
             'account_type': 'bar', 'phone': '+1-416-555-0100',
             'osm_id': 'node/111', 'source': 'overpass'},
            {'name': 'Casa Bella', 'address': '', 'city': 'Toronto',
             'postal': '', 'lat': 43.6600, 'lng': -79.3900,
             'account_type': 'restaurant', 'phone': '',
             'osm_id': 'way/222', 'source': 'overpass'},
        ]

    def test_import_inserts_prospects(self, client, app_module):
        _clear_horeca(app_module)
        r = client.post('/api/horeca/prospect/import',
                        json={'accounts': self._candidates(), 'rep': 'Vaneet'})
        assert r.status_code == 200
        data = r.get_json()
        assert data['imported'] == 2
        assert data['skipped'] == 0
        conn = sqlite3.connect(TEST_DB)
        rows = conn.execute(
            "SELECT name, status, source, rep_name, osm_id FROM horeca_accounts "
            "ORDER BY name").fetchall()
        conn.close()
        assert [r0[0] for r0 in rows] == ['Casa Bella', 'The Velvet Fox']
        for row in rows:
            assert row[1] == 'prospect'
            assert row[2] == 'overpass'
            assert row[3] == 'Vaneet'
            assert row[4]  # osm_id stored

    def test_reimport_is_idempotent(self, client, app_module):
        _clear_horeca(app_module)
        client.post('/api/horeca/prospect/import', json={'accounts': self._candidates()})
        r2 = client.post('/api/horeca/prospect/import', json={'accounts': self._candidates()})
        data = r2.get_json()
        assert data['imported'] == 0
        assert data['skipped'] == 2
        conn = sqlite3.connect(TEST_DB)
        n = conn.execute('SELECT COUNT(*) FROM horeca_accounts').fetchone()[0]
        conn.close()
        assert n == 2

    def test_double_submit_in_one_batch_skips(self, client, app_module):
        _clear_horeca(app_module)
        cands = self._candidates()
        r = client.post('/api/horeca/prospect/import',
                        json={'accounts': cands + [dict(cands[0])]})
        data = r.get_json()
        assert data['imported'] == 2
        assert data['skipped'] == 1

    def test_territory_autocreated_by_city_slug(self, client, app_module):
        _clear_horeca(app_module)
        accounts = [{'name': 'Harbour Tap', 'city': 'St. Catharines',
                     'lat': 43.1594, 'lng': -79.2469, 'account_type': 'pub'}]
        r = client.post('/api/horeca/prospect/import', json={'accounts': accounts})
        assert r.get_json()['imported'] == 1
        conn = sqlite3.connect(TEST_DB)
        terr = conn.execute(
            "SELECT code, name FROM territories WHERE code='st-catharines'").fetchone()
        acct = conn.execute(
            "SELECT territory_id FROM horeca_accounts WHERE name='Harbour Tap'").fetchone()
        tid = conn.execute(
            "SELECT id FROM territories WHERE code='st-catharines'").fetchone()[0]
        conn.close()
        assert terr is not None
        assert terr[1] == 'St. Catharines'
        assert acct[0] == tid

    def test_territory_reused_not_duplicated(self, client, app_module):
        _clear_horeca(app_module)
        a1 = [{'name': 'Bar One', 'city': 'Guelph', 'lat': 43.5448, 'lng': -80.2482}]
        a2 = [{'name': 'Bar Two', 'city': 'Guelph', 'lat': 43.5460, 'lng': -80.2500}]
        client.post('/api/horeca/prospect/import', json={'accounts': a1})
        client.post('/api/horeca/prospect/import', json={'accounts': a2})
        conn = sqlite3.connect(TEST_DB)
        n = conn.execute(
            "SELECT COUNT(*) FROM territories WHERE code='guelph'").fetchone()[0]
        conn.close()
        assert n == 1

    def test_import_requires_accounts(self, client, app_module):
        r = client.post('/api/horeca/prospect/import', json={})
        assert r.status_code == 400


# ========================================================================
# AGCO CSV upload
# ========================================================================

AGCO_CSV = '﻿' + (  # real UTF-8 BOM, same as the live AGCO file
    'Licence Number,Licence Type,Legal Entity Name,Premises Name,Street Address,'
    'City,Province,Postal Code,Endorsement(s),Effective Date,Issue Date,'
    'Expiry Date,Deemed to Continue Until,Licence Status\r\n'
    'LSL100001,Liquor Sales Licence,1000001 Ontario Inc,The Copper Still,'
    '99 Queen St,Toronto,ON,M5V 2A1,,2024-01-01,2024-01-01,2027-01-01,,Active\r\n'
    'LSL100002,Liquor Sales Licence,1000002 Ontario Inc,Dead Parrot Pub,'
    '1 Old Rd,Toronto,ON,M4C 1A1,,2018-01-01,2018-01-01,2020-01-01,,Expired\r\n'
    'LSL100003,Liquor Sales Licence,1000003 Ontario Inc,Pending Renewal Bistro,'
    '5 New St,Toronto,ON,M6K 3C3,,2023-01-01,2023-01-01,2026-01-01,2026-09-01,'
    'Deemed to Continue\r\n'
)


class TestAgcoUpload:
    def test_csv_upload_filters_dead_licences(self, client, app_module):
        _clear_horeca(app_module)
        import io as _io
        r = client.post(
            '/api/horeca/prospect/agco',
            data={'file': (_io.BytesIO(AGCO_CSV.encode('utf-8')), 'agco.csv')},
            content_type='multipart/form-data')
        assert r.status_code == 200
        data = r.get_json()
        names = [c['name'] for c in data['candidates']]
        assert 'The Copper Still' in names          # Active → kept
        assert 'Pending Renewal Bistro' in names    # Deemed to Continue → kept
        assert 'Dead Parrot Pub' not in names       # Expired → filtered
        for c in data['candidates']:
            assert c['source'] == 'agco'
            assert c['licence_no'].startswith('LSL')
            assert c['account_type'] == 'agco_licensee'

    def test_agco_bom_handled(self, client, app_module):
        """The AGCO file ships a UTF-8 BOM — first DictReader key must still
        be 'Licence Number', so licence_no must parse non-empty."""
        import io as _io
        r = client.post(
            '/api/horeca/prospect/agco',
            data={'file': (_io.BytesIO(AGCO_CSV.encode('utf-8')), 'agco.csv')},
            content_type='multipart/form-data')
        assert all(c['licence_no'] for c in r.get_json()['candidates'])

    def test_agco_licence_no_is_idempotency_key(self, client, app_module):
        _clear_horeca(app_module)
        accounts = [{'name': 'The Copper Still', 'city': 'Toronto',
                     'licence_no': 'LSL100001', 'source': 'agco',
                     'account_type': 'agco_licensee'}]
        r1 = client.post('/api/horeca/prospect/import', json={'accounts': accounts})
        assert r1.get_json()['imported'] == 1
        # Same licence, different name spelling → still a duplicate
        accounts2 = [{'name': 'Copper Still (The)', 'city': 'Toronto',
                      'licence_no': 'LSL100001', 'source': 'agco'}]
        r2 = client.post('/api/horeca/prospect/import', json={'accounts': accounts2})
        assert r2.get_json()['imported'] == 0
        assert r2.get_json()['skipped'] == 1

    def test_no_coords_fsa_fuzzy_fallback(self, client, app_module):
        """AGCO rows have no lat/lng — fuzzy name + same city + same postal
        FSA must stand in for the proximity check."""
        _clear_horeca(app_module)
        conn = sqlite3.connect(TEST_DB)
        conn.execute(
            "INSERT INTO horeca_accounts (name, city, postal, lat, lng) "
            "VALUES ('The Copper Still', 'Toronto', 'M5V 2A1', 0, 0)")
        conn.commit()
        conn.close()
        # Fuzzy-name variant ('copper still' ⊂ 'copper still bar'), same city,
        # same FSA M5V, different licence + unit number → duplicate.
        dup = {'name': 'Copper Still Bar', 'city': 'Toronto',
               'postal': 'M5V 2B9', 'licence_no': 'LSL999999', 'source': 'agco'}
        # Same fuzzy name + city but a different FSA → NOT a duplicate
        # (Toronto has many 'X Bar' name collisions across neighbourhoods).
        not_dup = {'name': 'Copper Still Bar', 'city': 'Toronto',
                   'postal': 'M4C 1A1', 'licence_no': 'LSL999998', 'source': 'agco'}
        with app_module.app.app_context():  # get_db needs Flask g
            existing = app_module._existing_horeca_rows()
        assert existing, 'seeded row must be visible to the dedupe fetch'
        assert existing[0]['postal'] == 'M5V 2A1'
        assert app_module._is_duplicate_prospect(dup, existing) is True
        assert app_module._is_duplicate_prospect(not_dup, existing) is False
        r = client.post('/api/horeca/prospect/import',
                        json={'accounts': [dup, not_dup]})
        assert r.get_json()['imported'] == 1
        assert r.get_json()['skipped'] == 1

    def test_agco_url_fetch_uses_etag_cache(self, client, app_module, monkeypatch):
        """First URL fetch stores the ETag; second sends If-None-Match and a
        304 reuses the cached CSV text without re-downloading ~5 MB."""
        app_module._agco_etag_cache.clear()
        calls = []

        class _Resp:
            def __init__(self, status_code, content=b'', headers=None):
                self.status_code = status_code
                self.content = content
                self.headers = headers or {}

            def raise_for_status(self):
                if self.status_code >= 400:
                    raise RuntimeError(f'HTTP {self.status_code}')

        def fake_get(url, headers=None, timeout=None, stream=None, **kw):
            calls.append(dict(headers or {}))
            if 'If-None-Match' in (headers or {}):
                assert headers['If-None-Match'] == '"6a27fbb8-4af0b5"'
                return _Resp(304)
            return _Resp(200, AGCO_CSV.encode('utf-8'),
                         {'ETag': '"6a27fbb8-4af0b5"'})

        monkeypatch.setattr(app_module.http_requests, 'get', fake_get)
        r1 = client.post('/api/horeca/prospect/agco', json={})
        assert r1.status_code == 200
        assert r1.get_json()['count'] == 2  # Active + Deemed to Continue
        r2 = client.post('/api/horeca/prospect/agco', json={})
        assert r2.status_code == 200
        assert r2.get_json()['count'] == 2  # served from ETag cache on 304
        assert len(calls) == 2
        assert 'If-None-Match' not in calls[0]
        assert calls[1].get('If-None-Match') == '"6a27fbb8-4af0b5"'
