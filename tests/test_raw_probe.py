"""The raw probe stays read-only, can read any weekday file in the rolling
window by name, and returns every requested row up to the higher cap."""
import importlib.util
import io
import os
import sys
import tempfile
import zipfile

import pytest

ROOT = os.path.join(os.path.dirname(__file__), '..')
sys.path.insert(0, ROOT)


def _line(d, sku, store, status, qty, name='WOLFHEAD COFFEE W'):
    return f"{d.replace('-', '')}{sku}{name:<17.17}{store:04d}{status} {qty:09d}"


def _zip(lines, member='alldlyinventoryMON.dat'):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
        z.writestr(member, '\n'.join(lines) + '\n')
    return buf.getvalue()


@pytest.fixture(scope='module')
def app_module():
    prev = os.environ.get('DB_DIR')
    tmp = tempfile.mkdtemp(prefix='probe_')
    os.environ.pop('DATABASE_URL', None)
    os.environ['DB_DIR'] = tmp
    os.environ['SALVAGE_RESTORE_DISABLED'] = '1'
    for m in list(sys.modules):
        if m == 'app' or m.startswith('app.'):
            del sys.modules[m]
    spec = importlib.util.spec_from_file_location('app', os.path.join(ROOT, 'app.py'))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    yield mod
    if prev is None:
        os.environ.pop('DB_DIR', None)
    else:
        os.environ['DB_DIR'] = prev
    for m in list(sys.modules):
        if m == 'app' or m.startswith('app.'):
            del sys.modules[m]


class _Fake:
    calls = []

    def __init__(self, *a, **k):
        pass

    def _filename_for_weekday(self, source, wd):
        return f'alldlyinventory{wd}.zip'

    def download_option(self, source, filename=None, **k):
        _Fake.calls.append((source, filename))
        lines = [_line('2026-11-02', '0669408', s, 'L', 5) for s in range(1, 451)]
        return _zip(lines), filename or 'alldlyinventoryTUE.zip', '2026-11-02'


def test_day_param_reads_the_named_file_and_cap_is_raised(app_module, monkeypatch):
    monkeypatch.setattr(app_module, 'SODClient', _Fake)
    c = app_module.app.test_client()
    _Fake.calls.clear()
    r = c.get('/api/sod/raw-probe?sku=669408&max_lines=1000&day=mon')
    assert r.status_code == 200, r.data
    j = r.get_json()
    assert _Fake.calls == [('daily_a', 'alldlyinventoryMON.zip')]
    assert j['file_name'] == 'alldlyinventoryMON.zip'
    k = j['requested_skus']['0669408']
    assert k['rows'] == 450 and len(k['raw_lines']) == 450, 'every row returned under the new cap'


def test_no_day_still_reads_the_freshest_file(app_module, monkeypatch):
    monkeypatch.setattr(app_module, 'SODClient', _Fake)
    c = app_module.app.test_client()
    _Fake.calls.clear()
    r = c.get('/api/sod/raw-probe?sku=669408')
    assert r.status_code == 200
    assert _Fake.calls == [('daily_a', None)]
    assert len(r.get_json()['requested_skus']['0669408']['raw_lines']) == 40, 'default stays 40'


def test_bad_day_is_refused(app_module, monkeypatch):
    monkeypatch.setattr(app_module, 'SODClient', _Fake)
    c = app_module.app.test_client()
    assert c.get('/api/sod/raw-probe?sku=669408&day=FUNDAY').status_code == 400
