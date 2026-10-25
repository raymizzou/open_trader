"""Startup region policy through the public CLI and synthetic HTTP only."""
from contextlib import contextmanager
import json
from types import SimpleNamespace

import pytest

from tests.test_polymarket_order_probe import exchange, PRIVATE, SECRET

_UNSET = object()


@pytest.fixture
def read_auth(exchange, monkeypatch, capsys):
    from open_trader import cli, polymarket_trading

    monkeypatch.setenv('OPEN_TRADER_CREDENTIAL_BACKEND', 'file')
    monkeypatch.setenv('OPEN_TRADER_CREDENTIAL_FILE', exchange.args[-1])
    region = {'blocked': True, 'country': 'JP', 'region': '13', 'ip': '192.0.2.1'}
    observations = []

    @contextmanager
    def http_get(request, *, timeout):
        assert request.full_url == 'https://polymarket.com/api/geoblock'
        assert request.get_method() == 'GET'
        observations.append(request)
        if isinstance(region, Exception):
            raise region
        yield SimpleNamespace(read=lambda: json.dumps(region).encode())

    monkeypatch.setattr(polymarket_trading, 'urlopen', http_get)

    def invoke(*, payload=_UNSET, observe=True, require=True):
        nonlocal region
        if payload is not _UNSET:
            region = payload
        args = ['prediction-arb', 'wallet', 'read-auth', '--config', exchange.args[1]]
        if require:
            args.append('--require-trading-region')
        if observe:
            args.append('--observe-jp-api-region')
        code = cli.main(args)
        captured = capsys.readouterr()
        assert PRIVATE not in captured.out + captured.err
        assert SECRET not in captured.out + captured.err
        assert '192.0.2.1' not in captured.out + captured.err
        assert exchange.mutations == []
        return code, json.loads(captured.out)

    return invoke, exchange, observations


def test_read_auth_observes_jp_without_hiding_geoblock(read_auth):
    invoke, venue, observations = read_auth
    code, report = invoke()
    assert code == 0
    assert report['result'] == 'PASS'
    assert report['initialization']['status'] == 'ready'
    assert report['account_read']['status'] == 'ready'
    assert report['trading_region'] == {
        'status': 'blocked', 'blocked': True, 'country': 'JP', 'region': '13',
        'policy': 'jp_api_observation_only', 'startup_allowed': True,
    }
    assert report['guard'] == {'mutation_attempts': 0, 'notification_attempts': 0}
    assert any(r.url.path == '/auth/derive-api-key' for r in venue.requests)
    assert len(observations) == 1


@pytest.mark.parametrize('payload', [
    pytest.param({'blocked': True, 'country': 'US'}, id='blocked-us'),
    pytest.param({'blocked': True}, id='missing-country'),
    pytest.param({'blocked': 'true', 'country': 'JP'}, id='malformed-boolean'),
    pytest.param(None, id='malformed-payload'),
    pytest.param(TimeoutError('private-error-sentinel'), id='timeout'),
])
def test_read_auth_region_policy_keeps_non_jp_and_unknown_blocked(read_auth, payload):
    invoke, _, _ = read_auth
    code, report = invoke(payload=payload)
    assert code == 2 and report['result'] == 'BLOCKED'
    assert report['account_read']['status'] == 'ready'
    assert report['trading_region']['startup_allowed'] is False
    assert report['trading_region']['policy'] == 'strict'
    assert 'private-error-sentinel' not in json.dumps(report)


def test_read_auth_jp_observation_keeps_account_unknown_blocked(read_auth):
    invoke, venue, _ = read_auth
    venue.account_bad = True
    code, report = invoke()
    assert code == 2 and report['result'] == 'BLOCKED'
    assert report['account_read']['status'] == 'unknown'
    assert report['guard'] == {'mutation_attempts': 0, 'notification_attempts': 0}


def test_read_auth_jp_observation_keeps_auth_failure_blocked(read_auth):
    from pathlib import Path

    invoke, venue, _ = read_auth
    Path(venue.args[-1]).unlink()  # Missing synthetic file, never a real credential.
    code, report = invoke()
    assert code == 2 and report['result'] == 'BLOCKED'
    assert report['initialization']['status'] == 'unknown'
    assert report['account_read']['status'] == 'unknown'


def test_read_auth_jp_observation_requires_explicit_region_check(read_auth):
    invoke, venue, observations = read_auth
    code, report = invoke(require=False)
    assert code == 2 and report['result'] == 'BLOCKED'
    assert observations == [] and venue.requests == []


def test_read_auth_strict_region_check_still_blocks_jp(read_auth):
    invoke, _, _ = read_auth
    code, report = invoke(observe=False)
    assert code == 2 and report['result'] == 'BLOCKED'
    assert report['trading_region'] == {
        'status': 'blocked', 'reason': 'geoblock_denied_or_unavailable',
    }


def test_read_auth_allowed_region_passes_observation_mode(read_auth):
    invoke, _, _ = read_auth
    code, report = invoke(payload={'blocked': False, 'country': 'US', 'region': 'CA'})
    assert code == 0 and report['result'] == 'PASS'
    assert report['trading_region'] == {
        'status': 'allowed', 'blocked': False, 'country': 'US', 'region': 'CA',
        'policy': 'strict', 'startup_allowed': True,
    }
