"""SALES COMMAND — commission engine, payments, next-best, top-100, scoreboard,
move-bottles. The layer that makes the tool a driver of sales.

Run: python3 -m pytest tests/test_sales_command.py -v
"""
import io
import os
import sys
import tempfile

import pytest

os.environ.pop('DATABASE_URL', None)
os.environ.pop('ADMIN_TOKEN', None)
os.environ.pop('API_KEY', None)
_TMP = tempfile.mkdtemp(prefix='anu_sales_cmd_test_')
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
    return mod


@pytest.fixture
def client(app_module):
    return app_module.app.test_client()


AGCO_CSV = '﻿' + '\n'.join([
    'Licence Number,Licence Type,Legal Entity Name,Premises Name,Street Address,City,Province,Postal Code,Endorsement(s),Effective Date,Issue Date,Expiry Date,Deemed to Continue Until,Licence Status',
    'LSL70001,Liquor Sales Licence,A,TANDOORI PALACE,1 KING ST,TORONTO,ON,M5H1A1,,,,,,Active',
    'LSL70002,Liquor Sales Licence,B,CURRY HOUSE,2 QUEEN ST,BRAMPTON,ON,L6T1A1,,,,,,Active',
    'LSL70003,Liquor Sales Licence,C,THE GRAND BANQUET HALL,3 MAIN ST,VAUGHAN,ON,L4L1A1,,,,,,Active',
    'LSL70004,Liquor Sales Licence,D,PLAIN BISTRO,4 BAY ST,TORONTO,ON,M5J1A1,,,,,,Active',
    'LSL70005,Liquor Sales Licence,E,BIRYANI EXPRESS,5 HWY7,RICHMOND HILL,ON,L4B1A1,,,,,,Active',
])


@pytest.fixture(scope='module')
def seeded(app_module):
    client = app_module.app.test_client()
    client.post('/api/horeca/agco/sync',
                data={'file': (io.BytesIO(AGCO_CSV.encode()), 'a.csv')},
                content_type='multipart/form-data')
    client.get('/api/horeca/sweep/status')  # ensure enrichment cols
    with app_module.app.app_context():
        db = app_module.get_db()
        # a customer account with a pin + a Rutland order by Namit
        db.execute("INSERT INTO horeca_accounts (name, city, status, priority, "
                   "products_carried, lat, lng, phone) VALUES "
                   "('Chai Corner','Toronto','customer','P1',"
                   "'Rutland Square Chai Gin',43.65,-79.38,'416-1')")
        db.execute("INSERT INTO horeca_accounts (name, city, status, priority, "
                   "products_carried, lat, lng) VALUES "
                   "('New Warm Bar','Toronto','warm','P2','Feni',43.66,-79.39)")
        db.commit()
        hid = db.execute("SELECT id FROM horeca_accounts WHERE name='Chai Corner'").fetchone()[0]
        db.execute("INSERT INTO deals (horeca_account_id, sku, stage, expected_units, "
                   "cases, owner_rep) VALUES (?, 'Rutland Square Chai Spiced Gin', "
                   "'ordered', 24, 2, 'Namit')", (hid,))
        # Rutland SOD stock: two stores with on-hand + store coords
        db.execute("INSERT OR IGNORE INTO stores (store_number, account, city, lat, lng) "
                   "VALUES (555,'Airport & Bovaird','Brampton',43.72,-79.75)")
        db.execute("INSERT OR IGNORE INTO stores (store_number, account, city, lat, lng) "
                   "VALUES (601,'Queen & McLaughlin','Brampton',43.68,-79.78)")
        db.execute("INSERT INTO sod_inventory (sku, store_number, snapshot_date, "
                   "status, on_hand, product_name) VALUES "
                   "('0049902', 555, '2026-07-14', 'L', 84, 'Rutland Square')")
        db.execute("INSERT INTO sod_inventory (sku, store_number, snapshot_date, "
                   "status, on_hand, product_name) VALUES "
                   "('0049902', 601, '2026-07-14', 'L', 28, 'Rutland Square')")
        # licensee near store 555 for the move-bottles pitch ring
        ph = '?'
        db.execute(f"UPDATE agco_licensees SET lat=43.73, lng=-79.74 "
                   f"WHERE licence_number='LSL70002'")
        db.commit()
    return True


class TestCommission:
    def test_program_and_tracker(self, seeded, client):
        r = client.post('/api/sales/commission/program', json={
            'sku': '49902', 'per_unit_bonus': 6.0, 'rep': 'Namit',
            'notes': 'Rutland push: $6 per bottle across the stock'})
        assert r.status_code == 200, r.get_json()
        assert r.get_json()['sku'] == '0049902'
        # log a tasting credit (IST)
        r2 = client.post('/api/sales/bottles-moved', json={
            'rep': 'Namit', 'sku': '0049902', 'units': 5, 'source': 'ist',
            'store_number': 555, 'note': 'in-store tasting moved 5'})
        assert r2.status_code == 200
        t = client.get('/api/sales/commission?rep=Namit').get_json()
        prog = next(p for p in t['programs'] if p['sku'] == RUTLAND)
        assert prog['per_unit_bonus'] == 6.0
        assert prog['units_from_orders'] == 24      # explicit units win over cases
        assert prog['units_from_tastings'] == 5
        assert prog['earned'] == round(29 * 6.0, 2)
        assert prog['stock_pool_units'] == 112      # 84 + 28
        assert prog['pool_value'] == round(112 * 6.0, 2)

    def test_program_upsert_not_duplicate(self, seeded, client):
        client.post('/api/sales/commission/program', json={
            'sku': '49902', 'per_unit_bonus': 6.5, 'rep': 'Namit'})
        t = client.get('/api/sales/commission?rep=Namit').get_json()
        progs = [p for p in t['programs'] if p['sku'] == RUTLAND]
        assert len(progs) == 1 and progs[0]['per_unit_bonus'] == 6.5
        client.post('/api/sales/commission/program', json={
            'sku': '49902', 'per_unit_bonus': 6.0, 'rep': 'Namit'})


class TestPayments:
    def test_payment_lifecycle(self, seeded, client, app_module):
        with app_module.app.app_context():
            db = app_module.get_db()
            did = db.execute("SELECT id FROM deals WHERE stage='ordered' LIMIT 1").fetchone()[0]
        r = client.post(f'/api/horeca/order/{did}/payment',
                        json={'payment_status': 'invoiced'})
        assert r.status_code == 200
        r2 = client.post(f'/api/horeca/order/{did}/payment',
                         json={'payment_status': 'paid', 'note': 'e-transfer'})
        assert r2.status_code == 200
        with app_module.app.app_context():
            db = app_module.get_db()
            row = db.execute("SELECT payment_status, paid_at FROM deals WHERE id=?",
                             (did,)).fetchone()
        assert row[0] == 'paid' and row[1] is not None

    def test_payment_rejects_bad_status(self, seeded, client):
        r = client.post('/api/horeca/order/1/payment', json={'payment_status': 'maybe'})
        assert r.status_code == 400


class TestNextBest:
    def test_location_ranked_actions(self, seeded, client):
        r = client.get('/api/sales/next-best?lat=43.65&lng=-79.38&limit=5')
        assert r.status_code == 200, r.get_json()
        rows = r.get_json()['rows']
        assert len(rows) >= 2
        # customer w/ old order... orders are fresh here, so check-in beats warm
        names = [x['name'] for x in rows]
        assert 'Chai Corner' in names and 'New Warm Bar' in names
        assert all('action' in x and x['km'] < 31 for x in rows)

    def test_requires_location(self, seeded, client):
        assert client.get('/api/sales/next-best').status_code == 422


class TestTop100:
    def test_indian_build_keywords_and_areas(self, seeded, client):
        r = client.post('/api/sales/top100/build', json={'list': 'indian'})
        assert r.status_code == 200, r.get_json()
        body = client.get('/api/sales/top100?list=indian').get_json()
        names = [x['name'] for x in body['rows']]
        assert 'TANDOORI PALACE' in names
        assert 'CURRY HOUSE' in names
        assert 'BIRYANI EXPRESS' in names
        assert 'PLAIN BISTRO' not in names          # not Indian
        areas = {x['area'] for x in body['rows']}
        assert 'Brampton' in areas and 'Richmond Hill' in areas

    def test_research_import_merges_and_matches(self, seeded, client):
        r = client.post('/api/sales/top100/import', json={
            'list': 'indian',
            'entries': [
                {'name': 'Tandoori Palace', 'city': 'Toronto', 'why': 'iconic room'},
                {'name': 'Brand New Research Spot', 'city': 'Toronto',
                 'area': 'Downtown / Core Toronto', 'why': 'buzzy opening'},
            ]})
        assert r.status_code == 200, r.get_json()
        body = r.get_json()
        assert body['added'] == 1                   # Tandoori Palace deduped
        listed = client.get('/api/sales/top100?list=indian').get_json()
        research = [x for x in listed['rows'] if x['source'] == 'research']
        assert any(x['name'] == 'Brand New Research Spot' for x in research)

    def test_rebuild_preserves_research(self, seeded, client):
        client.post('/api/sales/top100/build', json={'list': 'indian'})
        listed = client.get('/api/sales/top100?list=indian').get_json()
        assert any(x['source'] == 'research' for x in listed['rows'])


class TestScoreboardAndPlay:
    def test_scoreboard_shape(self, seeded, client):
        b = client.get('/api/sales/scoreboard').get_json()
        assert 'points_of_distribution' in b
        assert b['points_of_distribution']['horeca_customers'] >= 1
        assert any(s['sku'] == RUTLAND and s['stock_units'] == 112
                   for s in b['per_sku'])
        assert b['tasting_bonus_earned'] >= 30.0     # 5 units x $6

    def test_move_bottles_play(self, seeded, client):
        r = client.get(f'/api/sales/move-bottles?sku=49902')
        assert r.status_code == 200, r.get_json()
        b = r.get_json()
        assert b['total_stock_units'] == 112
        assert b['stock_by_store'][0]['store_number'] == 555   # deepest first
        assert len(b['tasting_candidates']) >= 1
        # licensee CURRY HOUSE placed ~1.2km from store 555 → in the pitch ring
        assert any('CURRY HOUSE' in v['name'] for v in b['pitch_venues_near_stock'])
        assert any(rr['name'] == 'Chai Corner' for rr in b['reorder_customers'])
        assert 'play' in b

    def test_move_bottles_rejects_unknown_sku(self, seeded, client):
        assert client.get('/api/sales/move-bottles?sku=99999').status_code == 400


class TestAutopilot:
    def test_generate_and_burn_down(self, seeded, client, app_module):
        # backdate the Chai Corner order so it's reorder-due
        with app_module.app.app_context():
            db = app_module.get_db()
            db.execute("UPDATE deals SET created_at='2026-06-01 12:00:00' "
                       "WHERE stage='ordered'")
            db.commit()
        r = client.post('/api/sales/actions/generate', json={})
        assert r.status_code == 200, r.get_json()
        assert r.get_json()['created'] >= 2   # reorder + store tasting (84 btl @ 555)
        q = client.get('/api/sales/actions?rep=Namit').get_json()
        kinds = {a['kind'] for a in q['rows']}
        assert 'reorder_call' in kinds and 'store_tasting' in kinds
        # store actions carry the ADDRESS beside the number (house rule)
        st = next(a for a in q['rows'] if a['kind'] == 'store_tasting')
        assert st['store_number'] and st['store_label']
        # idempotent: re-generate adds nothing while actions stay open
        assert client.post('/api/sales/actions/generate', json={}).get_json()['created'] == 0
        # burn one down
        aid = q['rows'][0]['id']
        assert client.post(f'/api/sales/actions/{aid}/status',
                           json={'status': 'done'}).status_code == 200
        left = client.get('/api/sales/actions?rep=Namit').get_json()
        assert all(a['id'] != aid for a in left['rows'])

    def test_move_bottles_and_reconcile_carry_address(self, seeded, client):
        b = client.get('/api/sales/move-bottles?sku=49902').get_json()
        assert b['stock_by_store'][0]['address']   # street address present
