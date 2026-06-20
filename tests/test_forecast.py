"""Forecasting v0 + Rep Coach v0 tests.

Covers the 4-week depletion moving-average math (including the restock
blind spot the old two-point velocity had), the RED/YELLOW/STALL/NEW/DROPPED
classification, and the coach endpoint contract (exactly 3 bullets).

Run with: pytest tests/test_forecast.py -v
"""
import os
import sqlite3
import sys
import tempfile
from datetime import date, timedelta

import pytest

# Force SQLite in an isolated temp dir so we never touch production Postgres
# (app.py reads DB_DIR, not DB_PATH).
os.environ.pop('DATABASE_URL', None)
_TMP = tempfile.mkdtemp(prefix='lcbo_forecast_test_')
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


@pytest.fixture(scope='module')
def seeded(app_module):
    """Seed a synthetic 28-day SOD history for tracked SKU 0045378."""
    sku = '0045378'
    anchor = date(2026, 6, 9)
    conn = sqlite3.connect(TEST_DB)
    conn.execute("INSERT OR IGNORE INTO stores (store_number, account, city, rep) "
                 "VALUES (1001, 'LCBO #1001 Test', 'Toronto', 'Ikshit')")
    conn.execute("INSERT OR IGNORE INTO stores (store_number, account, city, rep) "
                 "VALUES (1002, 'LCBO #1002 Stall', 'Ottawa', 'Namit')")

    # Store 1001: sells ~1/day with a restock in the middle.
    # on_hand: 14,13,12,...,8 then restock to 20, then 19,18,... (daily -1)
    oh = 14
    for i in range(28):
        d = anchor - timedelta(days=27 - i)
        if i == 7:
            oh = 20  # restock event
        elif i > 0:
            oh -= 1
        conn.execute(
            "INSERT OR REPLACE INTO sod_inventory "
            "(sku, store_number, snapshot_date, status, on_hand, product_name) "
            "VALUES (?, 1001, ?, 'L', ?, 'Rock Paper Rum Indian Spiced')",
            (sku, d.isoformat(), oh))

    # Store 1002: 12 on hand, flat for 28 days → STALL
    for i in range(28):
        d = anchor - timedelta(days=27 - i)
        conn.execute(
            "INSERT OR REPLACE INTO sod_inventory "
            "(sku, store_number, snapshot_date, status, on_hand, product_name) "
            "VALUES (?, 1002, ?, 'L', 12, 'Rock Paper Rum Indian Spiced')",
            (sku, d.isoformat(),))
    conn.commit()
    conn.close()
    return {'sku': sku, 'anchor': anchor}


@pytest.fixture
def client(app_module):
    return app_module.app.test_client()


# ========================================================================
# Pure classification rules
# ========================================================================

class TestForecastClassify:
    def test_out_of_stock_while_selling_is_red(self, app_module):
        flag, cover, _ = app_module._forecast_classify(0, 2.0, 27)
        assert flag == 'RED'
        assert cover == 0.0

    def test_under_week_of_cover_is_red(self, app_module):
        # 2 on hand at 3/wk → ~4.7 days
        flag, cover, _ = app_module._forecast_classify(2, 3.0, 27)
        assert flag == 'RED'
        assert cover <= 7

    def test_below_reorder_pace_is_yellow(self, app_module):
        # 6 on hand at 3/wk → 14 days — inside the 21-day reorder window
        flag, cover, _ = app_module._forecast_classify(6, 3.0, 27)
        assert flag == 'YELLOW'
        assert 7 < cover <= 21

    def test_healthy_cover_is_green(self, app_module):
        flag, cover, _ = app_module._forecast_classify(30, 2.0, 27)
        assert flag == 'GREEN'
        assert cover > 21

    def test_stock_not_moving_is_stall(self, app_module):
        flag, cover, _ = app_module._forecast_classify(12, 0.0, 27)
        assert flag == 'STALL'
        assert cover is None

    def test_thin_history_is_new(self, app_module):
        flag, _, _ = app_module._forecast_classify(5, 1.0, 6)
        assert flag == 'NEW'

    def test_stale_pair_is_dropped(self, app_module):
        flag, _, _ = app_module._forecast_classify(5, 1.0, 27, stale_days=10)
        assert flag == 'DROPPED'

    def test_delisted_here_is_dropped(self, app_module):
        flag, _, _ = app_module._forecast_classify(0, 0.0, 27, status='F')
        assert flag == 'DROPPED'


# ========================================================================
# Depletion aggregation — the restock blind spot
# ========================================================================

class TestDepletionAggregation:
    def test_restock_does_not_hide_depletion(self, app_module, seeded):
        """Store 1001 sold ~1/day with a mid-window restock. The old
        two-point math (first vs last on_hand) would report almost zero
        velocity; the day-over-day sum must see ~26 units depleted."""
        with app_module.app.test_request_context():
            rows = app_module._forecast_agg_rows([seeded['sku']], store_number=1001)
        assert len(rows) == 1
        r = rows[0]
        # 27 daily decrements minus the restock day (no drop that day) = 26
        assert r['depleted_window'] == 26
        assert r['restocked_window'] > 0
        assert r['span_days'] == 27
        # weekly MA ≈ 26 * 7/27 ≈ 6.7/wk
        assert 6.0 <= r['weekly_ma'] <= 7.5

    def test_flat_stock_has_zero_velocity(self, app_module, seeded):
        with app_module.app.test_request_context():
            rows = app_module._forecast_agg_rows([seeded['sku']], store_number=1002)
        assert len(rows) == 1
        assert rows[0]['depleted_window'] == 0
        assert rows[0]['weekly_ma'] == 0.0


# ========================================================================
# Endpoint contracts
# ========================================================================

class TestForecastEndpoint:
    def test_forecast_flags_both_stores(self, client, seeded):
        resp = client.get('/api/crm/forecast?portfolio=all&nocache=1')
        assert resp.status_code == 200
        data = resp.get_json()
        by_store = {r['store_number']: r for r in data['rows']}
        # 1001: ~6.7/wk, anchor on_hand 6 → ~6 days cover → RED
        assert by_store[1001]['flag'] == 'RED'
        # 1002: 12 sitting, zero movement → STALL
        assert by_store[1002]['flag'] == 'STALL'
        assert data['counts']['RED'] >= 1
        assert data['counts']['STALL'] >= 1

    def test_store_forecast_single(self, client, seeded):
        resp = client.get('/api/crm/store/1001/forecast?nocache=1')
        assert resp.status_code == 200
        rows = resp.get_json()['rows']
        assert any(r['sku'] == seeded['sku'] and r['flag'] == 'RED' for r in rows)

    def test_rep_filter(self, client, seeded):
        resp = client.get('/api/crm/forecast?rep=Namit&nocache=1')
        assert resp.status_code == 200
        rows = resp.get_json()['rows']
        assert all(r['rep'] == 'Namit' for r in rows)
        assert any(r['store_number'] == 1002 for r in rows)


class TestCoachEndpoint:
    def test_exactly_three_bullets(self, client, seeded):
        resp = client.get('/api/crm/store/1001/coach?nocache=1')
        assert resp.status_code == 200
        data = resp.get_json()
        assert len(data['bullets']) == 3
        for b in data['bullets']:
            assert b['text']
            assert b['tag']
            assert b['why']  # every bullet must cite its data

    def test_red_store_gets_reorder_bullet_first(self, client, seeded):
        resp = client.get('/api/crm/store/1001/coach?nocache=1')
        bullets = resp.get_json()['bullets']
        assert bullets[0]['tag'] in ('REORDER', 'OOS')
        assert bullets[0]['flag'] == 'RED'

    def test_stall_store_gets_activation_bullet(self, client, seeded):
        resp = client.get('/api/crm/store/1002/coach?nocache=1')
        bullets = resp.get_json()['bullets']
        assert any(b['tag'] == 'ACTIVATE' for b in bullets)

    def test_unknown_store_404(self, client, seeded):
        resp = client.get('/api/crm/store/999999/coach?nocache=1')
        assert resp.status_code == 404
