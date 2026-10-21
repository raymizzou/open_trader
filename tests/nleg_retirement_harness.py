"""Offline real owners; only credential, SDK, process and SQLite I/O is doubled."""
from __future__ import annotations

from contextlib import closing
from datetime import UTC, datetime
import hashlib
import io
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import threading

import pytest

from tests.lp_d2_service_harness import ServiceChild, seed_no_reconcile_permit


class RetirementChild(ServiceChild):
    """Reuse pristine release/admission fixture preparation, with a dedicated child."""
    def launch(self, scenario, *, expected_running=None):
        self.output = self.root / 'retirement-child.log'
        self.log_handle = self.output.open('w')
        env = dict(os.environ, PYTHONPATH=str(self.release_root/'src')+os.pathsep+str(self.release_root), PYTHONDONTWRITEBYTECODE='1')
        env.pop('OPEN_TRADER_NLEG_PAUSED', None)
        env.pop('OPEN_TRADER_DISABLE_NLEG_BACKGROUND', None)
        env['OPEN_TRADER_CREDENTIAL_BACKEND'] = 'file'
        if scenario == 'shadow-keychain-default':
            env.pop('OPEN_TRADER_CREDENTIAL_BACKEND')
        elif scenario == 'shadow-keychain-explicit':
            env['OPEN_TRADER_CREDENTIAL_BACKEND'] = 'keychain'
        if scenario.endswith('-zero'):
            env['OPEN_TRADER_NLEG_PAUSED'] = '0'
        self.process = subprocess.Popen([
            sys.executable, str(Path(__file__).resolve()), str(self.runtime_root),
            str(self.port), scenario, str(self.config_path),
        ], cwd=self.release_root, env=env, stdout=self.log_handle, stderr=subprocess.STDOUT)


def legacy_snapshot(path):
    with closing(sqlite3.connect(path)) as db:
        return {table: db.execute('SELECT * FROM '+table+' ORDER BY 1').fetchall()
                for table in ('safety_policy', 'validation_mode', 'cross_auto_state', 'control_events')}


def seed_legacy(store, variant):
    # Independent literal snapshot of the selected wallet's current policy.
    wallet = '0x'+'a'*40
    policy = dict(policy_version='prediction-controls-v1', identity=dict(
        signer_address=wallet, wallet_address=wallet, predict_wallet_address='', predict_environment=''),
        limits=dict(book_freshness_seconds='10', cross_auto_daily_principal_cap='100',
            max_cross_unsettled_principal='100', max_emergency_loss='2.00', max_normal_cost='20.00',
            max_wallet_balance='65.00', min_estimated_profit='1.00', min_threshold_annualized_yield='0.15'))
    if variant == 'changed':
        policy['limits']['max_normal_cost'] = '19.00'
    encoded = json.dumps(policy, sort_keys=True, separators=(',', ':'))
    digest = hashlib.sha256(encoded.encode()).hexdigest()
    stamp = '2001-01-01T00:00:00Z'
    pool = dict(account_id='30ffa9b5e1968e54bddadba7ff14c633c7c955a6f41808d9f5633a42c07537dd',
        run_id='retained-pool', config_version=7, desired_running=False, ever_enabled=True,
        enabled_at=stamp, updated_at=stamp, budget_usd='123.45', target_buy_count=3, buy_price_level=2,
        allocations=[], intents={}, events={}, rounds={}, last_round={})
    with closing(sqlite3.connect(store.path)) as db:
        db.execute('INSERT INTO safety_policy VALUES (1, ?, ?, ?, ?)', (digest, encoded, 'old-sha', stamp))
        db.execute("INSERT INTO validation_mode VALUES (1, 'auto', ?)", (stamp,))
        db.execute("INSERT INTO cross_auto_state VALUES (1, 'auto_submit', 1, 'operator', ?)", (stamp,))
        db.execute("INSERT INTO control_events VALUES ('original-audit', 'safety_policy', 'production', 'baseline_enrolled', '{}', ?, ?)", (stamp, stamp))
        db.execute('INSERT INTO lp_auto_pool VALUES (1, ?)', (json.dumps(pool),))
        db.commit()
    return pool


def child(root, port, scenario, config):
    from open_trader.polymarket_trading import PolymarketTradingClient, TradingConfig, LP_COMPETITIVENESS_ENDPOINT, LP_PRICE_HISTORY_ENDPOINT
    from open_trader.predict_trading import PredictTradingClient
    from open_trader.prediction_arbitrage_store import PredictionArbitrageStore
    from open_trader.prediction_runtime import PredictionRuntime
    from tests.test_lp_order_registration_contract import _SDKAccountClient, _SDKPublicClient, WALLET

    root, config = Path(root), Path(config)
    events = root/'events'
    store = PredictionArbitrageStore(root/'data')
    runtime_only = scenario.startswith('policy-')
    if runtime_only:
        pool = seed_legacy(store, scenario.removeprefix('policy-'))
        (events/'legacy-before.json').write_text(json.dumps(legacy_snapshot(store.path)))
        (events/'pool-before.json').write_text(json.dumps(pool))
    seed_no_reconcile_permit(root, Path.cwd(), config, port)
    now = datetime.now(UTC)
    sdk = _SDKAccountClient(now)
    def public_http(request, **kwargs):
        if request.full_url.startswith(LP_COMPETITIVENESS_ENDPOINT+'?'):
            return io.BytesIO(b'{"data":[],"next_cursor":"LTE="}')
        if request.full_url == LP_PRICE_HISTORY_ENDPOINT:
            return io.BytesIO(b'{"history":{}}')
        raise AssertionError('uncontrolled LP HTTP: '+request.full_url)
    adapter = PolymarketTradingClient(TradingConfig(WALLET, WALLET), sdk,
        public_client_factory=lambda: _SDKPublicClient(now), urlopen_fn=public_http)
    # Count actual SDK work and prohibit all nonlocal network access.
    native_connect = socket.socket.connect
    native_connect_ex = socket.socket.connect_ex
    native_getaddrinfo = socket.getaddrinfo
    native_popen = subprocess.Popen
    native_sqlite = sqlite3.connect
    trace_lock = threading.Lock()
    sequence = [0]
    def mark(name):
        (events/name).touch()
    def deny(name):
        mark('unexpected-'+name)
        raise AssertionError('retired external boundary: '+name)
    def network(sock, address):
        if not isinstance(address, tuple) or address[0] not in ('127.0.0.1', '::1'):
            return deny('network')
        return native_connect(sock, address)
    def network_ex(sock, address):
        if not isinstance(address, tuple) or address[0] not in ('127.0.0.1', '::1'):
            return deny('network')
        return native_connect_ex(sock, address)
    def getaddrinfo(host, *args, **kwargs):
        if host not in ('127.0.0.1', '::1', 'localhost'):
            return deny('network-resolution')
        return native_getaddrinfo(host, *args, **kwargs)
    def popen(args, *a, **kw):
        if any(any(name in str(value) for name in ('solver', 'codex', 'security')) for value in args):
            return deny('non-lp-process')
        return native_popen(args, *a, **kw)
    def connect(*args, **kwargs):
        db = native_sqlite(*args, **kwargs)
        with trace_lock:
            sequence[0] += 1
            identity = sequence[0]
        def trace(sql):
            with trace_lock, (events/'sqlite.jsonl').open('a') as out:
                out.write(json.dumps(dict(connection=identity, thread=threading.current_thread().name, sql=sql))+'\n')
        db.set_trace_callback(trace)
        return db
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(socket.socket, 'connect', network)
        patch.setattr(socket.socket, 'connect_ex', network_ex)
        patch.setattr(socket, 'getaddrinfo', getaddrinfo)
        patch.setattr(subprocess, 'Popen', popen)
        patch.setattr(sqlite3, 'connect', connect)
        patch.setattr(PolymarketTradingClient, 'from_keychain', classmethod(lambda cls, config, **kwargs: adapter))
        patch.setattr(PredictTradingClient, 'from_keychain', classmethod(lambda cls, *a, **kw: deny('predict-account')))
        if runtime_only:
            runtime = PredictionRuntime(data_dir=root/'data', prediction_config_path=config,
                dashboard_url=f'http://127.0.0.1:{port}', n_leg_paused=True)
            try:
                runtime.start()
                status = runtime.lp_d2.control.status()
                result = dict(state=runtime.state, production_owner=runtime.production_owner,
                    effective_running=status.current.effective_running,
                    fatal=status.fatal, posts=sdk.posts, cancels=sdk.cancels,
                    legacy=legacy_snapshot(store.path))
                (events/'runtime-start.json').write_text(json.dumps(result, default=str))
            finally:
                runtime.stop()
                (events/'runtime-stop.json').write_text(json.dumps(dict(state=runtime.state,
                    production_owner=runtime.production_owner)))
        else:
            from open_trader.prediction_service import serve_prediction_service
            result = serve_prediction_service(data_dir=root/'data', prediction_config_path=config,
                host='127.0.0.1', port=port, mode=scenario.split('-')[0],
                release_manifest_path=Path.cwd()/'ops/prediction-service-release.json')
            (events/'service-exit.json').write_text(json.dumps(dict(code=result, posts=sdk.posts, cancels=sdk.cancels)))
            raise SystemExit(result)
    adapter.close()


if __name__ == '__main__':
    child(sys.argv[1], int(sys.argv[2]), sys.argv[3], sys.argv[4])
