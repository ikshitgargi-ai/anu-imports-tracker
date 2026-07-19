"""OUTREACH GATE — the never-again tests.

These exist because of a real incident: ~400 cold emails a day went out in
batches, and a venue that asked to be removed kept receiving them. Under CASL
that is the clearest kind of violation. Every test below encodes one promise:

  1. A suppressed address can never be sent to, by any path.
  2. Suppression outranks a later consent record.
  3. No consent + no evidence = no send.
  4. Daily caps are enforced inside the gate, so no caller can skip them.
  5. Blocked attempts are still logged, so the block is auditable.

Run: python3 -m pytest tests/test_outreach_gate.py -v
"""
import os
import sys
import tempfile

import pytest

os.environ.pop('DATABASE_URL', None)
os.environ.pop('ADMIN_TOKEN', None)
os.environ.pop('API_KEY', None)
_TMP = tempfile.mkdtemp(prefix='anu_outreach_test_')
os.environ['DB_DIR'] = _TMP

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

VICTIM = 'manager@thatrestaurant.example'   # the venue that unsubscribed


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


class TestSuppressionIsAbsolute:
    def test_unsubscribe_blocks_every_further_send(self, client):
        # Consent first, so this is a contact we were legitimately emailing.
        assert client.post('/api/outreach/consent', json={
            'destination': VICTIM, 'kind': 'implied_published',
            'evidence': 'published on venue website contact page 2026-07-19'
        }).status_code == 200
        assert client.get(
            f'/api/outreach/check?destination={VICTIM}').get_json()['allowed']

        # They unsubscribe.
        assert client.post('/api/outreach/suppress', json={
            'destination': VICTIM, 'reason': 'replied asking to stop'
        }).status_code == 200

        # The exact failure from the incident: another batch goes out.
        chk = client.get(f'/api/outreach/check?destination={VICTIM}').get_json()
        assert chk['allowed'] is False
        assert 'SUPPRESSED' in chk['reason']

        r = client.post('/api/outreach/log', json={
            'destination': VICTIM, 'channel': 'email', 'subject': 'batch 7'})
        assert r.status_code == 403
        assert r.get_json()['status'] == 'BLOCKED'

    def test_case_and_whitespace_cannot_slip_past(self, client):
        for variant in (f'  {VICTIM.upper()}  ', VICTIM.title()):
            r = client.post('/api/outreach/log', json={
                'destination': variant, 'channel': 'email'})
            assert r.status_code == 403, f'{variant!r} got through'

    def test_new_consent_does_not_resurrect_a_suppressed_contact(self, client):
        # Someone re-imports a list and re-records consent. They must STILL
        # not receive anything: suppression outranks consent, permanently.
        client.post('/api/outreach/consent', json={
            'destination': VICTIM, 'kind': 'express',
            'evidence': 're-imported from an old spreadsheet 2026-07-19'})
        chk = client.get(f'/api/outreach/check?destination={VICTIM}').get_json()
        assert chk['allowed'] is False
        assert 'SUPPRESSED' in chk['reason']

    def test_withdrawn_consent_auto_suppresses(self, client):
        dest = 'owner@anotherplace.example'
        client.post('/api/outreach/consent', json={
            'destination': dest, 'kind': 'express',
            'evidence': 'signed up at the trade show booth 2026-06-01'})
        assert client.get(
            f'/api/outreach/check?destination={dest}').get_json()['allowed']
        client.post('/api/outreach/consent', json={
            'destination': dest, 'kind': 'withdrawn'})
        assert client.get(
            f'/api/outreach/check?destination={dest}').get_json()['allowed'] is False


class TestConsentRequired:
    def test_no_consent_no_send(self, client):
        chk = client.get(
            '/api/outreach/check?destination=cold@neverheardofus.example'
        ).get_json()
        assert chk['allowed'] is False
        assert 'consent' in chk['reason'].lower()

    def test_consent_without_evidence_is_refused(self, client):
        r = client.post('/api/outreach/consent', json={
            'destination': 'x@y.example', 'kind': 'implied_published',
            'evidence': 'idk'})
        assert r.status_code == 400
        assert 'evidence required' in r.get_json()['error']

    def test_expired_consent_stops_sending(self, client, app_module):
        dest = 'expired@venue.example'
        client.post('/api/outreach/consent', json={
            'destination': dest, 'kind': 'implied_business',
            'evidence': 'bought from us, 2-year window',
            'expires_at': '2020-01-01'})
        chk = client.get(f'/api/outreach/check?destination={dest}').get_json()
        assert chk['allowed'] is False


class TestDailyCaps:
    def test_cap_is_enforced_inside_the_gate(self, client, app_module):
        # 400/day is exactly what went wrong. The cap lives in the gate so a
        # caller cannot loop around it.
        assert app_module._OUTREACH_DAILY_CAP['email'] < 400
        cap = app_module._OUTREACH_DAILY_CAP['email']
        for i in range(cap + 3):
            # The app's own 50-req/sec IP limiter would fire on a tight loop,
            # so drain it: we are testing the outreach cap, not that one.
            app_module._rate_buckets.clear()
            d = f'venue{i}@caps.example'
            client.post('/api/outreach/consent', json={
                'destination': d, 'kind': 'implied_published',
                'evidence': f'published contact page for venue {i} 2026-07-19'})
            client.post('/api/outreach/log', json={
                'destination': d, 'channel': 'email', 'subject': 'intro'})
        app_module._rate_buckets.clear()
        # A fully-consented contact must STILL be refused once the day's cap
        # is spent: the cap is the thing under test, so consent is in place.
        client.post('/api/outreach/consent', json={
            'destination': 'onemore@caps.example', 'kind': 'express',
            'evidence': 'asked us to send the list at the Feb trade show'})
        chk = client.get(
            '/api/outreach/check?destination=onemore@caps.example').get_json()
        assert chk['allowed'] is False
        assert 'cap reached' in chk['reason']
        assert chk['sent_today'] >= cap


class TestAuditTrail:
    def test_blocked_attempts_are_still_logged(self, client, app_module):
        app_module._rate_buckets.clear()
        with app_module.app.app_context():
            db = app_module.get_db()
            n = db.execute(
                "SELECT COUNT(*) FROM outreach_log WHERE blocked_reason != ''"
            ).fetchone()[0]
        assert n > 0, 'blocks must be auditable, not silent'

    def test_scoreboard_ranks_by_bottles_not_volume(self, client, app_module):
        app_module._rate_buckets.clear()
        client.post('/api/outreach/log', json={
            'destination': '', 'channel': 'visit', 'rep': 'Namit',
            'outcome': 'ordered', 'bottles_moved': 24, 'sku': '0049902'})
        sb = client.get('/api/outreach/scoreboard?days=90').get_json()
        assert sb['rows'][0]['channel'] == 'visit'   # bottles, not blast size
        assert sb['suppression_list_size'] >= 1
        visit = next(r for r in sb['rows'] if r['channel'] == 'visit')
        assert visit['bottles_per_touch'] > 0


class TestNonElectronicChannels:
    def test_visits_and_calls_do_not_need_an_email(self, client, app_module):
        app_module._rate_buckets.clear()
        # CASL governs electronic messages. A walk-in is not one, and it is
        # the channel that actually sells.
        r = client.post('/api/outreach/log', json={
            'channel': 'visit', 'rep': 'Namit', 'outcome': 'tasting booked'})
        assert r.status_code == 200


class TestTeamQueues:
    def test_each_role_returns_work(self, client, app_module):
        app_module._rate_buckets.clear()
        for role in ('sales', 'marketing', 'outreach'):
            r = client.get(f'/api/team/queue?role={role}')
            assert r.status_code == 200, (role, r.get_json())
            assert r.get_json()['role'] == role

    def test_bad_role_rejected(self, client, app_module):
        app_module._rate_buckets.clear()
        assert client.get('/api/team/queue?role=ceo').status_code == 400

    def test_outreach_queue_leads_with_calls_not_email(self, client,
                                                       app_module):
        # The whole point: the first-contact queue must never hand a rep a
        # cold email. Calls are unregulated by CASL and sell better.
        app_module._rate_buckets.clear()
        rows = client.get('/api/team/queue?role=outreach').get_json()['rows']
        assert all(r['action'] == 'call' for r in rows)
        assert all(r.get('phone') for r in rows)

    def test_depots_never_get_a_tasting(self, client, app_module):
        app_module._rate_buckets.clear()
        rows = client.get('/api/team/queue?role=marketing').get_json()['rows']
        assert all(r['store_number'] not in app_module._DEPOTS for r in rows)


class TestFailClosedRendering:
    """The research verdict that mattered most: a rep cannot message what the
    screen will not show. Suppressed venues must have their contact details
    REMOVED from payloads, not merely flagged."""

    def test_suppressed_contact_details_are_hidden(self, client, app_module):
        app_module._rate_buckets.clear()
        with app_module.app.app_context():
            db = app_module.get_db()
            row = {'name': 'Quiet Place', 'email': VICTIM,
                   'phone': '416-555-0000', 'website': 'https://x.example'}
            out = app_module._dnc_scrub(db, row)
        assert out['do_not_contact'] is True
        assert out['email'] == '' and out['phone'] == ''
        assert out['website'] == ''
        assert 'asked not to be contacted' in out['dnc_note']

    def test_clean_contact_is_untouched(self, client, app_module):
        with app_module.app.app_context():
            db = app_module.get_db()
            row = {'name': 'Fine Place', 'email': 'hello@fine.example',
                   'phone': '416-555-1111'}
            out = app_module._dnc_scrub(db, row)
        assert out['email'] == 'hello@fine.example'
        assert 'do_not_contact' not in out

    def test_bulk_list_screening(self, client, app_module):
        app_module._rate_buckets.clear()
        r = client.post('/api/outreach/suppression-check', json={
            'destinations': [VICTIM, 'ok1@venue.example', 'ok2@venue.example']})
        body = r.get_json()
        assert body['blocked'] == [VICTIM]
        assert body['safe_count'] == 2


class TestAllElectronicChannelsGated:
    def test_whatsapp_sms_and_dm_are_treated_like_email(self, client,
                                                        app_module):
        # CASL is technology-neutral. A WhatsApp to a suppressed venue is the
        # same violation as an email to them.
        app_module._rate_buckets.clear()
        for ch in ('whatsapp', 'sms', 'dm'):
            r = client.post('/api/outreach/log', json={
                'destination': VICTIM, 'channel': ch})
            assert r.status_code == 403, f'{ch} was not gated'
