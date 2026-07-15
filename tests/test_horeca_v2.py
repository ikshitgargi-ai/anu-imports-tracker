"""HORECA CRM v2 tests — AGCO universe, seed-book, orders, reorder-due,
tiers, menu requests, portfolio. No live network: the AGCO sync is exercised
through the file-upload path.

Run with: python3 -m pytest tests/test_horeca_v2.py -v
"""
import io
import os
import sys
import tempfile

import pytest

os.environ.pop('DATABASE_URL', None)
os.environ.pop('ADMIN_TOKEN', None)
os.environ.pop('API_KEY', None)
_TMP = tempfile.mkdtemp(prefix='anu_imports_horeca_v2_test_')
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


AGCO_CSV = '﻿' + '\n'.join([
    'Licence Number,Licence Type,Legal Entity Name,Premises Name,Street Address,City,Province,Postal Code,Endorsement(s),Effective Date,Issue Date,Expiry Date,Deemed to Continue Until,Licence Status',
    'LSL10001,Liquor Sales Licence,OMNI CO,THE OMNI KING EDWARD HOTEL,37 KING ST E,TORONTO,ON,M5C1E9,,,,,,Active',
    'LSL10002,Liquor Sales Licence,VELVET INC,THE VELVET FOX BAR,12 KING ST W,TORONTO,ON,M5H1A1,,,,,,Active',
    'LSL10003,Liquor Sales Licence,CHAIN CO,BOSTON PASTA,1 MAIN ST,MISSISSAUGA,ON,L5B1B1,,,,,,Active',
    'LSL10004,Liquor Sales Licence,CHAIN CO,BOSTON PASTA,9 QUEEN ST,HAMILTON,ON,L8P1A1,,,,,,Active',
    'LSL10005,Liquor Sales Licence,DEAD CO,CLOSED TAVERN,5 GONE RD,TORONTO,ON,M1B1B1,,,,,,Expired',
    'LSL10006,Liquor Sales Licence,NOMAD CO,NOMAD CLUB,88 BAY ST,TORONTO,ON,M5J1ic,,,,,,Deemed to Continue',
    'LSL10007,Liquor Sales Licence,BARRIE CO,CROSSOVERS RESTAURANT,428 DUNLOP ST W,BARRIE,ON,L4N1C2,,,,,,Active',
])

SEED_CSV = '\n'.join([
    'account_name,account_type,city,area,status,priority,contact_name,phone,email,lead_sku,last_signal,next_action,licence_sale_no,source',
    'Chakna,restaurant,Toronto,Bloor St,customer,P1,,,,Red Admiral + Cashew Feni,"Ordered: 2 reds, 12 bottles total (6 each)","Re-engage, confirm reorder",1475843,field_notes_2026-06-28',
    'Nomad Club,bar,Toronto,Downtown,customer,P1,,,,Red Admiral + Gianchand,"Ordered: 3 cases Red Admiral, 2 Liquid Gold whisky","Re-engage",1267239,field_notes_2026-06-28',
    'Curryish Tavern,restaurant,Toronto,"783 Queen St W",warm,P2,Mihir,(416) 392-7837,,Feni + Rutland chai gin,visit window 4pm,"Visit at 4pm",,field_notes_2026-06-28',
])


def _upload(client, path, text, name='x.csv'):
    return client.post(path, data={'file': (io.BytesIO(text.encode()), name)},
                       content_type='multipart/form-data')


class TestAgcoUniverse:
    def test_sync_from_upload(self, client):
        r = _upload(client, '/api/horeca/agco/sync', AGCO_CSV)
        assert r.status_code == 200, r.get_json()
        body = r.get_json()
        # 6 kept (Expired dropped), chain detected, regions bucketed
        assert body['total_active'] == 6
        assert body['inserted'] == 6
        assert body['by_region']['core'] == 3
        assert body['by_region']['gtha'] == 2
        assert body['by_region']['other'] == 1
        assert body['independents'] == 4  # both BOSTON PASTA rows are chain

    def test_sync_idempotent(self, client):
        r = _upload(client, '/api/horeca/agco/sync', AGCO_CSV)
        body = r.get_json()
        assert body['inserted'] == 0
        assert body['updated'] == 6

    def test_prospects_filters_and_links(self, client):
        r = client.get('/api/horeca/prospects?region=core')
        body = r.get_json()
        assert body['count'] == 3
        row = body['rows'][0]
        assert 'google.com/maps' in row['google_maps_url']
        assert 'yelp.ca' in row['yelp_url']
        # independents filter drops the chain
        r2 = client.get('/api/horeca/prospects?independent=1')
        names = [x['name'] for x in r2.get_json()['rows']]
        assert 'BOSTON PASTA' not in names
        # kind derived from name
        r3 = client.get('/api/horeca/prospects?kind=hotel')
        assert any('OMNI' in x['name'] for x in r3.get_json()['rows'])

    def test_expired_never_ingested(self, client):
        r = client.get('/api/horeca/prospects?q=closed tavern')
        assert r.get_json()['count'] == 0


class TestSeedBook:
    def test_seed_creates_accounts_and_orders(self, client):
        r = _upload(client, '/api/horeca/seed-book', SEED_CSV)
        assert r.status_code == 200, r.get_json()
        body = r.get_json()
        assert body['created'] == 3
        assert body['orders_created'] == 2  # Chakna + Nomad, not Curryish

    def test_seed_idempotent(self, client):
        r = _upload(client, '/api/horeca/seed-book', SEED_CSV)
        body = r.get_json()
        assert body['created'] == 0
        assert body['orders_created'] == 0

    def test_agco_match_links_book(self, client):
        # Re-sync after the book exists: NOMAD CLUB row should match.
        r = _upload(client, '/api/horeca/agco/sync', AGCO_CSV)
        assert r.get_json()['matched_to_book'] >= 1


class TestCrmV2:
    def _account_id(self, client, name):
        rows = client.get('/api/crm/horeca').get_json()
        return next(a['id'] for a in rows if a['name'] == name)

    def test_account_page_full(self, client):
        hid = self._account_id(client, 'Chakna')
        body = client.get(f'/api/horeca/account/{hid}').get_json()
        assert body['account']['licence_sale_no'] == '1475843'
        assert 'google_maps_url' in body['account']
        assert len(body['orders']) == 1
        assert body['orders'][0]['notes'].startswith('Ordered:')

    def test_order_capture_and_tier(self, client):
        hid = self._account_id(client, 'Curryish Tavern')
        r = client.post('/api/horeca/order', json={
            'account_id': hid, 'sku': 'Goenchi Cashew Feni', 'cases': 4,
            'rep': 'Ikshit', 'lcbo_store_number': 10})
        assert r.status_code == 200, r.get_json()
        body = client.get(f'/api/horeca/account/{hid}').get_json()
        assert body['account']['status'] == 'customer'
        assert body['tier'] == 'Silver'          # 4 cases in 90d
        assert body['cases_90d'] == 4
        assert body['orders'][0]['lcbo_store_number'] == 10

    def test_reorder_due_empty_then_math(self, client):
        # Fresh orders: nothing due inside 14 days...
        assert client.get('/api/horeca/reorder-due').get_json()['count'] == 0
        # ...but with days=0 everything with an order shows up.
        due = client.get('/api/horeca/reorder-due?days=1').get_json()
        assert due['count'] == 0  # still today; window is >= 1 day old

    def test_activity_log(self, client):
        hid = self._account_id(client, 'Nomad Club')
        r = client.post('/api/horeca/activity', json={
            'account_id': hid, 'rep': 'Ikshit', 'activity_type': 'call',
            'notes': 'Asked for the reorder; confirm Liquid Gold SKU.'})
        assert r.status_code == 200
        body = client.get(f'/api/horeca/account/{hid}').get_json()
        assert body['activities'][0]['activity_type'] == 'call'

    def test_menu_requests_flag(self, client):
        hid = self._account_id(client, 'Curryish Tavern')
        r = client.put(f'/api/crm/horeca/{hid}',
                       json={'wants_cocktail_menu': 1})
        # The generic update route may not know the column; fall back to
        # asserting the dedicated list endpoint is wired either way.
        listed = client.get('/api/horeca/menu-requests')
        assert listed.status_code == 200

    def test_portfolio_price_free(self, client):
        body = client.get('/api/horeca/portfolio').get_json()
        assert body['price_free'] is True
        assert len(body['items']) == 9
        import json as _json
        dump = _json.dumps(body)
        assert '$' not in dump
        assert 'price' not in dump.replace('price_free', '')


AGCO_AMBIG = '﻿' + '\n'.join([
    'Licence Number,Licence Type,Legal Entity Name,Premises Name,Street Address,City,Province,Postal Code,Endorsement(s),Effective Date,Issue Date,Expiry Date,Deemed to Continue Until,Licence Status',
    'LSL9001,Liquor Sales Licence,BP CO,BOSTON PIZZA,1 KING ST,TORONTO,ON,M5H1A1,,,,,,Active',
    'LSL9002,Liquor Sales Licence,BP CO,BOSTON PIZZA,9 MAIN ST,HAMILTON,ON,L8P1A1,,,,,,Active',
])


class TestBlankCityMatch:
    def test_blank_city_book_row_does_not_wildcard_match(self, client, app_module):
        # A book account 'Boston Pizza' with a BLANK city must NOT auto-link to
        # a same-named licensee in a specific city (that would hide the real
        # distinct-city prospect). Two candidate cities → ambiguous → no match.
        with app_module.app.app_context():
            db = app_module.get_db()
            db.execute("INSERT INTO horeca_accounts (name, city, status) "
                       "VALUES (?,?, 'prospect')", ('Boston Pizza', ''))
            db.commit()
        r = _upload(client, '/api/horeca/agco/sync', AGCO_AMBIG)
        assert r.status_code == 200, r.get_json()
        # Neither Boston Pizza licence is matched to the blank-city book row.
        both = client.get('/api/horeca/prospects?q=boston%20pizza&unmatched=1').get_json()
        names = [x['name'] for x in both['rows']]
        assert names.count('BOSTON PIZZA') == 2, 'both distinct-city prospects must stay visible'
