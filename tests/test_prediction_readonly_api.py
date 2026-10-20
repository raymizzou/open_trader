import hashlib
import json

import pytest

from readonly_http_support import MemoryHTTP


MINI_TOKEN = 'mini-fixture-token-0123456789abcdef'
AIR_TOKEN = 'air-fixture-token-0123456789abcdef'


@pytest.fixture
def wire(monkeypatch):
    value = MemoryHTTP(monkeypatch)
    yield value
    value.close()


def private_files(tmp_path):
    directory = tmp_path / 'private'
    directory.mkdir(mode=0o700)
    registry = directory / 'tokens.json'
    registry.write_text(json.dumps({'mini': hashlib.sha256(MINI_TOKEN.encode()).hexdigest(),
                                    'air': hashlib.sha256(AIR_TOKEN.encode()).hexdigest()}))
    registry.chmod(0o600)
    token = directory / 'client-token'
    token.write_text(MINI_TOKEN)
    token.chmod(0o600)
    return registry, token


def test_readonly_adapter_offline_policy_and_revocation(tmp_path, monkeypatch, wire):
    import open_trader.prediction_readonly_api as adapter
    registry, _ = private_files(tmp_path)
    payload = dict(mode='production', mutations='enabled', production_owner=True, git_sha='a'*40,
                   stale=True, checked_at='old', csrf_token='must-remove',
                   nested=[{'session_token': 'must-remove', 'session_id': 'business-session',
                            'session': {'value': 'must-remove'}, 'csrfToken': 'must-remove'}])
    wire.backend(payload)
    wire.patch_server(adapter)
    server = adapter.create_prediction_readonly_api(token_file=registry, port=8770)
    def request(path, token=MINI_TOKEN, method='GET'):
        headers = {'Cookie': 'browser', 'X-CSRF-Token': 'browser'}
        if token is not None:
            headers['Authorization'] = 'Bearer ' + token
        return wire.request(server.server_port, method, path, headers)
    path = '/api/prediction-arbitrage/lp/dashboard'
    assert request(path, None).status == 401
    assert request(path, 'wrong').status == 401
    assert not any(call[0] == 8769 for call in wire.calls)
    response = request(path)
    assert response.status == 200
    assert response.getheader('Set-Cookie') is None
    assert response.getheader('X-CSRF-Token') is None
    assert json.loads(response.read()) == dict(mode='production', mutations='enabled', production_owner=True,
        git_sha='a'*40, stale=True, checked_at='old', access='read_only', nested=[{'session_id': 'business-session'}])
    forwarded = wire.calls[-1][3]
    assert not {'Cookie', 'Authorization', 'X-CSRF-Token'} & forwarded.keys()
    assert forwarded['X-Open-Trader-Read-Only'] == 'true'
    before = len([c for c in wire.calls if c[0] == 8769])
    for target in ('/api/prediction-arbitrage/lp/sessions/current', '/api/prediction-arbitrage/state',
                   path+'?refresh=true', path+'%2f..', path+'?url=http://example.invalid',
                   'http://example.invalid'+path, '//api/prediction-arbitrage/lp/dashboard', path+'?x=1', path+'?x=1&x=2',
                   path+'/%2e%2e',
                   '/api/prediction-arbitrage/lp/account/trades?limit=1&limit=2',
                   '/api/prediction-arbitrage/lp/account/trades?limit=101',
                   '/api/prediction-arbitrage/lp/auto/reports/2026-02-30'):
        assert request(target).status == 403, target
    for method in ('POST', 'PUT', 'DELETE', 'PATCH', 'HEAD', 'OPTIONS', 'CONNECT', 'TRACE', 'BREW'):
        assert request(path, method=method).status == 405
    assert len([c for c in wire.calls if c[0] == 8769]) == before
    for target in ('/healthz', '/api/prediction-arbitrage/venues',
                   '/api/prediction-arbitrage/lp/account/trades?offset=10&limit=20',
                   '/api/prediction-arbitrage/lp/auto/state', '/api/prediction-arbitrage/lp/auto/reservations',
                   '/api/prediction-arbitrage/lp/auto/reports',
                   '/api/prediction-arbitrage/lp/auto/reports/2026-10-19',
                   '/api/prediction-arbitrage/lp/reports/2026-10-19'):
        assert request(target).status == 200, target
    registry.write_text(json.dumps({'air': hashlib.sha256(AIR_TOKEN.encode()).hexdigest()}))
    assert request(path).status == 401
    assert request(path, AIR_TOKEN).status == 200
    registry.chmod(0o644)
    assert request(path, AIR_TOKEN).status == 401
    registry.chmod(0o600)
    registry.write_text('broken')
    assert request(path).status == 401
    registry.write_text(json.dumps({'air': hashlib.sha256(AIR_TOKEN.encode()).hexdigest()}))
    registry.unlink()
    registry.symlink_to(tmp_path / 'missing')
    assert request(path, AIR_TOKEN).status == 401


def test_readonly_venues_does_not_issue_auth(tmp_path, monkeypatch, wire):
    from types import SimpleNamespace
    import open_trader.prediction_service as service
    wire.patch_server(service, '_PredictionHTTPServer')
    runtime = SimpleNamespace(mode='production', production_owner=True, state='RUNNING')
    server = service.create_prediction_server(runtime=runtime, port=8769,
        session_token='fixture-session', csrf_token='fixture-csrf', runtime_metadata={})
    path = '/api/prediction-arbitrage/venues'
    response = wire.request(server.server_port, 'GET', path, {'X-Open-Trader-Read-Only': 'true'})
    assert response.status == 200
    assert response.getheader('Set-Cookie') is None
    value = json.loads(response.read())
    assert not value.get('csrf_token')
    assert value['mode'] == 'production' and value['mutations'] == 'enabled'
    normal = wire.request(server.server_port, 'GET', path)
    assert normal.status == 200
    assert 'fixture-session' in normal.getheader('Set-Cookie')
    assert json.loads(normal.read())['csrf_token'] == 'fixture-csrf'


def test_readonly_gateway_injects_private_token_and_never_falls_back(tmp_path, monkeypatch, wire):
    import open_trader.frontend_gateway as gateway
    import open_trader.prediction_readonly_api as adapter
    registry, token = private_files(tmp_path)
    health = dict(module='prediction_service', status='running', git_sha='a'*40,
                  source_state='clean', mode='production', production_owner=True, mutations='enabled',
                  checked_at='backend-time', stale=True)
    wire.backend(health)
    wire.patch_server(adapter)
    adapter.create_prediction_readonly_api(token_file=registry, port=8770)
    wire.patch_server(gateway)
    static = tmp_path / 'static'
    static.mkdir()
    (static / 'index.html').write_text('<body>dashboard</body>')
    route = tmp_path / 'route.json'
    route.write_text(json.dumps(dict(schema_version='open_trader.frontend_gateway.prediction_route.v1',
        mode='service', operation_id='fixture', updated_at='fixture')))
    config = gateway.FrontendGatewayConfig(static_dir=static, prediction_route_path=route,
        prediction_upstream_port=8770, prediction_access='read_only', prediction_token_file=token,
        prediction_tailnet_host='tokyo.example.ts.net', prediction_expected_sha='a'*40,
        prediction_expected_mode='production', prediction_only=True)
    server = gateway.create_frontend_gateway(config=config, host='127.0.0.1', port=8766)
    response = wire.request(8766)
    html = response.read().decode()
    assert 'data-prediction-access="read_only"' in html and 'data-prediction-only="true"' in html
    assert MINI_TOKEN not in html
    path = '/api/prediction-arbitrage/venues'
    response = wire.request(8766, 'GET', path, {'Authorization': 'Bearer browser-token',
        'Cookie': 'browser-session', 'X-CSRF-Token': 'browser-csrf'})
    assert response.status == 200
    value = json.loads(response.read())
    assert value['mode'] == 'production' and value['mutations'] == 'enabled'
    assert value['git_sha'] == 'a'*40 and value['stale'] is True and value['checked_at'] == 'backend-time'
    assert value['access'] == 'read_only'
    transport = [call for call in wire.calls if call[0] == 8770][-1]
    assert transport[3]['Authorization'] == 'Bearer ' + MINI_TOKEN
    assert 'Cookie' not in transport[3] and 'X-CSRF-Token' not in transport[3]
    before = len([call for call in wire.calls if call[0] == 8770])
    for method in ('POST', 'PUT', 'DELETE', 'PATCH', 'OPTIONS'):
        assert wire.request(8766, method, path, body=b'{}').status == 405
    for target in ('/api/prediction-arbitrage/lp/sessions/current',
                   '/api/prediction-arbitrage/%76enues', '//api/prediction-arbitrage/venues',
                   path+'?x=1', path+'/%2e%2e', 'http://example.invalid'+path):
        assert wire.request(8766, 'GET', target).status == 403
    assert len([call for call in wire.calls if call[0] == 8770]) == before
    health['git_sha'] = 'b'*40
    response = wire.request(8766, 'GET', path)
    assert response.status == 503 and json.loads(response.read())['code'] == 'prediction_identity_unavailable'
    health['git_sha'] = 'a'*40
    wire.servers.pop(8770).server_close()
    assert wire.request(8766, 'GET', path).status == 503
    route.write_text(route.read_text().replace('service', 'legacy'))
    assert wire.request(8766, 'GET', path).status == 503
    assert not any(call[0] in (8767, 8768) for call in wire.calls)


def test_readonly_gateway_requires_prediction_only(tmp_path, monkeypatch, wire):
    import open_trader.frontend_gateway as gateway
    wire.patch_server(gateway)
    _, token = private_files(tmp_path)
    route = tmp_path / 'route.json'
    route.write_text(json.dumps(dict(schema_version='open_trader.frontend_gateway.prediction_route.v1',
        mode='service', operation_id='fixture', updated_at='fixture')))
    config = gateway.FrontendGatewayConfig(static_dir=tmp_path, prediction_route_path=route,
        prediction_upstream_port=18879, prediction_access='read_only', prediction_token_file=token,
        prediction_tailnet_host='100.64.0.1', prediction_expected_sha='a'*40,
        prediction_expected_mode='production')
    with pytest.raises(ValueError, match='prediction-only'):
        gateway.create_frontend_gateway(config=config, host='127.0.0.1', port=8765)
    assert wire.calls == []


def test_readonly_adapter_rejects_unsafe_files_binds_and_upstream(tmp_path, monkeypatch, wire):
    import open_trader.prediction_readonly_api as adapter
    registry, _ = private_files(tmp_path)
    for host in ('0.0.0.0', '100.64.0.1', 'example.com', '::'):
        with pytest.raises(ValueError, match='loopback'):
            adapter.create_prediction_readonly_api(token_file=registry, host=host)
    registry.chmod(0o640)
    with pytest.raises(ValueError, match='0600'):
        adapter.create_prediction_readonly_api(token_file=registry)
    registry.chmod(0o600)
    registry.parent.chmod(0o755)
    with pytest.raises(ValueError, match='0700'):
        adapter.create_prediction_readonly_api(token_file=registry)
    registry.parent.chmod(0o700)
    wire.patch_server(adapter)
    server = adapter.create_prediction_readonly_api(token_file=registry, port=8770)
    headers = {'Authorization': 'Bearer ' + MINI_TOKEN}
    response = wire.request(server.server_port, path='/healthz', headers=headers)
    assert response.status == 503
    assert json.loads(response.read()) == {'code': 'prediction_service_unavailable'}
    monkeypatch.setattr(adapter, 'MAX_RESPONSE_BYTES', 256)
    backend = wire.backend({'body': 'x'*300})
    assert wire.request(server.server_port, path='/healthz', headers=headers).status == 503
    wire.servers.pop(8769)
    backend.server_close()
    # No redirect follows and no transport token reaches the redirect target.
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    class Redirect(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(302)
            self.send_header('Location', 'http://example.invalid/?token=leak')
            self.send_header('Content-Length', '2')
            self.end_headers()
            self.wfile.write(b'{}')
        def log_message(self, *args):
            pass
    backend = ThreadingHTTPServer(('127.0.0.1', 8769), Redirect, bind_and_activate=False)
    wire.servers[8769] = backend
    response = wire.request(server.server_port, path='/healthz', headers=headers)
    assert response.status == 503 and response.getheader('Location') is None
    assert not any(call[0] not in (8769, 8770) for call in wire.calls)


def test_readonly_client_health_auth_and_revoke(tmp_path, monkeypatch, wire):
    import open_trader.prediction_readonly_api as adapter
    from open_trader.prediction_client import backend_health
    registry, token = private_files(tmp_path)
    wire.patch_server(adapter)
    wire.backend(dict(module='prediction_service', git_sha='a'*40, mode='shadow', production_owner=False))
    adapter.create_prediction_readonly_api(token_file=registry, port=18879)
    config = dict(mode='shadow', access='read_only', token_file=str(token))
    value = backend_health(config)
    assert value['access'] == 'read_only'
    assert value['git_sha'] == 'a'*40 and value['mode'] == 'shadow'
    assert value['production_owner'] is False
    assert wire.calls[0][3]['Authorization'] == 'Bearer ' + MINI_TOKEN
    assert 'Authorization' not in wire.calls[1][3]
    registry.write_text('{}')
    with pytest.raises(ValueError, match='adapter unavailable'):
        backend_health(config)
    assert not any(call[0] in (8767, 8768) for call in wire.calls)


def test_gateway_control_auth_and_readonly_requires_prediction_only(tmp_path, monkeypatch, wire):
    from dataclasses import replace
    import open_trader.frontend_gateway as gateway
    wire.patch_server(gateway)
    wire.backend({'legacy': True}, 8767)
    wire.backend({'account': True}, 8768)
    wire.backend({'prediction': True}, 8769)
    route = tmp_path / 'route.json'
    route.write_text(json.dumps(dict(schema_version='open_trader.frontend_gateway.prediction_route.v1',
        mode='service', operation_id='fixture', updated_at='fixture')))
    config = gateway.FrontendGatewayConfig(static_dir=tmp_path, prediction_route_path=route)
    gateway.create_frontend_gateway(config=config, host='127.0.0.1', port=8766)
    headers = {'Cookie': 'ot_prediction_session=fixture', 'X-CSRF-Token': 'fixture-csrf',
               'Origin': 'http://127.0.0.1:8766'}
    response = wire.request(8766, 'POST', '/api/prediction-arbitrage/lp/auto/stop', headers, b'{"confirm":true}')
    assert response.status == 200
    assert response.getheader('Set-Cookie') == 'session=must-remove'
    assert json.loads(response.read()) == {'prediction': True}
    port, method, _, forwarded, body = wire.calls[-1]
    assert port == 8769 and method == 'POST' and body == b'{"confirm":true}'
    assert forwarded['Cookie'] == 'ot_prediction_session=fixture'
    assert forwarded['X-CSRF-Token'] == 'fixture-csrf'
    assert forwarded['Origin'] == 'http://127.0.0.1:8769'
    for path, expected_port, expected in (('/api/dashboard', 8767, {'legacy': True}),
                                         ('/api/v1/account/snapshot', 8768, {'account': True})):
        response = wire.request(8766, 'GET', path)
        assert response.status == 200 and json.loads(response.read()) == expected
        assert wire.calls[-1][0] == expected_port
        assert 'Authorization' not in wire.calls[-1][3]
    _, token = private_files(tmp_path)
    config = replace(config, prediction_access='read_only', prediction_token_file=token,
        prediction_tailnet_host='100.64.0.1', prediction_expected_sha='a'*40,
        prediction_expected_mode='shadow', prediction_upstream_port=18879)
    before = len(wire.calls)
    with pytest.raises(ValueError, match='prediction-only'):
        gateway.create_frontend_gateway(config=config, host='127.0.0.1', port=8765)
    assert len(wire.calls) == before


def test_control_gateway_strips_client_readonly_header(tmp_path, monkeypatch, wire):
    import open_trader.frontend_gateway as gateway
    wire.patch_server(gateway)
    wire.backend({'prediction': True}, 8769)
    route = tmp_path / 'route.json'
    route.write_text(json.dumps(dict(schema_version='open_trader.frontend_gateway.prediction_route.v1',
        mode='service', operation_id='fixture', updated_at='fixture')))
    config = gateway.FrontendGatewayConfig(static_dir=tmp_path, prediction_route_path=route)
    gateway.create_frontend_gateway(config=config, host='127.0.0.1', port=8766)
    response = wire.request(8766, 'POST', '/api/prediction-arbitrage/lp/auto/stop', {
        'Cookie': 'ot_prediction_session=fixture', 'X-CSRF-Token': 'fixture-csrf',
        'Origin': 'http://127.0.0.1:8766',
        'X-Open-Trader-Read-Only': 'true', 'x-open-trader-read-only': 'false'},
        b'{"confirm":true}')
    assert response.status == 200
    forwarded = {key.lower(): value for key, value in wire.calls[-1][3].items()}
    assert 'x-open-trader-read-only' not in forwarded
    assert forwarded['cookie'] == 'ot_prediction_session=fixture'
    assert forwarded['x-csrf-token'] == 'fixture-csrf'
    assert forwarded['origin'] == 'http://127.0.0.1:8769'


def test_readonly_client_token_minimum_length(tmp_path):
    import open_trader.prediction_readonly_api as adapter
    token = tmp_path / 'token'
    token.write_text('a' * 31)
    token.chmod(0o600)
    with pytest.raises(ValueError, match='private client token'):
        adapter.client_token(token)
    token.write_text('a' * 31 + '!')
    with pytest.raises(ValueError, match='private client token'):
        adapter.client_token(token)
    token.write_text('a' * 32)
    assert adapter.client_token(token) == 'a' * 32
    token.write_text('a' * 513)
    with pytest.raises(ValueError, match='private client token'):
        adapter.client_token(token)


def test_readonly_adapter_rejects_short_registered_token(tmp_path, monkeypatch, wire):
    import open_trader.prediction_readonly_api as adapter
    registry, _ = private_files(tmp_path)
    weak = 'a' * 31
    registry.write_text(json.dumps({'mini': hashlib.sha256(weak.encode()).hexdigest()}))
    wire.backend({'mode': 'production'})
    wire.patch_server(adapter)
    server = adapter.create_prediction_readonly_api(token_file=registry, port=8770)
    response = wire.request(server.server_port, 'GET', '/healthz',
        {'Authorization': 'Bearer ' + weak})
    assert response.status == 401
    assert not any(call[0] == 8769 for call in wire.calls)
