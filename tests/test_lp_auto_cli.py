"""CLI contracts observed at an isolated HTTP service boundary."""
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import socket
import threading
from types import SimpleNamespace

import pytest

from open_trader import cli


ROOT = '/api/prediction-arbitrage/lp/auto/'


@pytest.fixture
def service():
    control = SimpleNamespace(
        state={
            'desired_running': False, 'pause_confirmed': True,
            'runtime_state': 'paused', 'budget_usd': '100',
            'target_buy_count': 5, 'buy_price_level': 2, 'config_version': 7,
            'slots': {'active': 2, 'pending': 1, 'canceling': 1, 'occupied': 4},
            'funds': {'inventory_cost_usd': '20', 'buy_reserved_usd': '30',
                      'available_usd': '50', 'spendable_usd': None},
            'block_reasons': ['financial_facts_unknown'],
            'last_check_at': '2026-10-08T01:02:03+00:00', 'last_check_error': None,
        },
        orders=[{'id': 'buy-1', 'side': 'BUY'}, {'id': 'sell-1', 'side': 'SELL'}],
        requests=[], rejection=None, reply=None, fault=None,
        csrf='fixture-csrf-secret', cookie='fixture-cookie-secret',
        entered=threading.Event(), release=threading.Event(),
    )

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def respond(self, code, body):
            raw = json.dumps(body).encode()
            self.send_response(code)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(raw)))
            if self.path.endswith('/venues'):
                self.send_header('Set-Cookie', f'session={control.cookie}; Path=/')
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            control.requests.append(('GET', self.path, None, dict(self.headers)))
            if control.fault == 'redirect-get':
                self.send_response(302)
                self.send_header('Location', '/redirect-target')
                self.send_header('Content-Length', '0')
                self.end_headers()
            elif self.path.endswith('/venues'):
                self.respond(200, {'csrf_token': control.csrf})
            elif self.path == ROOT + 'state':
                self.respond(200, control.state)
            else:
                self.respond(404, {'error': 'unexpected_path'})

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            control.requests.append(('POST', self.path, body, dict(self.headers)))
            if (self.headers.get('Cookie') != f'session={control.cookie}'
                or self.headers.get('X-CSRF-Token') != control.csrf
                or self.headers.get('Origin') != control.url
                or self.headers.get('Host') != control.url.removeprefix('http://')):
                self.respond(403, {'error': 'authentication_failed'})
                return
            if control.fault == 'redirect-post':
                self.send_response(307)
                self.send_header('Location', '/redirect-target')
                self.send_header('Content-Length', '0')
                self.end_headers()
                return
            if control.rejection:
                self.respond(control.rejection[0], {'error': control.rejection[1]})
                return
            if self.path == ROOT + 'config':
                control.state.update({k: v for k, v in body.items() if k != 'expected_config_version'})
                control.state['config_version'] += 1
            elif self.path == ROOT + 'enable':
                control.state.update(desired_running=True, pause_confirmed=False, runtime_state='blocked')
            elif self.path == ROOT + 'pause':
                control.state.update(desired_running=False, pause_confirmed=True, runtime_state='paused')
            else:
                self.respond(404, {'error': 'unexpected_path'})
                return
            control.entered.set()
            if control.fault == 'disconnect':
                self.connection.shutdown(socket.SHUT_RDWR)
                self.connection.close()
                return
            if control.fault == 'timeout':
                assert control.release.wait(5), 'test must release the accepted write'
                return
            self.respond(200, control.reply if control.reply is not None else control.state)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    control.url = f'http://127.0.0.1:{server.server_port}'
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield control
    finally:
        control.release.set()
        server.shutdown()
        server.server_close()
        worker.join(5)
        assert not worker.is_alive()


def invoke(service, action, *args):
    try:
        return cli.main(['prediction-arb', 'lp-auto', action, '--url', service.url, *args])
    except SystemExit as exc:
        return exc.code


def posts(service):
    return [(path, body) for method, path, body, _ in service.requests if method == 'POST']


def test_status_reports_service_state_without_mutation(service, capsys):
    service.state.update(desired_running=True, runtime_state='blocked', pause_confirmed=False)
    before = deepcopy(service.state)
    assert invoke(service, 'status') == 0
    output = capsys.readouterr().out
    for literal in ('desired_running: ON', 'runtime_state: BLOCKED', 'budget_usd: 100',
                    'target_buy_count: 5', 'buy_price_level: 2', 'active: 2', 'pending: 1',
                    'canceling: 1', 'occupied: 4', 'inventory_cost_usd: 20', 'buy_reserved_usd: 30',
                    'available_usd: 50', 'spendable_usd: UNKNOWN', 'financial_facts_unknown',
                    'last_check_at: 2026-10-08T01:02:03+00:00', 'last_check_error: NONE'):
        assert literal in output
    assert 'LIVE' not in output
    assert invoke(service, 'status', '--json') == 0
    document = json.loads(capsys.readouterr().out)
    assert document['result'] == 'STATUS'
    assert document['state'] == before
    assert document['state']['funds']['spendable_usd'] is None
    assert posts(service) == []
    assert service.state == before
    service.state = {'desired_running': False, 'runtime_state': 'paused'}
    assert invoke(service, 'status') == 0
    output = capsys.readouterr().out
    assert 'budget_usd: UNKNOWN' in output and 'active: UNKNOWN' in output
    assert 'desired_running: OFF' in output and 'runtime_state: PAUSED' in output


@pytest.mark.parametrize('fields,expected', [
    ({'reason': None, 'last_check_error': None},
     ('reason: NONE', 'last_check_error: NONE')),
    ({}, ('reason: UNKNOWN', 'last_check_error: UNKNOWN')),
    ({'reason': 'account_read_failed', 'last_check_error': 'transport_error'},
     ('reason: account_read_failed', 'last_check_error: transport_error')),
], ids=['null', 'absent', 'concrete'])
def test_human_status_distinguishes_absent_null_and_error(service, capsys, fields, expected):
    service.state.pop('reason', None)
    service.state.pop('last_check_error', None)
    service.state.update(fields)
    before, orders = deepcopy(service.state), deepcopy(service.orders)
    assert invoke(service, 'status') == 0
    human = capsys.readouterr()
    assert human.err == ''
    assert service.state == before and service.orders == orders
    assert invoke(service, 'status', '--json') == 0
    machine = capsys.readouterr()
    assert machine.err == ''
    assert json.loads(machine.out) == {
        'result': 'STATUS', 'state': before, 'reason': None, 'next_action': None,
    }
    assert [(method, path, body) for method, path, body, _ in service.requests] == [
        ('GET', ROOT + 'state', None), ('GET', ROOT + 'state', None),
    ]
    assert service.state == before and service.orders == orders
    for literal in expected:
        assert literal in human.out.splitlines()


@pytest.mark.parametrize('slots,configuration,expected', [
    ({'active': 5, 'pending': 0, 'canceling': 0, 'occupied': 5}, {'target_buy_count': 5},
     ('buy_orders: 5/5', 'active: 5', 'pending: 0', 'canceling: 0', 'occupied: 5')),
    ({'active': 3, 'pending': 2, 'canceling': 0, 'occupied': 5}, {'target_buy_count': 5},
     ('buy_orders: 3/5', 'active: 3', 'pending: 2', 'canceling: 0', 'occupied: 5')),
    ({'pending': 2, 'canceling': 0, 'occupied': 5}, {'target_buy_count': 5},
     ('buy_orders: UNKNOWN/5', 'active: UNKNOWN', 'pending: 2', 'canceling: 0', 'occupied: 5')),
    ({'active': 5, 'pending': 0, 'canceling': 0, 'occupied': 5}, {},
     ('buy_orders: 5/UNKNOWN', 'active: 5', 'pending: 0', 'canceling: 0', 'occupied: 5')),
    ({'active': None, 'pending': 2, 'canceling': 0, 'occupied': 5}, {'target_buy_count': 5},
     ('buy_orders: UNKNOWN/5', 'active: UNKNOWN', 'pending: 2', 'canceling: 0', 'occupied: 5')),
    ({'active': 5, 'pending': 0, 'canceling': 0, 'occupied': 5}, {'target_buy_count': None},
     ('buy_orders: 5/UNKNOWN', 'target_buy_count: UNKNOWN', 'active: 5', 'pending: 0',
      'canceling: 0', 'occupied: 5')),
], ids=['full', 'pending', 'missing-active', 'missing-target', 'null-active', 'null-target'])
def test_human_status_reports_active_buy_progress(service, capsys, slots, configuration, expected):
    service.state['slots'] = slots
    service.state.pop('target_buy_count', None)
    service.state.update(configuration)
    before, orders = deepcopy(service.state), deepcopy(service.orders)
    assert invoke(service, 'status') == 0
    human = capsys.readouterr()
    assert human.err == ''
    assert service.state == before and service.orders == orders
    assert invoke(service, 'status', '--json') == 0
    machine = capsys.readouterr()
    assert machine.err == ''
    assert json.loads(machine.out) == {
        'result': 'STATUS', 'state': before, 'reason': None, 'next_action': None,
    }
    assert [(method, path, body) for method, path, body, _ in service.requests] == [
        ('GET', ROOT + 'state', None), ('GET', ROOT + 'state', None),
    ]
    assert service.state == before and service.orders == orders
    for literal in expected:
        assert literal in human.out.splitlines()


@pytest.mark.parametrize('context,expected', [
    ({'check_in_progress': True,
      'last_round': {'checked_at': '2026-10-09T01:02:03+00:00',
                     'reason': 'rotation_awaiting_reconciliation'}},
     ('check_in_progress: true', 'last_round_checked_at: 2026-10-09T01:02:03+00:00',
      'last_round_reason: rotation_awaiting_reconciliation')),
    ({'check_in_progress': False,
      'last_round': {'checked_at': '2026-10-09T01:02:03+00:00', 'reason': None}},
     ('check_in_progress: false', 'last_round_checked_at: 2026-10-09T01:02:03+00:00',
      'last_round_reason: NONE')),
    ({}, ('check_in_progress: UNKNOWN', 'last_round_checked_at: UNKNOWN',
          'last_round_reason: UNKNOWN')),
    ({'check_in_progress': 0,
      'last_round': {'checked_at': '2026-10-09T01:02:03+00:00', 'reason': None}},
     ('check_in_progress: UNKNOWN', 'last_round_checked_at: 2026-10-09T01:02:03+00:00',
      'last_round_reason: NONE')),
    ({'check_in_progress': 1,
      'last_round': {'checked_at': '2026-10-09T01:02:03+00:00', 'reason': None}},
     ('check_in_progress: UNKNOWN', 'last_round_checked_at: 2026-10-09T01:02:03+00:00',
      'last_round_reason: NONE')),
    ({'check_in_progress': 'false',
      'last_round': {'checked_at': '2026-10-09T01:02:03+00:00', 'reason': None}},
     ('check_in_progress: UNKNOWN', 'last_round_checked_at: 2026-10-09T01:02:03+00:00',
      'last_round_reason: NONE')),
    ({'check_in_progress': None,
      'last_round': {'checked_at': '2026-10-09T01:02:03+00:00', 'reason': None}},
     ('check_in_progress: UNKNOWN', 'last_round_checked_at: 2026-10-09T01:02:03+00:00',
      'last_round_reason: NONE')),
    ({'check_in_progress': False, 'last_round': None},
     ('check_in_progress: false', 'last_round_checked_at: UNKNOWN', 'last_round_reason: UNKNOWN')),
    ({'check_in_progress': False, 'last_round': ['unexpected']},
     ('check_in_progress: false', 'last_round_checked_at: UNKNOWN', 'last_round_reason: UNKNOWN')),
], ids=['in-progress', 'completed-without-reason', 'legacy', 'check-zero', 'check-one',
        'check-string-false', 'check-null', 'round-null', 'round-list'])
def test_human_status_reports_round_context_without_overriding_runtime(service, capsys, context, expected):
    service.state.update(desired_running=True, pause_confirmed=False, runtime_state='running')
    service.state.update(context)
    before, orders = deepcopy(service.state), deepcopy(service.orders)
    assert invoke(service, 'status') == 0
    human = capsys.readouterr()
    assert human.err == ''
    assert service.state == before and service.orders == orders
    assert invoke(service, 'status', '--json') == 0
    machine = capsys.readouterr()
    assert machine.err == ''
    assert json.loads(machine.out) == {
        'result': 'STATUS', 'state': before, 'reason': None, 'next_action': None,
    }
    assert [(method, path, body) for method, path, body, _ in service.requests] == [
        ('GET', ROOT + 'state', None), ('GET', ROOT + 'state', None),
    ]
    assert service.state == before and service.orders == orders
    assert 'runtime_state: RUNNING' in human.out.splitlines()
    assert 'runtime_state: BLOCKED' not in human.out.splitlines()
    for literal in expected:
        assert literal in human.out.splitlines()


def test_config_saves_parameters_without_enabling(service, capsys):
    assert invoke(service, 'config', '--budget', '100', '--target-buys', '5', '--bid-level', '2', '--json') == 0
    document = json.loads(capsys.readouterr().out)
    assert document['result'] == 'CONFIGURED'
    for key, expected in (('budget_usd', '100'), ('target_buy_count', 5),
                          ('buy_price_level', 2), ('config_version', 8), ('desired_running', False)):
        assert document['state'][key] == expected
    assert posts(service) == [(ROOT + 'config', {'budget_usd': '100', 'target_buy_count': 5,
                                              'buy_price_level': 2, 'expected_config_version': 7})]
    service.requests.clear()
    assert invoke(service, 'config', '--budget', '120', '--target-buys', '0', '--json') == 0
    document = json.loads(capsys.readouterr().out)
    assert document['state']['buy_price_level'] == 2
    assert document['state']['budget_usd'] == '120'
    assert document['state']['target_buy_count'] == 0
    assert document['state']['desired_running'] is False
    assert document['state']['config_version'] == 9
    assert posts(service) == [(ROOT + 'config', {'budget_usd': '120', 'target_buy_count': 0,
                                              'expected_config_version': 8})]
    assert service.orders == [{'id': 'buy-1', 'side': 'BUY'}, {'id': 'sell-1', 'side': 'SELL'}]


@pytest.mark.parametrize('option,value', [
    ('--budget', '-1'), ('--budget', 'NaN'), ('--budget', 'Infinity'),
    ('--budget', '-Infinity'), ('--target-buys', '-1'), ('--target-buys', '1.5'),
    ('--bid-level', '0'), ('--bid-level', '3'), ('--bid-level', '1.5'),
])
def test_config_rejects_invalid_arguments_before_requests(service, capsys, option, value):
    arguments = {'--budget': '100', '--target-buys': '5', '--bid-level': '2'}
    arguments[option] = value
    assert invoke(service, 'config', *(f'{key}={item}' for key, item in arguments.items()), '--json') == 2
    document = json.loads(capsys.readouterr().out)
    assert document['result'] == 'UNKNOWN' and document['reason']
    assert service.requests == []
    assert service.state['config_version'] == 7


@pytest.mark.parametrize('code,reason,running', [
    (409, 'config_version_changed', False),
    (400, 'pause_and_finish_automatic_buys_before_configuring', True),
    (400, 'pause_and_finish_automatic_buys_before_configuring', False),
])
def test_config_rejection_preserves_state_without_automatic_repair(service, capsys, code, reason, running):
    service.state['desired_running'] = running
    service.state['pause_confirmed'] = not running
    service.rejection = (code, reason)
    before, orders = deepcopy(service.state), deepcopy(service.orders)
    assert invoke(service, 'config', '--budget', '120', '--target-buys', '6', '--json') == 2
    document = json.loads(capsys.readouterr().out)
    assert reason in document['reason']
    assert document['result'] == 'UNKNOWN'
    assert document['state'] == before
    assert service.state == before and service.orders == orders
    assert posts(service) == [(ROOT + 'config', {'budget_usd': '120', 'target_buy_count': 6,
                                              'expected_config_version': 7})]


def test_on_confirms_saved_mode_and_reports_blocked_state(service, capsys):
    before, orders = deepcopy(service.state), deepcopy(service.orders)
    assert invoke(service, 'on') == 0
    output = capsys.readouterr().out
    assert 'result: ON' in output and 'runtime_state: BLOCKED' in output
    assert 'financial_facts_unknown' in output
    assert 'filled' not in output.lower() and 'LIVE' not in output
    assert posts(service) == [(ROOT + 'enable', {'confirm': True})]
    for key in ('budget_usd', 'target_buy_count', 'buy_price_level', 'config_version'):
        assert service.state[key] == before[key]
    assert service.orders == orders
    assert invoke(service, 'on', '--json') == 0
    document = json.loads(capsys.readouterr().out)
    assert document['result'] == 'ON'
    assert document['state']['desired_running'] is True
    assert document['state']['runtime_state'] == 'blocked'
    assert document['state']['budget_usd'] == '100'
    assert document['state']['target_buy_count'] == 5
    assert document['state']['buy_price_level'] == 2
    assert posts(service) == [(ROOT + 'enable', {'confirm': True})] * 2


@pytest.mark.parametrize('action', ['off', 'pause'])
def test_off_and_pause_preserve_orders_and_require_confirmation(service, capsys, action):
    service.state.update(desired_running=True, pause_confirmed=False, runtime_state='running')
    before, orders = deepcopy(service.state), deepcopy(service.orders)
    assert invoke(service, action) == 0
    output = capsys.readouterr().out
    assert 'result: PAUSED' in output
    assert 'existing orders and protection remain' in output
    assert service.state['desired_running'] is False and service.state['pause_confirmed'] is True
    assert service.orders == orders
    for key in ('budget_usd', 'target_buy_count', 'buy_price_level', 'config_version'):
        assert service.state[key] == before[key]
    assert invoke(service, action, '--json') == 0
    assert json.loads(capsys.readouterr().out)['result'] == 'PAUSED'
    for reply in ({'desired_running': False, 'pause_confirmed': False},
                  {'desired_running': True, 'pause_confirmed': True}):
        service.reply = reply
        assert invoke(service, action, '--json') == 2
        assert json.loads(capsys.readouterr().out)['result'] == 'UNKNOWN'
    assert posts(service) == [(ROOT + 'pause', {'confirm': True})] * 4
    assert service.orders == orders


@pytest.mark.parametrize('action,patch', [
    ('on', {}), ('on', {'desired_running': 'true'}), ('on', {'desired_running': 1}),
    ('on', {'desired_running': False}),
    ('on', {'desired_running': True, 'pause_confirmed': True}),
    ('on', {'desired_running': True, 'runtime_state': 'paused'}),
    ('off', {'desired_running': 'false', 'pause_confirmed': True}),
    ('off', {'desired_running': False, 'pause_confirmed': 'true'}),
    ('off', {'desired_running': False}),
    ('off', {'desired_running': False, 'pause_confirmed': True, 'runtime_state': 'running'}),
    ('config', {'budget_usd': '99'}), ('config', {'budget_usd': 'NaN'}),
    ('config', {'budget_usd': 100}), ('config', {'target_buy_count': '5'}),
    ('config', {'target_buy_count': True}), ('config', {'buy_price_level': '2'}),
    ('config', {'buy_price_level': None}), ('config', {'config_version': 7}),
    ('config', {'config_version': '8'}), ('config', {'config_version': None}),
    ('config', {'desired_running': 'false'}), ('config', {'desired_running': True}),
    ('config', {'pause_confirmed': False}),
    ('on', []), ('off', 'not-a-state'), ('config', []),
])
def test_unconfirmed_control_results_are_unknown(service, capsys, action, patch):
    if action == 'config' and isinstance(patch, dict):
        service.reply = dict(service.state, config_version=8)
        service.reply.update(patch)
        if service.reply.get('config_version') is None:
            service.reply.pop('config_version')
        if service.reply.get('buy_price_level') is None:
            service.reply.pop('buy_price_level')
    else:
        service.reply = patch
    arguments = ('--budget', '100', '--target-buys', '5', '--bid-level', '2') if action == 'config' else ()
    assert invoke(service, action, *arguments, '--json') == 2
    document = json.loads(capsys.readouterr().out)
    assert document['result'] == 'UNKNOWN'
    if isinstance(service.reply, dict):
        assert document['state'] == service.reply
    assert 'status' in document['next_action']
    assert len(posts(service)) == 1


@pytest.mark.parametrize('fault', [
    'nonloopback', 'credentials', 'empty-credentials', 'path', 'https',
    'redirect-get', 'redirect-post', 'missing-csrf', 'forbidden', 'unreachable',
    'timeout', 'disconnect', 'reflected-unconfirmed',
    'timeout-nan', 'timeout-infinite', 'timeout-zero', 'timeout-negative', 'timeout-overflow',
])
def test_transport_failures_do_not_retry_or_expose_credentials(service, capsys, monkeypatch, fault):
    service.fault = fault
    url, timeout = service.url, '2'
    expected_posts = 0
    if fault == 'nonloopback':
        url = 'http://example.com'
    elif fault == 'credentials':
        url = service.url.replace('http://', f'http://{service.csrf}:{service.cookie}@')
    elif fault == 'empty-credentials':
        url = service.url.replace('http://', 'http://@')
    elif fault == 'path':
        url += '/path'
    elif fault == 'https':
        url = url.replace('http://', 'https://')
    elif fault == 'missing-csrf':
        service.csrf = None
    elif fault == 'forbidden':
        service.rejection = (403, f'forbidden {service.csrf} {service.cookie}')
        expected_posts = 1
    elif fault in ('redirect-post', 'timeout', 'disconnect', 'reflected-unconfirmed'):
        expected_posts = 1
        if fault == 'timeout':
            timeout = '0.5'
        if fault == 'reflected-unconfirmed':
            service.reply = {'desired_running': 'true', 'last_check_error': f'{service.csrf} {service.cookie}'}
        if fault == 'disconnect':
            # An external proxy must not receive this loopback mutation.
            monkeypatch.setenv('http_proxy', service.url)
            monkeypatch.setenv('HTTP_PROXY', service.url)
            monkeypatch.setenv('no_proxy', '')
            monkeypatch.setenv('NO_PROXY', '')
    elif fault.startswith('timeout-'):
        timeout = {'timeout-nan': 'NaN', 'timeout-infinite': 'Infinity',
                   'timeout-zero': '0', 'timeout-negative': '-1', 'timeout-overflow': '1e308'}[fault]
    unavailable = socket.socket()
    try:
        if fault == 'unreachable':
            # Reserve an ephemeral port without listening; never probe a production port.
            unavailable.bind(('127.0.0.1', 0))
            url = f'http://127.0.0.1:{unavailable.getsockname()[1]}'
        try:
            action = 'status' if fault == 'timeout-overflow' else 'on'
            code = cli.main(['prediction-arb', 'lp-auto', action, '--url', url,
                             '--timeout', timeout, '--json'])
        except SystemExit as exc:
            code = exc.code
    finally:
        unavailable.close()
        service.release.set()
    captured = capsys.readouterr()
    assert code == 2
    document = json.loads(captured.out)
    assert document['result'] == 'UNKNOWN'
    assert 'status' in document['next_action']
    if fault == 'timeout-overflow':
        assert 'Traceback' not in captured.out + captured.err
    assert 'fixture-csrf-secret' not in captured.out + captured.err
    assert 'fixture-cookie-secret' not in captured.out + captured.err
    assert len(posts(service)) == expected_posts
    assert all(path != '/redirect-target' for _, path, _, _ in service.requests)
    if fault in ('timeout', 'disconnect'):
        assert service.entered.is_set()
        assert service.state['desired_running'] is True  # Accepted write was not rolled back.
    if fault in ('nonloopback', 'credentials', 'empty-credentials', 'path', 'https') or fault.startswith('timeout-'):
        assert service.requests == []


def test_non_lp_unknown_arguments_keep_argparse_errors(service, capsys):
    with pytest.raises(SystemExit) as failure:
        cli.main(['account-sync-status', '--account-url', service.url, '--json', '--unknown-option'])
    assert failure.value.code == 2
    captured = capsys.readouterr()
    assert captured.out == ''
    assert 'unrecognized arguments' in captured.err
    assert '--unknown-option' in captured.err
    assert service.requests == []


@pytest.mark.parametrize('action', ['status', 'config', 'on', 'off', 'pause'])
def test_unknown_lp_arguments_use_json_error_contract(service, capsys, action):
    before, orders = deepcopy(service.state), deepcopy(service.orders)
    arguments = ('--budget', '100', '--target-buys', '5') if action == 'config' else ()
    assert invoke(service, action, *arguments, '--json', '--unknown-option') == 2
    captured = capsys.readouterr()
    document = json.loads(captured.out)
    assert document['result'] == 'UNKNOWN'
    assert document['reason']
    assert document['state'] is None
    assert 'status' in document['next_action']
    assert 'usage:' not in captured.err and 'Traceback' not in captured.err
    assert service.requests == []
    assert service.state == before and service.orders == orders
