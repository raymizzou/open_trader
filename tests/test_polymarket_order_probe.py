"""Public CLI contracts through the locked SDK's external HTTP boundary."""
from __future__ import annotations

import importlib
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
from eth_account import Account
from polymarket import PRODUCTION

PRIVATE = '0x' + '01' * 32  # Synthetic, never funded.
SIGNER = Account.from_key(PRIVATE).address
CONDITION = '0x' + 'ab' * 32
TOKEN = '123456789'
SECRET = 'c3ludGhldGljLXNlY3JldA=='


class Exchange:
    def __init__(self):
        self.requests = []
        self.blocked = True
        self.balance = '10000000'
        self.allowance = '10000000'
        self.tick = '0.01'
        self.minimum = '5'
        self.ask = '0.12'
        self.server_time = 1700000000
        self.book_time = 1700000000000
        self.accepting = True
        self.mode = 'normal'
        self.cancel_mode = 'normal'
        self.cancelled = False
        self.posted = False
        self.filled = '0'
        self.account_bad = False
        self.expire_preparation = False
        self.trade_fill = False
        self.position_changed = False
        self.clock = None
        self.book_token = None
        self.market_token = None
        self.terminal_get_status = None
        self.time_sequence = None
        self.submission_clock = None

    @property
    def mutations(self):
        return [r for r in self.requests if r.method != 'GET']

    def order(self, oid='probe-1', status=None):
        return dict(id=oid, market=CONDITION, asset_id=TOKEN, owner='synthetic-api',
                    maker_address=SIGNER, side='BUY', price='0.10', original_size='5',
                    size_matched=self.filled if oid == 'probe-1' else '0', outcome='Yes',
                    order_type='GTD', status=status or ('CANCELED' if self.cancelled else 'LIVE'),
                    created_at=1700000000, expiration=1700000240)

    def handle(self, request):
        self.requests.append(request)
        path = request.url.path
        if self.clock is not None:
            if path == '/positions': self.clock['now'] = max(self.clock['now'], 6)
            if path.startswith('/markets/'): self.clock['now'] = max(self.clock['now'], 9)
            if path == '/neg-risk': self.clock['now'] = max(self.clock['now'], 11)
        if path == '/api/geoblock':
            data = dict(blocked=self.blocked, country='JP', region='13', ip='192.0.2.1')
        elif path == '/auth/derive-api-key':
            data = dict(apiKey='synthetic-api', secret=SECRET, passphrase='synthetic-pass')
        elif path == '/balance-allowance':
            data = dict(balance=self.balance, allowances={PRODUCTION.standard_exchange: self.allowance})
        elif path == '/data/orders':
            old = self.order('other-1')
            if self.account_bad: old['status'] = ''
            data = dict(data=[old], next_cursor='LTE=')
        elif path == '/data/trades':
            rows = []
            if self.trade_fill and self.cancelled:
                rows = [dict(id='fill-1', market=CONDITION, asset_id=TOKEN, owner='synthetic-api',
                    maker_address=SIGNER, taker_order_id='foreign-taker', side='SELL', trader_side='MAKER',
                    price='0.10', size='2', outcome='Yes', status='CONFIRMED', fee_rate_bps='0',
                    bucket_index=0, transaction_hash='0x' + 'cd' * 32,
                    match_time=1700000000, last_update=1700000000,
                    maker_orders=[dict(order_id='probe-1', asset_id=TOKEN, maker_address=SIGNER,
                        owner='synthetic-api', side='BUY', price='0.10', matched_amount='2', outcome='Yes')])]
            data = dict(data=rows, next_cursor='LTE=')
        elif path == '/positions':
            data = [dict(conditionId=CONDITION, proxyWallet=SIGNER, asset=TOKEN,
                         size='2' if self.position_changed and self.cancelled else '0')]
        elif path == '/time':
            data = self.server_time if self.time_sequence is None else self.time_sequence.pop(0)
            if self.submission_clock is not None and not self.time_sequence:
                self.submission_clock['now'] = 2
        elif path == '/book':
            data = dict(market=CONDITION, asset_id=self.book_token or TOKEN, timestamp=str(self.book_time),
                        bids=[dict(price='0.09', size='10')], asks=[dict(price=self.ask, size='10')],
                        min_order_size=self.minimum, tick_size=self.tick, neg_risk=False, hash='book')
        elif path == f'/markets/{CONDITION}':
            data = dict(condition_id=CONDITION, accepting_orders=self.accepting,
                        active=True, closed=False, tokens=[dict(token_id=self.market_token or TOKEN)])
        elif path == '/tick-size':
            data = dict(minimum_tick_size=self.tick)
        elif path == '/neg-risk':
            if self.expire_preparation:
                self.server_time = 1700000061
            data = dict(neg_risk=False)
        elif path == '/order' and request.method == 'POST':
            self.posted = True
            if self.mode == 'crash':
                raise SystemExit('synthetic process crash after attempted was persisted')
            if self.mode == 'timeout':
                raise httpx.ReadTimeout('synthetic timeout', request=request)
            if self.mode == 'rejected':
                return httpx.Response(403, json={'error': 'geoblocked ' + SECRET}, request=request)
            data = dict(errorMsg='', makingAmount='0', takingAmount='0',
                        orderID='' if self.mode == 'missing_id' else 'probe-1',
                        status='live', success=True)
        elif path == '/orders' and request.method == 'DELETE':
            if self.cancel_mode == 'timeout':
                raise httpx.ReadTimeout('synthetic cancel timeout', request=request)
            self.cancelled = self.cancel_mode != 'still_live'
            data = dict(canceled=[] if self.cancel_mode == 'missing_ack' else ['probe-1'], not_canceled={})
        elif path == '/data/order/probe-1':
            if self.cancelled and self.terminal_get_status is not None:
                return httpx.Response(self.terminal_get_status, json={'error': SECRET}, request=request)
            if self.cancelled and self.cancel_mode == 'read_error':
                return httpx.Response(500, json={'error': SECRET}, request=request)
            data = self.order()
            if self.posted:
                sent = json.loads(next(r.content for r in self.requests if r.method == 'POST' and r.url.path == '/order'))
                data['price'] = str(int(sent['order']['makerAmount']) / int(sent['order']['takerAmount']))
                data['original_size'] = str(int(sent['order']['takerAmount']) / 1000000)
        else:
            raise AssertionError(f'unexpected external request: {request.method} {request.url}')
        return httpx.Response(200, json=data, request=request)


@pytest.fixture
def exchange(tmp_path, monkeypatch):
    venue = Exchange()
    monkeypatch.setattr(httpx.HTTPTransport, 'handle_request', lambda _, request: venue.handle(request))
    monkeypatch.setattr(socket.socket, 'connect', lambda *a: pytest.fail('real network forbidden'))
    monkeypatch.setattr(time, 'time', lambda: 1700000000.0)
    directory = tmp_path / 'credentials'
    directory.mkdir(mode=0o700)
    credentials = directory / 'synthetic.json'
    credentials.write_text(json.dumps({'com.open-trader.polymarket': {
        'signing-private-key': PRIVATE, 'builder-key': 'synthetic-builder',
        'builder-secret': SECRET, 'builder-passphrase': 'synthetic-pass'}}))
    credentials.chmod(0o600)
    config = tmp_path / 'account.json'
    config.write_text(json.dumps(dict(signer_address=SIGNER, wallet_address=SIGNER)))
    monkeypatch.delenv('OPEN_TRADER_CREDENTIAL_BACKEND', raising=False)
    monkeypatch.delenv('OPEN_TRADER_CREDENTIAL_FILE', raising=False)
    venue.args = ['--config', str(config), '--credential-backend', 'file',
                  '--credentials-file', str(credentials)]
    venue.record = tmp_path / 'receipt.json'
    return venue


def invoke(exchange, capsys, command='check', extra=None, price='0.10', quantity='5'):
    module = importlib.import_module('open_trader.polymarket_order_probe')
    args = [command, *exchange.args]
    if command in ('check', 'run'):
        args += ['--token', TOKEN, '--price', price, '--quantity', quantity]
    if command != 'check':
        args += ['--record', str(exchange.record)]
    if extra:
        args += extra
    code = module.main(args)
    captured = capsys.readouterr()
    return code, json.loads(captured.out), captured.out + captured.err


LIVE = ['--confirm-live', '--confirm-single-writer']


def test_check_is_read_only_and_separates_region_from_auth(exchange, capsys):
    code, report, _ = invoke(exchange, capsys)
    assert code == 0
    assert report['account']['status'] == 'ready'
    assert report['region'] == {'status': 'blocked', 'blocked': True, 'country': 'JP', 'region': '13'}
    assert report['order_validation'] == 'UNVERIFIED'
    assert exchange.mutations == []
    assert any(r.url.path == '/auth/derive-api-key' for r in exchange.requests)


@pytest.mark.parametrize('price,quantity,tick,ask,confirm,reason', [
    ('0.20', '5', '0.01', '0.30', LIVE, None),
    ('0.2001', '5', '0.0001', '0.30', LIVE, 'budget_exceeded'),
    ('0.105', '5', '0.01', '0.12', LIVE, 'price_off_tick'),
    ('0.10', '4', '0.01', '0.12', LIVE, 'quantity_below_minimum'),
    ('NaN', '5', '0.01', '0.12', LIVE, 'invalid_input'),
    ('Infinity', '5', '0.01', '0.12', LIVE, 'invalid_input'),
    ('0', '5', '0.01', '0.12', LIVE, 'invalid_input'),
    ('-0.1', '5', '0.01', '0.12', LIVE, 'invalid_input'),
    ('0.10', '0', '0.01', '0.12', LIVE, 'invalid_input'),
    ('0.10', '-5', '0.01', '0.12', LIVE, 'invalid_input'),
    ('0.10', 'NaN', '0.01', '0.12', LIVE, 'invalid_input'),
    ('0.10', 'Infinity', '0.01', '0.12', LIVE, 'invalid_input'),
    ('0.10', '5', '0.01', '0.12', ['--confirm-single-writer'], 'live_confirmation_required'),
    ('0.10', '5', '0.01', '0.12', ['--confirm-live'], 'writer_confirmation_required'),
])
def test_run_rejects_invalid_inputs_without_mutation(exchange, capsys, price, quantity, tick, ask, confirm, reason):
    exchange.tick, exchange.ask = tick, ask
    code, report, _ = invoke(exchange, capsys, 'run', confirm, price, quantity)
    if reason is None:
        assert code == 0
        assert report['input_validation'] == 'valid'
        assert report['notional'] == '1.00'
    else:
        assert code == 2
        assert report['result'] == 'BLOCKED'
        assert report['reason'] == reason
        assert exchange.mutations == []


@pytest.mark.parametrize('expired', [False, True])
def test_run_posts_once_and_verifies_exact_order_cancelled(exchange, capsys, expired):
    exchange.expire_preparation = expired
    code, report, _ = invoke(exchange, capsys, 'run', LIVE)
    if expired:
        assert code == 2
        assert report['reason'] == 'expiration_too_close'
        assert exchange.mutations == []
        return
    assert code == 0
    assert report['result'] == 'PASS'
    assert report['live_observed'] is True
    assert report['cancel_acknowledged'] is True
    assert report['terminal_status'] == 'CANCELED'
    assert report['filled_quantity'] == '0'
    assert report['funds_reconciled'] is True
    posts = [r for r in exchange.mutations if r.method == 'POST']
    assert len(posts) == 1
    body = json.loads(posts[0].content)
    assert body['orderType'] == 'GTD'
    assert body['postOnly'] is True
    assert body['order']['side'] == 'BUY'
    assert body['order']['tokenId'] == TOKEN
    assert body['order']['makerAmount'] == '500000'
    assert body['order']['takerAmount'] == '5000000'
    assert body['order']['expiration'] == '1700000240'
    deletes = [r for r in exchange.mutations if r.method == 'DELETE']
    assert [json.loads(r.content) for r in deletes] == [['probe-1']]
    assert all(r.url.path != '/cancel-all' for r in exchange.requests)
    receipt = json.loads(exchange.record.read_text())
    assert receipt['attempted'] is True
    assert receipt['order_id'] == 'probe-1'
    assert receipt['result'] == 'PASS'


@pytest.mark.parametrize('fault,result,reason', [
    ('balance', 'BLOCKED', 'balance_insufficient'),
    ('allowance', 'BLOCKED', 'allowance_insufficient'),
    ('account', 'UNKNOWN', 'account_incomplete'),
    ('closed', 'BLOCKED', 'market_not_accepting'),
    ('book', 'UNKNOWN', 'external_or_local_failure'),
    ('tick', 'UNKNOWN', 'external_or_local_failure'),
    ('minimum', 'UNKNOWN', 'external_or_local_failure'),
    ('cross', 'BLOCKED', 'price_crosses_ask'),
    ('stale', 'UNKNOWN', 'market_stale'),
])
def test_run_blocks_unknown_or_unusable_market_and_account(exchange, capsys, fault, result, reason):
    price = '0.10'
    if fault == 'balance': exchange.balance = '499999'
    if fault == 'allowance': exchange.allowance = '499999'
    if fault == 'account': exchange.account_bad = True
    if fault == 'closed': exchange.accepting = False
    if fault == 'book': exchange.ask = None
    if fault == 'tick': exchange.tick = None
    if fault == 'minimum': exchange.minimum = None
    if fault == 'cross': exchange.ask, price = '0.11', '0.12'
    if fault == 'stale': exchange.book_time = 1699999980000
    code, report, _ = invoke(exchange, capsys, 'run', LIVE, price=price)
    assert code == 2
    assert report['result'] == result
    assert report['reason'] == reason
    assert exchange.mutations == []


@pytest.mark.parametrize('mode,result', [
    ('rejected', 'REJECTED'), ('timeout', 'UNKNOWN'), ('missing_id', 'UNKNOWN'), ('crash', 'UNKNOWN'),
])
def test_submit_rejection_and_ambiguity_never_resubmit(exchange, capsys, mode, result):
    exchange.mode = mode
    if mode == 'crash':
        with pytest.raises(SystemExit, match='synthetic process crash'):
            invoke(exchange, capsys, 'run', LIVE)
        capsys.readouterr()
        assert json.loads(exchange.record.read_text())['attempted'] is True
    else:
        code, report, _ = invoke(exchange, capsys, 'run', LIVE)
        assert code == 2
        assert report['result'] == result
    assert len([r for r in exchange.mutations if r.method == 'POST']) == 1
    code, report, _ = invoke(exchange, capsys, 'run', LIVE)
    assert code == 2
    assert report['result'] == result
    assert len([r for r in exchange.mutations if r.method == 'POST']) == 1
    before = len(exchange.mutations)
    code, report, _ = invoke(exchange, capsys, 'status')
    assert code == 2
    assert report['result'] == result
    assert len(exchange.mutations) == before
    code, report, _ = invoke(exchange, capsys, 'cancel', LIVE)
    assert code == 2
    assert len(exchange.mutations) == before
    assert all(r.method != 'DELETE' for r in exchange.requests)


@pytest.mark.parametrize('fault', ['missing_ack', 'read_error', 'filled', 'still_live', 'timeout', 'trade_fill_with_zero_order_match', 'target_position_changed_without_fill_receipt'])
def test_cancel_requires_terminal_and_fill_reconciliation(exchange, capsys, fault):
    if fault == 'target_position_changed_without_fill_receipt':
        exchange.position_changed = True
    elif fault == 'trade_fill_with_zero_order_match':
        exchange.trade_fill = True
    elif fault == 'filled':
        exchange.filled = '2'
    else:
        exchange.cancel_mode = fault
    code, report, _ = invoke(exchange, capsys, 'run', LIVE)
    assert code == 2
    assert report['result'] == ('PARTIAL' if fault == 'trade_fill_with_zero_order_match' else 'UNKNOWN')
    if fault == 'trade_fill_with_zero_order_match':
        assert report['filled_quantity'] == '2'
        assert report['filled_notional'] == '0.20'
        assert report['fill_discrepancy'] is True
        assert report['order_matched_quantity'] == '0'
        assert report['trade_filled_quantity'] == '2'
    if fault == 'target_position_changed_without_fill_receipt':
        assert report['reason'] == 'target_position_changed'
        assert report['position_before'] == '0'
        assert report['position_after'] == '2'
        assert report['filled_quantity'] == '0'
    if fault == 'filled':
        assert report['filled_quantity'] == '2'
        assert report['filled_notional'] == '0.20'
    deletes = [r for r in exchange.mutations if r.method == 'DELETE']
    assert [json.loads(r.content) for r in deletes] == [['probe-1']]
    assert len([r for r in exchange.mutations if r.method == 'POST']) == 1
    receipt = json.loads(exchange.record.read_text())
    assert receipt['order_id'] == 'probe-1'
    assert receipt['attempted'] is True
    assert receipt['result'] == ('PARTIAL' if fault == 'trade_fill_with_zero_order_match' else 'UNKNOWN')
    # Recovery is GET-only and must not turn a missing ACK or fill into pure hang/cancel success.
    count = len(exchange.mutations)
    code, report, _ = invoke(exchange, capsys, 'status')
    assert code == 2
    assert report['result'] == ('PARTIAL' if fault == 'trade_fill_with_zero_order_match' else 'UNKNOWN')
    assert len(exchange.mutations) == count


@pytest.mark.parametrize('fault', ['reflection', 'symlink', 'permissions', 'write_failure', 'identity_status', 'identity_cancel', 'entrypoints'])
def test_cli_receipts_redact_secrets_and_reject_unsafe_files(exchange, capsys, monkeypatch, fault):
    if fault == 'entrypoints':
        root = Path(__file__).resolve().parents[1]
        env = dict(os.environ, PYTHONPATH=str(root / 'src'), PYTHONDONTWRITEBYTECODE='1', OPEN_TRADER_PYTHON=sys.executable)
        args = ['run', *exchange.args, '--token', TOKEN, '--price', 'NaN', '--quantity', '5',
                '--record', str(exchange.record), *LIVE]
        module = subprocess.run([sys.executable, '-B', '-m', 'open_trader.polymarket_order_probe', *args],
                                capture_output=True, text=True, env=env, timeout=15)
        shell = subprocess.run(['bash', str(root / 'scripts/polymarket-order-probe.sh'), *args],
                               capture_output=True, text=True, env=env, timeout=15)
        assert module.returncode == shell.returncode == 2
        assert module.stdout == shell.stdout
        assert module.stderr == shell.stderr == ''
        assert json.loads(module.stdout)['reason'] == 'invalid_input'
        return
    if fault.startswith('identity_'):
        code, _, _ = invoke(exchange, capsys, 'run', LIVE)
        assert code == 0
        data = json.loads(exchange.record.read_text())
        data['wallet'] = '0x' + '22' * 20
        exchange.record.write_text(json.dumps(data))
        count = len(exchange.mutations)
        code, report, output = invoke(exchange, capsys, fault.split('_')[1], LIVE if fault.endswith('cancel') else [])
        assert code == 2
        assert report['reason'] == 'record_identity_mismatch'
        assert len(exchange.mutations) == count
        return
    if fault == 'reflection':
        exchange.mode = 'rejected'
    elif fault == 'symlink':
        target = exchange.record.parent / 'other.json'
        target.write_text('{}')
        exchange.record.symlink_to(target)
    elif fault == 'permissions':
        exchange.record.write_text('{}')
        exchange.record.chmod(0o644)
    elif fault == 'write_failure':
        def unavailable(*a, **kw):
            raise OSError('synthetic write failure ' + SECRET)
        monkeypatch.setattr(os, 'replace', unavailable)
    code, report, output = invoke(exchange, capsys, 'run', LIVE)
    assert code == 2
    for secret in [PRIVATE, SECRET, 'synthetic-pass', 'POLY_SIGNATURE', 'Authorization']:
        assert secret not in output
        if exchange.record.exists() and fault not in ('symlink', 'permissions'):
            assert secret not in exchange.record.read_text()
    if fault == 'reflection':
        assert exchange.record.stat().st_mode & 0o777 == 0o600
        assert report['result'] == 'REJECTED'
    else:
        assert exchange.mutations == []


def test_known_order_is_cancelled_when_id_receipt_write_fails(exchange, capsys, monkeypatch):
    real_replace = os.replace
    failures = []
    def replace_after_submit(*args, **kwargs):
        if exchange.posted:
            failures.append('synthetic ID receipt replace OSError')
            raise OSError('synthetic ID receipt replace OSError')
        return real_replace(*args, **kwargs)
    monkeypatch.setattr(os, 'replace', replace_after_submit)
    code, report, output = invoke(exchange, capsys, 'run', LIVE)
    assert failures
    assert code == 2
    assert report['result'] == 'UNKNOWN'
    assert report['reason'] == 'record_write_failed'
    assert report['order_id'] == 'probe-1'
    assert report['cancel_acknowledged'] is True
    assert report['terminal_status'] == 'CANCELED'
    assert [json.loads(r.content) for r in exchange.mutations if r.method == 'DELETE'] == [['probe-1']]
    assert len([r for r in exchange.mutations if r.method == 'POST']) == 1
    data = json.loads(exchange.record.read_text())
    assert data['attempted'] is True
    assert data['order_id'] is None
    assert SECRET not in output
    code, report, _ = invoke(exchange, capsys, 'run', LIVE)
    assert code == 2
    assert report['result'] == 'UNKNOWN'
    assert len([r for r in exchange.mutations if r.method == 'POST']) == 1


def test_run_blocks_fact_window_expired_before_submit(exchange, capsys, monkeypatch):
    clock = {'now': 0}
    exchange.clock = clock
    monkeypatch.setattr(time, 'monotonic', lambda: clock['now'])
    code, report, _ = invoke(exchange, capsys, 'run', LIVE)
    assert clock['now'] == 11
    assert code == 2
    assert report['result'] == 'UNKNOWN'
    assert report['reason'] == 'account_or_market_stale'
    assert exchange.mutations == []


def test_run_rejects_book_for_another_token(exchange, capsys):
    exchange.book_token = '456'
    exchange.market_token = '123'
    module = importlib.import_module('open_trader.polymarket_order_probe')
    code = module.main(['run', *exchange.args, '--token', '123', '--price', '0.10', '--quantity', '5',
                        '--record', str(exchange.record), *LIVE])
    report = json.loads(capsys.readouterr().out)
    assert code == 2
    assert report['result'] == 'UNKNOWN'
    assert report['reason'] == 'book_token_mismatch'
    assert exchange.mutations == []


@pytest.mark.parametrize('quantity', ['5.0000001', '5.000001', '5.0000000000000000000000000001'])
def test_run_rejects_inexact_signed_amounts(exchange, capsys, quantity):
    code, report, _ = invoke(exchange, capsys, 'run', LIVE, quantity=quantity)
    assert code == 2
    assert report['result'] == 'BLOCKED'
    assert report['reason'] == 'signed_order_mismatch'
    assert exchange.mutations == []


@pytest.mark.parametrize('terminal_get_status', [401, 403, 404])
def test_post_submit_read_rejection_remains_unknown(exchange, capsys, terminal_get_status):
    exchange.terminal_get_status = terminal_get_status
    code, report, output = invoke(exchange, capsys, 'run', LIVE)
    assert code == 2
    assert report['result'] == 'UNKNOWN'
    assert report['order_validation'] == 'UNKNOWN'
    assert report['order_id'] == 'probe-1'
    assert report['live_observed'] is True
    assert report['cancel_acknowledged'] is True
    assert report.get('submit_http_status') != terminal_get_status
    assert SECRET not in output
    receipt = json.loads(exchange.record.read_text())
    assert receipt['order_id'] == 'probe-1'
    assert receipt['result'] == 'UNKNOWN'
    assert receipt['order_validation'] == 'UNKNOWN'
    assert receipt['live_observed'] is True
    assert receipt['cancel_acknowledged'] is True
    assert receipt.get('submit_http_status') != terminal_get_status
    assert [json.loads(r.content) for r in exchange.mutations if r.method == 'DELETE'] == [['probe-1']]
    assert len([r for r in exchange.mutations if r.method == 'POST']) == 1
    before = len(exchange.mutations)
    code, report, _ = invoke(exchange, capsys, 'status')
    assert code == 2
    assert report['result'] == 'UNKNOWN'
    assert len(exchange.mutations) == before
    code, _, _ = invoke(exchange, capsys, 'run', LIVE)
    assert code == 2
    assert len(exchange.mutations) == before


def test_run_rechecks_book_age_at_submission(exchange, capsys, monkeypatch):
    clock = {'now': 0}
    exchange.time_sequence = [1700000009, 1700000011]
    exchange.submission_clock = clock
    monkeypatch.setattr(time, 'monotonic', lambda: clock['now'])
    code, report, _ = invoke(exchange, capsys, 'run', LIVE)
    assert clock['now'] == 2
    assert exchange.mutations == []
    assert code == 2
    assert report['result'] == 'UNKNOWN'
    assert report['reason'] == 'market_stale'
