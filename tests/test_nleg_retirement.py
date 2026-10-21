"""Active public-entrypoint regressions for permanent non-LP retirement."""
from __future__ import annotations

from contextlib import closing
import re
import sqlite3
from pathlib import Path

import pytest

from open_trader.prediction_arbitrage_store import PredictionArbitrageStore


def trace_sqlite(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Observe the external SQLite boundary, including connections in workers."""
    statements: list[str] = []
    connect = sqlite3.connect

    def traced(*args, **kwargs):
        connection = connect(*args, **kwargs)
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(sqlite3, "connect", traced)
    return statements


def test_store_initialization_preserves_retired_llm_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = PredictionArbitrageStore(tmp_path)
    rows = [
        ("expired-call", "call", "success", '{"provider":"codex","input_tokens":17}', "2001-01-01T00:00:00Z"),
        ("legacy-hit", "cache_hit", "success", '{"provider":"deepseek"}', "2001-01-02T00:00:00Z"),
    ]
    with closing(sqlite3.connect(store.path)) as connection:
        connection.executemany("INSERT INTO llm_usage VALUES (?, ?, ?, ?, ?)", rows)
        connection.commit()
    statements = trace_sqlite(monkeypatch)

    reopened = PredictionArbitrageStore(tmp_path)

    with closing(sqlite3.connect(reopened.path)) as connection:
        actual = connection.execute("SELECT * FROM llm_usage ORDER BY usage_id").fetchall()
    writes = [sql for sql in statements if re.search(r"\b(?:DELETE|INSERT|UPDATE)\b.*\bllm_usage\b", sql, re.I | re.S)]
    assert actual == rows
    assert writes == []


@pytest.mark.parametrize('policy', ['identical', 'changed'])
def test_paused_production_start_never_enters_legacy_policy_transaction(tmp_path: Path, policy: str) -> None:
    import json
    from tests.nleg_retirement_harness import RetirementChild

    with RetirementChild(tmp_path/'child', scenario='policy-'+policy) as child:
        child.start(expected_running=None)
        assert child.wait_exit() == 0, child.output.read_text()
        result = json.loads(child.marker('runtime-start.json').read_text())
        assert result['state'] == 'RUNNING' and result['production_owner'] is True
        assert result['effective_running'] is False and result['fatal'] is None
        assert result['posts'] == [] and result['cancels'] == []
        before = json.loads(child.marker('legacy-before.json').read_text())
        assert result['legacy'] == before
        transactions: dict[int, list[str]] = {}
        legacy_transactions = []
        for line in child.marker('sqlite.jsonl').read_text().splitlines():
            row = json.loads(line)
            sql, connection = row['sql'], row['connection']
            if sql.upper().startswith('BEGIN'):
                transactions[connection] = []
            if connection in transactions:
                transactions[connection].append(sql)
                if re.search(r'\b(?:safety_policy|validation_mode|cross_auto_state)\b', sql, re.I):
                    legacy_transactions.append(list(transactions[connection]))
            if sql.upper().startswith(('COMMIT', 'ROLLBACK')):
                transactions.pop(connection, None)
        assert legacy_transactions == []
        stopped = json.loads(child.marker('runtime-stop.json').read_text())
        assert stopped == dict(state='STOPPED', production_owner=False)
        child.assert_owner_available()
        assert not list(child.events.glob('unexpected-*'))
        with closing(sqlite3.connect(child.data_dir/'prediction_arbitrage/prediction_arbitrage.sqlite3')) as db:
            saved = json.loads(db.execute('SELECT payload FROM lp_auto_pool WHERE singleton=1').fetchone()[0])
        assert saved == json.loads(child.marker('pool-before.json').read_text())


@pytest.mark.parametrize('scenario', ['production-unset', 'production-zero', 'shadow-unset', 'shadow-zero'])
def test_service_entrypoint_keeps_nleg_retired(tmp_path: Path, scenario: str) -> None:
    import json
    import time
    from threading import Event
    from urllib.error import HTTPError
    from urllib.request import Request, ProxyHandler, build_opener
    from open_trader.lp_auto_cli import _Client
    from tests.nleg_retirement_harness import RetirementChild

    with RetirementChild(tmp_path/'child', scenario=scenario) as child:
        child.start(expected_running=None)
        opener = build_opener(ProxyHandler({}))
        def request(path, payload=None):
            data = None if payload is None else json.dumps(payload).encode()
            req = Request(child.base+path, data=data, headers={'Content-Type':'application/json'})
            try:
                response = opener.open(req, timeout=3)
            except HTTPError as exc:
                response = exc
            with response:
                return response.code, json.load(response)
        deadline = time.monotonic()+15
        while True:
            assert child.process.poll() is None, child.output.read_text()
            try:
                status, health = request('/healthz')
                break
            except OSError:
                assert time.monotonic() < deadline, child.output.read_text()
                Event().wait(.01)
        assert status == 200 and health['runtime_state'] == 'RUNNING'
        assert health['n_leg'] == {'status':'paused', 'code':'N_LEG_PAUSED'}
        assert health['mode'] == scenario.split('-')[0]
        assert health['production_owner'] is scenario.startswith('production')
        assert health['mutations'] == ('enabled' if scenario.startswith('production') else 'prohibited')
        for path, payload in (
            ('/api/prediction-arbitrage/state', None),
            ('/api/prediction-arbitrage/preview', {}),
            ('/api/prediction-arbitrage/n-leg/orders/confirm', {}),
        ):
            assert request(path, payload) == (409, {'error':'N_LEG_PAUSED', 'error_code':'N_LEG_PAUSED'})
        status, dashboard = request('/api/prediction-arbitrage/lp/dashboard')
        assert status == 200 and isinstance(dashboard, dict)
        if scenario.startswith('production'):
            status, control = request('/api/prediction-arbitrage/lp/auto/state')
            assert status == 200 and control['effective_running'] is False
            paused = _Client(child.base, 5).write('pause', {'confirm':True})
            assert paused['effective_running'] is False
        else:
            assert request('/api/prediction-arbitrage/lp/auto/pause', {'confirm':True}) == (
                403, {'code':'shadow_read_only', 'message':'Shadow Prediction Service is read-only'})
        child.process.terminate()
        assert child.wait_exit() == 0, child.output.read_text()
        child.assert_owner_available()
        assert not list(child.events.glob('unexpected-*'))
        exited = json.loads(child.marker('service-exit.json').read_text())
        assert exited == dict(code=0, posts=[], cancels=[])


def filesystem_snapshot(root: Path):
    return {str(path.relative_to(root)): (path.stat().st_mode, path.stat().st_mtime_ns,
            path.read_bytes() if path.is_file() else None)
            for path in [root, *sorted(root.rglob('*'))]}


def forbid_business_io(monkeypatch: pytest.MonkeyPatch):
    import fcntl
    import socket
    import subprocess
    from open_trader import polymarket_trading
    calls = []
    def forbidden(name):
        def reject(*args, **kwargs):
            calls.append(name)
            raise AssertionError('retired CLI reached external boundary: '+name)
        return reject
    for owner, name in ((sqlite3, 'connect'), (fcntl, 'flock'), (subprocess, 'Popen'),
                        (socket.socket, 'connect'), (socket.socket, 'connect_ex'),
                        (socket, 'getaddrinfo')):
        monkeypatch.setattr(owner, name, forbidden(name))
    # Includes file/SSM/keychain credential selection before any SDK transport.
    monkeypatch.setattr(polymarket_trading.PolymarketTradingClient, 'from_keychain', forbidden('credentials'))
    return calls


@pytest.mark.parametrize('case', [
    'ingest', 'cleanup-dry', 'cleanup-apply', 'dedup-dry', 'dedup-apply',
    'doctor-report', 'doctor-apply', 'candidates-apply', 'recover-free', 'recover-busy',
])
def test_retired_business_cli_rejects_before_external_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys, case: str
) -> None:
    import fcntl
    import json
    from threading import Thread
    from open_trader import cli
    from tests.test_relation_catalog import discovery

    data = tmp_path/'data'
    store = PredictionArbitrageStore(data)
    with closing(sqlite3.connect(store.path)) as db:
        db.execute("INSERT INTO incidents VALUES ('retained-incident', 'old-execution', ?, NULL, NULL, ?, ?)",
            ('{"reason":"unknown_external_state","positions":[{"token_id":"old-token","size":"13.31"}]}',
             '2001-01-01T00:00:00Z', '2001-01-01T00:00:00Z'))
        db.commit()
    config = tmp_path/'prediction.json'
    config.write_text(json.dumps(dict(signer_address='0x'+'a'*40, wallet_address='0x'+'a'*40)))
    config.chmod(0o600)
    discovery_path = tmp_path/'discovery.json'
    discovery_path.write_text(json.dumps(discovery()))
    report = tmp_path/'report.json'
    report.write_text('existing-report')
    commands = {
        'ingest':['relation-ingest', '--file', str(discovery_path)],
        'cleanup-dry':['catalog-cleanup', '--dry-run'],
        'cleanup-apply':['catalog-cleanup', '--apply'],
        'dedup-dry':['catalog-dedup', '--dry-run'],
        'dedup-apply':['catalog-dedup', '--apply'],
        'doctor-report':['catalog-doctor', '--json'],
        'doctor-apply':['catalog-doctor', '--apply', '--yes', '--drop', 'old-identity'],
        'candidates-apply':['relation-candidates', '--apply', '--report', str(report)],
    }
    recovery = ['recover-external-positions', '--config', str(config), '--incident-id',
                'retained-incident', '--position', 'old-token=13.31', '--confirm', '--note', 'operator-confirmed']
    argv = ['prediction-arb', *commands.get(case, recovery), '--data-dir', str(data)]
    with (data/'prediction_arbitrage/runtime.lock').open('a+') as owner:
        if case == 'recover-busy':
            fcntl.flock(owner.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        before = filesystem_snapshot(tmp_path)
        result, errors = [], []
        with monkeypatch.context() as patch:
            calls = forbid_business_io(patch)
            def invoke():
                try:
                    result.append(cli.main(argv))
                except BaseException as exc:
                    errors.append(exc)
            worker = Thread(target=invoke, daemon=True)
            worker.start()
            worker.join(3)  # Independent real watchdog; never wait for the busy lock.
            assert not worker.is_alive(), 'retired CLI waited for external work'
        assert errors == [], errors
        assert result == [2]
        assert calls == []
        assert json.loads(capsys.readouterr().out)['reason'] == 'N_LEG_PAUSED'
        assert filesystem_snapshot(tmp_path) == before


def test_shadow_seed_cli_is_retired_without_destination_or_report_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    import json
    from open_trader.prediction_shadow import main

    source = PredictionArbitrageStore(tmp_path/'source')
    source.save_relation_state({'relations':[]}, full_scanned_at='2001-01-01T00:00:00Z')
    report = tmp_path/'seed-report.json'
    report.write_text('original-report-sentinel')
    target = tmp_path/'shadow'
    before = filesystem_snapshot(tmp_path)
    with monkeypatch.context() as patch:
        calls = forbid_business_io(patch)
        code = main(['--source-data-dir', str(source.data_dir), '--shadow-data-dir', str(target),
                     '--report', str(report)])
    assert code == 2
    assert json.loads(capsys.readouterr().out)['reason'] == 'N_LEG_PAUSED'
    assert calls == []
    assert not target.exists()
    assert filesystem_snapshot(tmp_path) == before
