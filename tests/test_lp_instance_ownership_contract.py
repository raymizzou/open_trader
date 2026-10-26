"""Stable instance provenance, using real persistence and offline transport."""
from dataclasses import replace
import json
from decimal import Decimal
from types import SimpleNamespace
import sqlite3
from datetime import UTC, datetime, timedelta
import time
import threading

import pytest

from open_trader.polymarket_trading import load_trading_config
from open_trader.polymarket_trading import PolymarketTradingClient, TradingConfig
from open_trader.polymarket_lp_live_execution import LPD2LiveExecution
from open_trader.polymarket_lp_market_calculation import CalculationPolicy
from open_trader.prediction_arbitrage_store import PredictionArbitrageStore
from tests.test_lp_order_registration_contract import _adapter, WALLET


def test_instance_config_and_runtime_identity_are_explicit(tmp_path):
    path = tmp_path / 'trading.json'
    original = dict(signer_address=WALLET, wallet_address=WALLET)
    path.write_text(json.dumps(original))
    legacy = load_trading_config(path)
    assert legacy.lp_instance_id is None
    for invalid in ('', ' mini', 'mini ', '../mini', WALLET, None, True, 7):
        path.write_text(json.dumps(original | dict(lp_instance_id=invalid)))
        with pytest.raises(ValueError, match='lp_instance_id'):
            load_trading_config(path)

    instances = []
    for instance in ('mini', 'tokyo'):
        path.write_text(json.dumps(original | dict(lp_instance_id=instance)))
        config = load_trading_config(path)
        assert config.wallet_address == WALLET
        assert config.lp_instance_id == instance
        adapter, _ = _adapter(tmp_path / instance)
        adapter.config = config
        store = PredictionArbitrageStore(tmp_path / instance)
        live = LPD2LiveExecution(store, adapter, account_id=WALLET, venue='polymarket',
            process_epoch='unrelated-process-epoch', policy=CalculationPolicy(None))
        try:
            assert live.lp_instance_id == instance
            assert live.account_id == WALLET
            assert live.close(timeout=3)
            restarted = LPD2LiveExecution(store, adapter, account_id=WALLET,
                venue='polymarket', process_epoch='new-epoch', policy=CalculationPolicy(None))
            assert restarted.lp_instance_id == instance
            assert restarted.close(timeout=3)
            adapter.config = replace(config, lp_instance_id='other')
            with pytest.raises(ValueError, match='instance.*bound'):
                LPD2LiveExecution(store, adapter, account_id=WALLET, venue='polymarket',
                    process_epoch='new-epoch', policy=CalculationPolicy(None))
        finally:
            live.close(timeout=3)
            adapter.close()
        instances.append(config.lp_instance_id)
    assert instances == ['mini', 'tokyo']


def test_prepared_order_identity_matches_pinned_exchange_vectors():
    from open_trader.lp_order_ownership import canonical_order_id
    # Pinned official Python builder and Hashing.sol; independently ABI-checked.
    standard = '0xE111180000d2663C0091e4f400237545B87B996B'
    neg = '0xe2222d279d744050d28e00520010520000310F59'
    fields = dict(salt='479249096354', maker='0x'+'11'*20,
        signer='0x'+'11'*20, tokenId='1234', makerAmount='100000000',
        takerAmount='50000000', timestamp='1710000000000',
        metadata='0x'+'00'*32, builder='0x'+'00'*32)
    vectors = (
        (80002, standard, 0, 3, '0x2afca94626db91b2d556c351584a4cd93e13da32e377dc48c7983e43afa5ab47'),
        (137, standard, 0, 0, '0x5443d6b36be8d6787f921dff365cff16beda765cbf5420b782c167c2f296936f'),
        (137, neg, 1, 1, '0xd6ca45b8dfca6c92b05b4809d8ab04dbc8d342d2185343058982bbeb0f570fb4'),
        (137, standard, 0, 2, '0x076adcde195812b6f5ad2ee23622afaf637299f87063b2f4db91197ba4a76c0c'),
        (137, neg, 1, 3, '0x3667e816b43669463a424cbe54390ab612fb69f3621ca9ee037271b38996eac9'),
    )
    for chain, exchange, side, signature_type, expected in vectors:
        order = fields | dict(side=side, signatureType=signature_type)
        assert canonical_order_id(order, chain_id=chain, exchange=exchange) == expected
        assert canonical_order_id(order | dict(salt='479249096355'), chain_id=chain, exchange=exchange) != expected


def _signed_adapter(*, signature_type=0, neg_risk=False, bad=None):
    """Synthetic ephemeral key; real SDK typed-data/envelope, no bootstrap."""
    from eth_account import Account
    from eth_account.messages import encode_typed_data
    from polymarket import PRODUCTION
    from polymarket._internal.actions.orders.types import UnsignedOrder
    from polymarket._internal.actions.orders.orders import create_signed_order
    from polymarket._internal.actions.orders.typed_data import build_order_typed_data, build_order_signature
    signer = Account.create()
    wallet = signer.address if signature_type == 0 else WALLET
    posts, signs = [], []

    def create_limit_order(**kwargs):
        side = kwargs['side']
        quantity = int(kwargs['size'] * 1000000)
        amount = int(kwargs['price'] * kwargs['size'] * 1000000)
        order = UnsignedOrder(chain_id=80002 if bad == 'domain' else 137,
            exchange_address=PRODUCTION.neg_risk_exchange if neg_risk else PRODUCTION.standard_exchange,
            expiration=kwargs.get('expiration') or 0, maker=wallet,
            maker_amount=amount if side == 'BUY' else quantity,
            taker_amount=quantity if side == 'BUY' else amount,
            signer=wallet if signature_type == 3 else signer.address,
            signature_type=signature_type, token_id=str(int(kwargs['token_id'], 16)) if kwargs['token_id'].startswith('0x') else kwargs['token_id'], side=side,
            salt=123456+len(signs), timestamp=1710000000000, metadata='0x'+'00'*32,
            builder='0x'+'00'*32, order_type='GTC' if kwargs.get('expiration') is None else 'GTD')
        signature = '0x' + signer.sign_message(encode_typed_data(full_message=build_order_typed_data(order))).signature.hex()
        signed = create_signed_order(order, build_order_signature(order, signature), post_only=True)
        signed = replace(signed, token_id=kwargs['token_id'])
        if bad == 'signer': signed = replace(signed, signer='0x'+'12'*20)
        if bad == 'token': signed = replace(signed, token_id='9999')
        if bad == 'quantity': signed = replace(signed, taker_amount=signed.taker_amount+1)
        signs.append(None)  # Never retain or print a key or signature in test evidence.
        return signed

    sdk = SimpleNamespace(environment=PRODUCTION, create_limit_order=create_limit_order,
        post_order=lambda order: posts.append(None), close=lambda: None)
    adapter = PolymarketTradingClient(TradingConfig(signer.address, wallet, lp_instance_id='tokyo'), sdk)
    return adapter, posts, signs


def test_prepared_order_identity_rejects_wrong_signer_and_changed_payload():
    for signature_type, neg_risk, side in ((0, False, 'BUY'), (1, True, 'SELL'),
            (2, False, 'BUY'), (3, True, 'SELL')):
        adapter, posts, signs = _signed_adapter(signature_type=signature_type, neg_risk=neg_risk)
        try:
            prepared = adapter.lp_prepare_limit_order(token_id='1234', side=side,
                price=Decimal('.4'), quantity=Decimal('5'), post_only=True, expiration=None)
            assert prepared.order_id.startswith('0x') and len(prepared.order_id) == 66
            assert prepared.token_id == '1234' and prepared.side == side
            assert prepared.quantity == Decimal('5') and prepared.price == Decimal('.4')
            assert len(signs) == 1 and posts == []
        finally:
            adapter.close()

    for bad in ('signer', 'token', 'quantity', 'domain'):
        adapter, posts, signs = _signed_adapter(bad=bad)
        try:
            with pytest.raises(ValueError, match='identity'):
                adapter.lp_prepare_limit_order(token_id='1234', side='BUY',
                    price=Decimal('.4'), quantity=Decimal('5'), post_only=True, expiration=None)
            assert len(signs) == 1 and posts == []
        finally:
            adapter.close()

def _isolated_manual_runtime(tmp_path, monkeypatch, *, process='isolated-1', instance='tokyo', policy=CalculationPolicy(None)):
    from tests.test_lp_d2_manual_bridge import manual_runtime, NOW
    from open_trader.polymarket_lp_current_state import ValidatedStartAdmission
    from open_trader.prediction_arbitrage_execution import PredictionExecutionService
    from tests.test_lp_order_registration_contract import _Notifier
    execution, previous, sdk, public, clock = manual_runtime(tmp_path, monkeypatch, process=process+'-fixture')
    assert previous.close(timeout=3)
    adapter = previous.trading
    signing, _, _ = _signed_adapter(signature_type=1)
    adapter.config = replace(signing.config,lp_instance_id=instance)
    sdk.environment = signing._client.environment
    sdk.create_limit_order = signing._client.create_limit_order
    live = LPD2LiveExecution(previous.store, adapter, account_id=WALLET, venue='polymarket',
        process_epoch=process, policy=policy, utc_now=lambda:clock[0])
    live.initialize(ValidatedStartAdmission(process, 'offline-start-permit'))
    execution = PredictionExecutionService(store=live.store, monitor=object(), trading=adapter,
        notifier=_Notifier(), lock_path=tmp_path/'execution.lock', lp=execution._lp, lp_d2=live)
    execution._breaker_open = False
    return execution, live, sdk, public, clock


def test_provenance_commits_before_post_and_conflicts_never_overwrite(tmp_path, monkeypatch):
    from tests.test_lp_d2_manual_bridge import entry
    execution, live, sdk, _, clock = _isolated_manual_runtime(tmp_path, monkeypatch)
    seen = []
    def post(signed):
        rows = live.store.lp_order_provenance(WALLET, 'polymarket')
        assert len(rows) == 1
        row, = rows
        assert row['instance_id'] == 'tokyo'
        assert row['token_id'] == signed.token_id and row['side'] == 'BUY'
        assert row['quantity'] == '20' and row['price'] == '0.29'
        seen.append(row)
        return dict(order_id=row['order_id'], accepted=True, status='LIVE', taking_amount='0')
    sdk.post_order = post
    try:
        result = execution.lp_submit_entry(entry(clock))
        assert result['submit_status'] == 'accepted', result
        assert len(seen) == 1
        row, = seen
        assert live.store.lp_register_order_provenance(row) == row
        for changes in (dict(instance_id='mini'), dict(price='0.28'), dict(request_id='other')):
            with pytest.raises(ValueError, match='provenance.*conflict'):
                live.store.lp_register_order_provenance(row | changes)
        assert live.store.lp_order_provenance(WALLET, 'polymarket') == (row,)
    finally:
        assert live.close(timeout=3)
        live.trading.close()


def test_failed_or_unknown_provenance_commit_never_posts(tmp_path, monkeypatch):
    from tests.test_lp_d2_manual_bridge import entry
    connect = sqlite3.connect
    for mode in ('failed', 'unknown'):
        execution, live, sdk, _, clock = _isolated_manual_runtime(tmp_path/mode, monkeypatch)
        posts = []
        sdk.post_order = lambda signed: posts.append(None)
        class Boundary(sqlite3.Connection):
            provenance = False
            def execute(self, sql, *args, **kwargs):
                if sql.startswith('INSERT INTO lp_order_provenance'):
                    self.provenance = True
                if sql == 'COMMIT' and self.provenance:
                    if mode == 'unknown':
                        super().execute(sql, *args, **kwargs)
                    raise sqlite3.OperationalError('injected provenance commit outcome')
                return super().execute(sql, *args, **kwargs)
        try:
            with monkeypatch.context() as boundary:
                boundary.setattr(sqlite3, 'connect', lambda *a, **k: connect(*a, **(k | dict(factory=Boundary))))
                result = execution.lp_submit_entry(entry(clock))
            assert result['submit_status'] == 'not_sent', result
            assert posts == []
            rows = live.store.lp_order_provenance(WALLET, 'polymarket')
            assert len(rows) == (1 if mode == 'unknown' else 0)
            assert live.store.lp_manual_command(WALLET, 'manual-key')['response']['submit_status'] == 'not_sent'
        finally:
            assert live.close(timeout=3)
            live.trading.close()


def test_crash_after_registration_before_post_never_replays(tmp_path, monkeypatch):
    from tests.test_lp_d2_manual_bridge import entry
    from open_trader.polymarket_lp_current_state import ValidatedStartAdmission
    execution, live, sdk, _, clock = _isolated_manual_runtime(tmp_path, monkeypatch)
    posts = []
    sdk.post_order = lambda signed: posts.append(None)
    connect = sqlite3.connect
    class Crash(BaseException):
        pass
    class Boundary(sqlite3.Connection):
        provenance = False
        def execute(self, sql, *args, **kwargs):
            if sql.startswith('INSERT INTO lp_order_provenance'):
                self.provenance = True
            result = super().execute(sql, *args, **kwargs)
            if sql == 'COMMIT' and self.provenance:
                raise Crash('process exit after durable registration')
            return result
    restarted = None
    try:
        with monkeypatch.context() as boundary:
            boundary.setattr(sqlite3, 'connect', lambda *a, **k: connect(*a, **(k | dict(factory=Boundary))))
            with pytest.raises(Crash):
                execution.lp_submit_entry(entry(clock))
        assert posts == [] and sdk.cancels == []
        assert len(live.store.lp_order_provenance(WALLET, 'polymarket')) == 1
        assert live.close(timeout=3)
        reopened = PredictionArbitrageStore(tmp_path)
        restarted = LPD2LiveExecution(reopened, live.trading, account_id=WALLET,
            venue='polymarket', process_epoch='after-crash', policy=CalculationPolicy(None), utc_now=lambda:clock[0])
        restarted.initialize(ValidatedStartAdmission('after-crash', 'offline-start-permit'))
        restarted.run_once()
        snapshot = restarted.state.snapshot()
        assert snapshot.active_orders == ()
        assert snapshot.buy_reserved.value == Decimal('0')
        assert posts == [] and sdk.cancels == []
        assert reopened.lp_order_provenance(WALLET, 'polymarket') == live.store.lp_order_provenance(WALLET, 'polymarket')
    finally:
        if restarted is not None: assert restarted.close(timeout=3)
        assert live.close(timeout=3)
        live.trading.close()


def _order_from_provenance(row, *, order_id=None, side=None):
    from tests.test_lp_order_registration_contract import _open_order
    return _open_order(order_id or row['order_id'], side or row['side'],
        price=row['price'], original=row['quantity']).model_copy(update={
            'condition_id':row['condition_id'], 'token_id':row['token_id'],
            'order_type':row['order_type'],
            'expires_at':datetime.fromtimestamp(row['expiration'], UTC) if row['expiration'] else None})


def test_unknown_post_and_restart_recover_only_exact_local_order(tmp_path, monkeypatch):
    from tests.test_lp_d2_manual_bridge import entry
    from open_trader.polymarket_lp_current_state import ValidatedStartAdmission
    execution, live, sdk, _, clock = _isolated_manual_runtime(tmp_path, monkeypatch)
    posted = []
    def post(signed):
        posted.append(None)
        raise TimeoutError('lost response after venue acceptance')
    sdk.post_order = post
    restarted = None
    try:
        result = execution.lp_submit_entry(entry(clock))
        assert result['submit_status'] == 'unknown' and len(posted) == 1
        row, = live.store.lp_order_provenance(WALLET, 'polymarket')
        peer = '0x'+'ab'*32
        sdk.orders = (_order_from_provenance(row), _order_from_provenance(row,order_id=peer))
        assert live.close(timeout=3)
        restarted = LPD2LiveExecution(PredictionArbitrageStore(tmp_path), live.trading,
            account_id=WALLET, venue='polymarket', process_epoch='unknown-restart',
            policy=CalculationPolicy(None), utc_now=lambda:clock[0])
        restarted.initialize(ValidatedStartAdmission('unknown-restart', 'offline-start-permit'))
        canceled = restarted.manual_cancel(order_ids=[row['order_id'], peer])
        assert canceled['canceled'] == [row['order_id']], canceled
        assert sdk.cancels == [(row['order_id'],)] and len(posted) == 1
        assert restarted.store.lp_manual_command(WALLET, 'manual-key')['response']['submit_status'] == 'unknown'
        assert {o.key[3] for o in restarted.state.snapshot().orders} == {row['order_id'],peer}
    finally:
        if restarted is not None: assert restarted.close(timeout=3)
        assert live.close(timeout=3)
        live.trading.close()


def _register_local(live, *, side='BUY', quantity='20', price='.29', request_id='seed-order'):
    from tests.test_lp_order_registration_contract import TOKEN_ID, CONDITION_ID
    prepared = live.trading.lp_prepare_limit_order(token_id=TOKEN_ID, side=side,
        quantity=Decimal(quantity), price=Decimal(price), post_only=True, expiration=None)
    return live.store.lp_register_order_provenance(prepared.provenance(instance_id=live.lp_instance_id,
        request_id=request_id, condition_id=CONDITION_ID))


def test_round_zero_cancels_only_local_buys_and_preserves_peer_and_sells(tmp_path, monkeypatch):
    from open_trader.polymarket_lp_current_state import ValidatedStartAdmission
    from tests.test_lp_d2_integration import settle_control
    from tests.test_lp_order_registration_contract import TOKEN_ID, CONDITION_ID
    _, live, sdk, _, clock = _isolated_manual_runtime(tmp_path, monkeypatch)
    restarted = None
    try:
        buy = _register_local(live)
        sell = _register_local(live, side='SELL', request_id='seed-sell')
        assert live.close(timeout=3)
        sdk.orders = (_order_from_provenance(buy), _order_from_provenance(buy,order_id='peer-buy'),
            _order_from_provenance(buy,order_id='unknown-buy'), _order_from_provenance(sell),
            _order_from_provenance(sell,order_id='peer-sell'))
        sdk.positions = (dict(condition_id=CONDITION_ID, token_id=TOKEN_ID,
            size=Decimal('40'), average_price=Decimal('.3')),)
        restarted = LPD2LiveExecution(PredictionArbitrageStore(tmp_path), live.trading, account_id=WALLET,
            venue='polymarket', process_epoch='round-zero', policy=CalculationPolicy(None), utc_now=lambda:clock[0])
        restarted.initialize(ValidatedStartAdmission('round-zero', 'offline-start-permit'))
        assert restarted.request_running(True).status == 'accepted'
        settle_control(restarted)
        result = restarted.run_once()
        assert sdk.cancels == [(buy['order_id'],)]
        assert restarted.rounds.snapshot().round_number == 1
        assert len(result.outcomes) == 1
        assert restarted.state.snapshot().positions[0].quantity.value == Decimal('40')
        assert sdk.posts == [] and sdk.market_orders == []
        # A fresh round-zero with only foreign BUY must complete normally.
        assert restarted.request_running(False).status == 'accepted'
        settle_control(restarted)
        sdk.orders = tuple(o for o in sdk.orders if o.id != buy['order_id'])
        assert restarted.request_running(True).status == 'accepted'
        settle_control(restarted)
        restarted.run_once()
        assert restarted.rounds.snapshot().round_number == 1
        assert sdk.cancels == [(buy['order_id'],)]
        assert {o.id for o in sdk.orders} == {'peer-buy','unknown-buy',sell['order_id'],'peer-sell'}
    finally:
        if restarted is not None: assert restarted.close(timeout=3)
        assert live.close(timeout=3)
        live.trading.close()


def _restart(live, clock, *, process='restart', monotonic_clock=time.monotonic, policy=None):
    from open_trader.polymarket_lp_current_state import ValidatedStartAdmission
    assert live.close(timeout=3)
    result = LPD2LiveExecution(PredictionArbitrageStore(live.store.data_dir), live.trading,
        account_id=WALLET, venue='polymarket', process_epoch=process, policy=policy or live.policy,
        utc_now=lambda:clock[0], clock=monotonic_clock)
    result.initialize(ValidatedStartAdmission(process, 'offline-start-permit'))
    return result


def test_protection_and_same_price_bucket_never_enroll_peer_orders(tmp_path, monkeypatch):
    _, live, sdk, public, clock = _isolated_manual_runtime(tmp_path, monkeypatch)
    monotonic = [0.0]
    restarted = None
    try:
        row = _register_local(live)
        sdk.orders = (_order_from_provenance(row),_order_from_provenance(row,order_id='peer-same-price'))
        restarted = _restart(live, clock, monotonic_clock=lambda:monotonic[0])
        restarted.run_once()
        restarted.run_protection_once()
        snapshot = restarted.state.snapshot()
        assert {oid for baseline in snapshot.baselines.values() for oid in baseline.anchor_order_ids} == {row['order_id']}
        assert {key[3] for key in snapshot.yields} == {row['order_id']}
        assert {o['order_id'] for s in restarted.store.lp_sessions() for o in s.get('exchange_orders',[])} <= {row['order_id']}
        def failed_book(**kwargs):
            raise ConnectionError('offline book outage')
        public.get_order_book = failed_book
        for index in range(12):
            monotonic[0] += 61
            clock[0] += timedelta(seconds=61)
            restarted.run_once()
            before = (sdk.order_reads, public.book_reads)
            for attempt in restarted.run_protection_once():
                attempt.result(timeout=3)
                attempt._thread.join(3)
                assert attempt.done()
            assert before == (sdk.order_reads, public.book_reads)
        assert sdk.cancels and {oid for batch in sdk.cancels for oid in batch} == {row['order_id']}
        assert sdk.posts == [] and sdk.market_orders == []
    finally:
        if restarted is not None: assert restarted.close(timeout=3)
        assert live.close(timeout=3)
        live.trading.close()


def test_trial_income_rotation_preserves_peer_orders_and_wallet_constraints(tmp_path, monkeypatch):
    from tests.test_lp_d2_integration import settle_control
    from open_trader.polymarket_lp_market_reader import SourceResult
    for pool in ('trial','income'):
        _, live, sdk, _, clock = _isolated_manual_runtime(tmp_path/pool, monkeypatch)
        restarted = None
        try:
            row = _register_local(live)
            policy = CalculationPolicy(Decimal('40'),3,allocation_mode='observed_pools',
                trial_budget_usd='40',income_budget_usd='20')
            restarted = _restart(live, clock, process='pool-'+pool, policy=policy)
            assert restarted.request_running(True).status == 'accepted'
            settle_control(restarted)
            restarted.run_once(pool=pool)  # empty round zero
            sdk.orders = (_order_from_provenance(row), *(
                _order_from_provenance(row,order_id='peer-'+str(i)) for i in range(5)))
            result = restarted.run_once(SourceResult('empty-candidates', (),checked_at=clock[0]),pool=pool)
            snapshot = restarted.state.snapshot()
            assert snapshot.buy_reserved.value == Decimal('34.80')
            assert snapshot.new_capacity.value == Decimal('25.20')
            assert set(result.selection.yields) <= {row['order_id']}
            assert set(result.selection.victim_order_ids) <= {row['order_id']}
            dispatched = restarted.dispatch_rotation(result)
            for outcome in dispatched.rotation_outcomes:
                if outcome.handle:
                    outcome.handle.result(timeout=3)
                    outcome.handle._thread.join(3)
            assert {oid for batch in sdk.cancels for oid in batch} <= {row['order_id']}
            sdk.get_balance_allowance = lambda **kwargs: SimpleNamespace(balance='0',allowances={sdk.environment.standard_exchange:'0'})
            blocked = restarted.run_once(SourceResult('empty-candidates-2',(),checked_at=clock[0]),pool=pool)
            assert restarted.state.snapshot().new_capacity.value == 0
            assert blocked.outcomes == () and sdk.posts == []
        finally:
            if restarted is not None: assert restarted.close(timeout=3)
            assert live.close(timeout=3)
            live.trading.close()


def test_explicit_cancel_scope_all_checks_each_buy_sell_owner(tmp_path, monkeypatch):
    from tests.test_lp_order_registration_contract import CONDITION_ID
    from tests.test_lp_d2_integration import settle_control
    from open_trader.polymarket_lp_live_execution import RotationDecision
    for selector in ('ids','condition','displayed','mixed-native'):
        _, live, sdk, _, clock = _isolated_manual_runtime(tmp_path/selector, monkeypatch)
        restarted = None
        try:
            buy = _register_local(live)
            sell = _register_local(live,side='SELL',request_id='seed-sell')
            restarted = _restart(live, clock, policy=CalculationPolicy(Decimal('40'),3,
                allocation_mode='observed_pools',trial_budget_usd='40'))
            if selector == 'mixed-native':
                restarted.request_running(True)
                settle_control(restarted)
                restarted.run_once()
            sdk.orders = (_order_from_provenance(buy),_order_from_provenance(sell),
                _order_from_provenance(buy,order_id='peer-buy'),_order_from_provenance(sell,order_id='unknown-sell'))
            ids = [o.id for o in sdk.orders]
            if selector == 'mixed-native':
                observed = restarted.run_once()
                snapshot = restarted.state.snapshot()
                decision = RotationDecision(WALLET,snapshot.current.cycle_generation,
                    snapshot.observation.observation_id,tuple(o for o in snapshot.active_orders if o.side == 'BUY'))
                dispatched = restarted.dispatch_rotation(replace(observed,rotation=decision))
                for outcome in dispatched.rotation_outcomes:
                    if outcome.handle:
                        outcome.handle.result(timeout=3)
                        outcome.handle._thread.join(3)
                assert sdk.cancels == []
                with pytest.raises(ValueError,match='ownership'):
                    restarted.cancel_independent(next(o.key for o in snapshot.active_orders if o.key[3] == 'peer-buy'))
            else:
                kwargs = dict(order_ids=ids) if selector == 'ids' else dict(condition_id=CONDITION_ID) if selector == 'condition' else dict(displayed_ids=ids)
                result = restarted.manual_cancel(**kwargs)
                assert set(result['canceled']) == {buy['order_id'],sell['order_id']}
                assert {r['order_id'] for r in result['skipped']} == {'peer-buy','unknown-sell'}
                assert sdk.cancels == [(buy['order_id'],sell['order_id'])]
            assert sdk.posts == [] and sdk.market_orders == []
        finally:
            if restarted is not None: assert restarted.close(timeout=3)
            assert live.close(timeout=3)
            live.trading.close()


def test_sell_reprice_and_named_stop_do_not_claim_peer_sell(tmp_path, monkeypatch):
    from tests.test_lp_d2_manual_bridge import entry
    from tests.test_lp_order_registration_contract import _maker_order, _trade, CONDITION_ID, TOKEN_ID
    execution, live, sdk, _, clock = _isolated_manual_runtime(tmp_path, monkeypatch)
    posted = []
    def post(signed):
        rows = live.store.lp_order_provenance(WALLET,'polymarket')
        row = next(r for r in rows if r['token_id'] == signed.token_id and r['side'] == signed.side)
        posted.append(row['order_id'])
        sdk.orders += (_order_from_provenance(row),)
        return dict(accepted=True,order_id=row['order_id'],status='LIVE',taking_amount='0')
    sdk.post_order = post
    try:
        response = execution.lp_submit_entry(entry(clock))
        assert response['submit_status'] == 'accepted'
        row, = live.store.lp_order_provenance(WALLET,'polymarket')
        clock[0] += timedelta(seconds=1)
        peer = _order_from_provenance(row,order_id='peer-sell',side='SELL').model_copy(update={
            'created_at':clock[0], 'original_size':Decimal('5'), 'size_matched':Decimal('5'),
            'status':'MATCHED','price':Decimal('.32')})
        sdk.orders = (sdk.orders[0].model_copy(update={'status':'MATCHED','size_matched':Decimal('20')}),peer)
        def trade(identity, order_id, side, quantity, price):
            return _trade(identity,_maker_order(order_id,side,quantity,price),size=quantity).model_copy(
                update={'matched_at':clock[0], 'updated_at':clock[0]})
        sdk.trades = (trade('local-fill',row['order_id'],'BUY','20','.29'),
            trade('peer-fill','peer-sell','SELL','5','.32'))
        sdk.positions = (dict(condition_id=CONDITION_ID,token_id=TOKEN_ID,size='15',average_price='.29'),)
        for _ in range(2):
            live.run_once()
            for handle in live.run_protection_once():
                handle.result(timeout=3)
                handle._thread.join(3)
        snapshot = live.state.snapshot()
        assert (WALLET,CONDITION_ID,TOKEN_ID,'peer-sell') not in snapshot.own_fills
        sell, = snapshot.sells.values()
        assert sell.position_reconciled is False
        stopped = execution.lp_stop(response['session_id'])
        assert stopped['state'] in ('review','needs_attention')
        assert all('peer-sell' not in batch for batch in sdk.cancels)
        assert posted == [row['order_id']] and sdk.market_orders == []
        assert snapshot.positions[0].quantity.value == Decimal('15')
    finally:
        assert live.close(timeout=3)
        live.trading.close()
    for mode in ('passive','protected'):
        _assert_sell_registration(tmp_path/mode, monkeypatch, mode)


def _assert_sell_registration(tmp_path, monkeypatch, mode):
    from tests.test_lp_d2_manual_bridge import entry
    from tests.test_lp_order_registration_contract import _maker_order, _trade, CONDITION_ID, TOKEN_ID
    execution, live, sdk, public, clock = _isolated_manual_runtime(tmp_path,monkeypatch)
    posts = []
    def post(signed):
        rows = live.store.lp_order_provenance(WALLET,'polymarket')
        row = next((r for r in rows if r['side'] == signed.side and r['protocol_fields']['salt'] == signed.salt),None)
        assert row is not None, 'SELL reached transport without reliable provenance'
        posts.append((signed.side,signed.order_type,row['order_id']))
        sdk.orders += (_order_from_provenance(row),)
        return dict(accepted=True,order_id=row['order_id'],status='LIVE',taking_amount='0',making_amount='0')
    sdk.post_order = post
    create = sdk.create_limit_order
    def market(**kwargs):
        signed = create(token_id=kwargs['token_id'], side='SELL', price=kwargs['min_price'],
            size=kwargs['shares'], expiration=None, post_only=True)
        return replace(signed,order_type='FOK',post_only=False)
    sdk.create_market_order = market
    try:
        response = execution.lp_submit_entry(entry(clock))
        assert response['submit_status'] == 'accepted'
        local_id = response['entry_order_id']
        clock[0] += timedelta(seconds=1)
        sdk.orders = (sdk.orders[0].model_copy(update={'size_matched':Decimal('20'),'status':'FILLED'}),)
        sdk.positions = (dict(condition_id=CONDITION_ID,token_id=TOKEN_ID,size='20',average_price='.29'),)
        sdk.trades = (_trade('own-buy',_maker_order(local_id,'BUY','20','.29'),size='20').model_copy(
            update={'matched_at':clock[0],'updated_at':clock[0]}),)
        if mode == 'protected':public.bid=Decimal('.01')
        for _ in range(3):
            live.run_once()
            for handle in live.run_protection_once():
                handle.result(timeout=3)
                handle._thread.join(3)
                assert handle.done()
        assert [p[:2] for p in posts] == [('BUY','GTD'),('SELL','GTC')]
        assert len(live.store.lp_order_provenance(WALLET,'polymarket')) == 2
        assert sdk.cancels == []
    finally:
        assert live.close(timeout=3)
        live.trading.close()


def test_delayed_registration_rechecks_control_and_current_facts(tmp_path, monkeypatch):
    from tests.test_lp_d2_manual_bridge import entry
    connect = sqlite3.connect
    for mode in ('control','facts'):
        execution,live,sdk,_,clock = _isolated_manual_runtime(tmp_path/mode,monkeypatch)
        entered,release = threading.Event(),threading.Event()
        results,errors,posts = [],[],[]
        sdk.post_order = lambda signed:posts.append(None)
        class Boundary(sqlite3.Connection):
            provenance=False
            def execute(self,sql,*args,**kwargs):
                if sql.startswith('INSERT INTO lp_order_provenance'):self.provenance=True
                if sql=='COMMIT' and self.provenance:
                    entered.set()
                    if not release.wait(5):raise TimeoutError('registration release watchdog')
                return super().execute(sql,*args,**kwargs)
        def submit():
            try:results.append(execution.lp_submit_entry(entry(clock)))
            except BaseException as error:errors.append(type(error).__name__)
        worker=threading.Thread(target=submit)
        try:
            with monkeypatch.context() as boundary:
                boundary.setattr(sqlite3,'connect',lambda *a,**k:connect(*a,**(k|dict(factory=Boundary))))
                worker.start()
                assert entered.wait(3)
                if mode=='control':
                    assert live.close(timeout=0) is False
                else:
                    sdk.get_balance_allowance=lambda **kwargs:SimpleNamespace(balance='0',allowances={sdk.environment.standard_exchange:'0'})
                    live.manual_cancel(order_ids=[])
                release.set()
                worker.join(5)
                assert not worker.is_alive() and errors==[]
            assert results[0]['submit_status']=='not_sent'
            assert posts==[] and sdk.cancels==[]
            assert len(live.store.lp_order_provenance(WALLET,'polymarket'))==1
        finally:
            release.set()
            if worker.ident:worker.join(5)
            assert not worker.is_alive()
            assert live.close(timeout=3)
            live.trading.close()


def test_receipt_id_mismatch_never_grants_another_order_ownership(tmp_path,monkeypatch):
    from tests.test_lp_d2_manual_bridge import entry
    execution,live,sdk,_,clock=_isolated_manual_runtime(tmp_path,monkeypatch)
    posts=[]
    def post(signed):
        posts.append(None)
        return dict(accepted=True,order_id='peer-receipt-id',status='LIVE',taking_amount='0')
    sdk.post_order=post
    try:
        result=execution.lp_submit_entry(entry(clock))
        assert result['submit_status']=='unknown',result
        rows=live.store.lp_order_provenance(WALLET,'polymarket')
        assert len(rows)==1 and rows[0]['order_id']!='peer-receipt-id'
        snapshot=live.state.snapshot()
        assert all(o.key[3]!='peer-receipt-id' for o in snapshot.orders)
        assert snapshot.buy_reserved.state=='unknown' and snapshot.new_capacity.state=='unknown'
        assert len(posts)==1
        sdk.orders=(_order_from_provenance(rows[0],order_id='peer-receipt-id'),)
        canceled=live.manual_cancel(order_ids=['peer-receipt-id'])
        assert canceled['canceled']==[] and sdk.cancels==[]
        assert live.store.lp_manual_command(WALLET,'manual-key')['response']['submit_status']=='unknown'
    finally:
        assert live.close(timeout=3)
        live.trading.close()


def test_shutdown_keeps_all_actual_inflight_owners(tmp_path,monkeypatch):
    from tests.test_lp_d2_integration import settle_control
    _,live,sdk,_,clock=_isolated_manual_runtime(tmp_path,monkeypatch)
    releases=[threading.Event(),threading.Event()]
    entered=[threading.Event(),threading.Event()]
    restarted=None
    handles=[]
    try:
        rows=[_register_local(live,quantity=str(20+i),request_id='shutdown-'+str(i)) for i in range(2)]
        restarted=_restart(live,clock)
        restarted.request_running(True)
        settle_control(restarted)
        restarted.run_once()
        sdk.orders=tuple(_order_from_provenance(row) for row in rows)+(_order_from_provenance(rows[0],order_id='peer'),)
        restarted.run_once()
        ids=[r['order_id'] for r in rows]
        def cancel(*,order_ids):
            sdk.cancels.append(order_ids)
            index=ids.index(order_ids[0])
            entered[index].set()
            assert releases[index].wait(5),'native exit watchdog'
            return dict(canceled=order_ids,not_canceled={})
        sdk.cancel_orders=cancel
        keys={o.key[3]:o.key for o in restarted.state.snapshot().active_orders}
        handles=[restarted.cancel_independent(keys[oid]) for oid in ids]
        assert all(event.wait(3) for event in entered)
        assert restarted.status()['native_inflight']==2
        assert restarted.close(timeout=0) is False
        releases[0].set()
        handles[0].result(timeout=3)
        handles[0]._thread.join(3)
        assert handles[0].done() and not handles[1].done()
        assert restarted.close(timeout=0) is False
        assert restarted.status()['native_inflight']==1
        releases[1].set()
        handles[1].result(timeout=3)
        handles[1]._thread.join(3)
        assert restarted.close(timeout=3) is True
        assert {oid for batch in sdk.cancels for oid in batch}==set(ids)
        assert len(sdk.cancels)==2 and sdk.posts==[]
    finally:
        for event in releases:event.set()
        for handle in handles:handle._thread.join(5)
        if restarted is not None:assert restarted.close(timeout=3)
        assert live.close(timeout=3)
        live.trading.close()


def test_provenance_uses_reliable_coordinator_and_survives_reopen(tmp_path,monkeypatch):
    from open_trader.lp_persistence import LPWriteCoordinator, LP_WRITE_OPERATIONS, write_priority
    from tests.test_lp_order_registration_contract import TOKEN_ID, CONDITION_ID
    _,live,sdk,_,clock=_isolated_manual_runtime(tmp_path,monkeypatch)
    prepared=live.trading.lp_prepare_limit_order(token_id=TOKEN_ID,side='BUY',
        price=Decimal('.29'),quantity=Decimal('20'),post_only=True,expiration=None)
    row=prepared.provenance(instance_id='tokyo',request_id='reliable',condition_id=CONDITION_ID)
    connect=sqlite3.connect
    started,exited=threading.Event(),threading.Event()
    results,errors=[],[]
    peer=PredictionArbitrageStore(tmp_path,initialize=False)
    coordinator=LPWriteCoordinator(peer.path)
    blocker=connect(peer.path,isolation_level=None)
    blocker.execute('BEGIN IMMEDIATE')
    class Boundary(sqlite3.Connection):
        def execute(self,sql,*args,**kwargs):
            if sql=='BEGIN IMMEDIATE' and threading.current_thread().name=='provenance-writer':started.set()
            return super().execute(sql,*args,**kwargs)
    def register():
        try:results.append(peer.lp_register_order_provenance(row))
        except BaseException as error:errors.append(type(error).__name__)
        finally:exited.set()
    worker=threading.Thread(target=register,name='provenance-writer')
    try:
        with monkeypatch.context() as boundary:
            boundary.setattr(sqlite3,'connect',lambda *a,**k:connect(*a,**(k|dict(factory=Boundary))))
            worker.start()
            assert started.wait(3) and not exited.is_set()
            assert coordinator.status()['holder'] is True
            blocker.execute('COMMIT')
            assert exited.wait(3)
            worker.join(3)
        assert errors==[] and results==[row]
        assert 'lp_register_order_provenance' in LP_WRITE_OPERATIONS
        assert write_priority('lp_register_order_provenance')=='reliable'
        transactions=coordinator.status()['transactions']
        assert any(r['operation']=='PredictionArbitrageStore.lp_register_order_provenance' and r['priority']=='reliable' and not r['failed'] for r in transactions)
        reopened=PredictionArbitrageStore(tmp_path,initialize=False)
        assert reopened.lp_order_provenance(WALLET,'polymarket')==(row,)
        with connect(f'file:{reopened.path}?mode=ro',uri=True) as readonly:
            persisted=readonly.execute('SELECT payload FROM lp_order_provenance').fetchone()[0]
            assert json.loads(persisted)==row
            assert not any(key in persisted for key in ('signature"','private_key','signed_order','credentials'))
        assert sdk.posts==[] and sdk.cancels==[]
    finally:
        if blocker.in_transaction:blocker.execute('ROLLBACK')
        blocker.close()
        if worker.ident:worker.join(5)
        assert not worker.is_alive()
        assert live.close(timeout=3)
        live.trading.close()


def test_unattributed_buy_is_reported_without_cross_cancel_or_false_financial_claims(tmp_path,monkeypatch):
    from tests.test_lp_d2_manual_bridge import entry, OTHER_CID, OTHER_TOKEN
    from tests.test_lp_order_registration_contract import _open_order
    execution,live,sdk,_,clock=_isolated_manual_runtime(tmp_path,monkeypatch)
    try:
        sdk.orders=(_open_order('unattributed','BUY',price='.29',original='10'),)
        live.run_once()
        ownership=live.status()['instance_ownership']
        assert ownership['instance_id']=='tokyo'
        assert ownership['unattributed_buy_ids']==['unattributed']
        assert ownership['managed_order_ids']==[] and ownership['ownership_allows_new_risk'] is True
        assert ownership['financial_admission']=='requires_current_wallet_checks'
        assert ownership['nonlocal_orders_automatically_protected'] is False
        assert ownership['alert_code']=='LP_ORDER_OWNERSHIP_UNKNOWN'
        assert live.diagnostics()['instance_ownership']==ownership
        assert live.state.snapshot().buy_reserved.value==Decimal('2.90')
        sdk.get_balance_allowance=lambda **kwargs:SimpleNamespace(balance='0',allowances={sdk.environment.standard_exchange:'0'})
        result=execution.lp_submit_entry(entry(clock,market_id='market-2',condition_id=OTHER_CID,token_id=OTHER_TOKEN))
        assert result['state']=='rejected' and result['reason']!='unattributed_buy_orders'
        canceled=live.manual_cancel(order_ids=['unattributed'])
        assert canceled['canceled']==[]
        assert sdk.posts==[] and sdk.cancels==[]
        assert live.store.lp_order_provenance(WALLET,'polymarket')==()
    finally:
        assert live.close(timeout=3)
        live.trading.close()


def test_bound_instance_cannot_be_disabled_or_replaced_on_restart(tmp_path,monkeypatch):
    _,live,sdk,_,clock=_isolated_manual_runtime(tmp_path/'bound',monkeypatch)
    adapter=live.trading
    config=adapter.config
    row=_register_local(live)
    assert live.close(timeout=3)
    try:
        for replacement in (None,'mini'):
            adapter.config=replace(config,lp_instance_id=replacement)
            with pytest.raises(ValueError,match='instance.*bound'):
                LPD2LiveExecution(PredictionArbitrageStore(tmp_path/'bound'),adapter,
                    account_id=WALLET,venue='polymarket',process_epoch='replacement',policy=CalculationPolicy(None))
            assert live.store.lp_order_provenance(WALLET,'polymarket')==(row,)
            assert sdk.posts==[] and sdk.cancels==[]
        adapter.config=config
        same=_restart(live,clock,process='same-instance')
        assert same.lp_instance_id=='tokyo'
        assert same.close(timeout=3)
        legacy_adapter,legacy_sdk=_adapter(tmp_path/'legacy')
        legacy=LPD2LiveExecution(PredictionArbitrageStore(tmp_path/'legacy'),legacy_adapter,
            account_id=WALLET,venue='polymarket',process_epoch='legacy',policy=CalculationPolicy(None))
        try:
            assert legacy.lp_instance_id is None
            assert legacy_sdk.posts==[] and legacy_sdk.cancels==[]
        finally:
            assert legacy.close(timeout=3)
            legacy_adapter.close()
    finally:
        adapter.config=config
        adapter.close()


def test_explicit_instance_legacy_fallback_cannot_send_or_cancel(tmp_path):
    from tests.test_lp_order_registration_contract import _runtime, _open_order
    _,adapter,sdk,_,execution=_runtime(tmp_path/'isolated',orders=(_open_order('foreign','BUY',price='.29',original='10'),))
    adapter.config=replace(adapter.config,lp_instance_id='tokyo')
    try:
        mutations=(lambda:execution.lp_cancel_orders(dict(confirm=True,order_ids=['foreign'])),
            lambda:execution.lp_submit_entry(dict(idempotency_key='entry')),
            lambda:execution.lp_submit_augment(dict(session_id='old',idempotency_key='augment')),
            lambda:execution.lp_start('old','start'),lambda:execution.lp_augment('old','old','augment'),
            lambda:execution.lp_stop('old'),lambda:execution.lp_tick(),
            lambda:execution.lp_auto_run_once(),lambda:execution.lp_auto_scheduled_check(),
            lambda:execution.lp_auto_set_desired_running(True))
        for operation in mutations:
            with pytest.raises(ValueError,match='lp_instance_requires_d2_owner'):
                operation()
        assert execution.lp_mutation_allowed() is False
        assert sdk.posts==[] and sdk.market_orders==[] and sdk.cancels==[]
    finally:
        adapter.close()
    _,legacy_adapter,legacy_sdk,_,legacy=_runtime(tmp_path/'legacy',orders=(_open_order('legacy-local','BUY',price='.29',original='10'),))
    try:
        result=legacy.lp_cancel_orders(dict(confirm=True,order_ids=['legacy-local']))
        assert result['canceled']==['legacy-local']
        assert legacy_sdk.cancels==[('legacy-local',)]
        assert legacy_sdk.posts==[] and legacy_sdk.market_orders==[]
    finally:
        legacy_adapter.close()


def test_automatic_registration_rechecks_current_financial_origin_before_post(tmp_path,monkeypatch):
    from tests.test_lp_d2_integration import settle_control
    from tests.test_lp_order_registration_contract import TOKEN_ID,CONDITION_ID,_open_order
    from open_trader.polymarket_lp_market_reader import SourceResult,MarketReader,ReadScope
    from open_trader.polymarket_lp_current_inputs import prepare_current_inputs
    from open_trader.polymarket_lp_market_calculation import calculate_current
    connect=sqlite3.connect
    for mode in ('funds','orders','control','unchanged'):
        _,previous,sdk,_,clock=_isolated_manual_runtime(tmp_path/mode,monkeypatch)
        live=_restart(previous,clock,process='automatic-'+mode,policy=CalculationPolicy(Decimal('40'),1))
        live.request_running(True);settle_control(live);live.run_once()
        book=live.trading.lp_order_books((TOKEN_ID,))[TOKEN_ID]
        candidates=SourceResult('original-automatic-plan',(
            dict(condition_id=CONDITION_ID,market_id='market-1',token_id=TOKEN_ID,
                venue='polymarket',price=Decimal('.30'),quantity=Decimal('20'),
                order_type='GTC',expiration=None,post_only=True,send_options=(),
                direction=dict(market=dict(reward_min_size=Decimal('1'),reward_max_spread=Decimal('.10')),
                    book=book,daily_pool_usd=Decimal('24'))),),checked_at=clock[0])
        entered,release=threading.Event(),threading.Event()
        results,errors,posts=[],[],[]
        class Boundary(sqlite3.Connection):
            provenance=False
            def execute(self,sql,*args,**kwargs):
                if sql.startswith('INSERT INTO lp_order_provenance'):self.provenance=True
                if sql=='COMMIT' and self.provenance:
                    entered.set()
                    assert release.wait(5),'automatic provenance watchdog'
                return super().execute(sql,*args,**kwargs)
        def post(signed):
            row,=live.store.lp_order_provenance(WALLET,'polymarket')
            posts.append(row['order_id'])
            return dict(accepted=True,status='LIVE',order_id=row['order_id'],taking_amount='0')
        sdk.post_order=post
        def run():
            try:results.append(live.run_once(candidates))
            except BaseException as error:errors.append(type(error).__name__)
        worker=threading.Thread(target=run,name='automatic-registration')
        reader=MarketReader(live.trading.lp_reader_source_port())
        try:
            with monkeypatch.context() as boundary:
                boundary.setattr(sqlite3,'connect',lambda *a,**k:connect(*a,**(k|dict(factory=Boundary))))
                worker.start();assert entered.wait(3)
                if mode=='control':assert live.close(timeout=0) is False
                elif mode!='unchanged':
                    if mode=='funds':sdk.get_balance_allowance=lambda **kwargs:SimpleNamespace(balance='0',allowances={sdk.environment.standard_exchange:'0'})
                    else:sdk.orders=(_open_order('unknown-current-buy','BUY',price='.29',original='10'),)
                    snapshot=live.state.snapshot()
                    ticket=live.state.begin_observation(snapshot.current.cycle_generation,('balance','open_orders','positions'))
                    frame=reader.read_current(ReadScope(WALLET),time.monotonic()+2)
                    prepared=prepare_current_inputs(frame,None,live.policy,clock[0])
                    accepted=live.state.accept(ticket,frame,calculate_current(prepared,live.policy,clock[0]),evaluated_at=clock[0])
                    assert accepted.status in ('accepted','partial')
                release.set();worker.join(5)
                assert not worker.is_alive() and errors==[]
            outcome,=results[0].outcomes
            assert outcome.status.value==('accepted' if mode=='unchanged' else 'not_sent')
            assert len(posts)==(1 if mode=='unchanged' else 0)
            assert len(live.store.lp_order_provenance(WALLET,'polymarket'))==1
            assert sdk.cancels==[]
        finally:
            release.set()
            if worker.ident:worker.join(5)
            assert not worker.is_alive()
            assert reader.close(timeout=3)
            assert live.close(timeout=3)
            previous.trading.close()


def test_instance_health_reports_actual_bound_owner_and_source_identity(tmp_path):
    import hashlib
    from urllib.error import HTTPError
    from urllib.request import build_opener,ProxyHandler
    from open_trader.prediction_runtime import PredictionRuntime
    from open_trader.prediction_service import create_prediction_server
    from tests.lp_d2_service_harness import ServiceChild
    runtime=PredictionRuntime(data_dir=tmp_path/'unavailable',prediction_config_path=tmp_path/'absent.json',
        dashboard_url='http://127.0.0.1:8769',mode='shadow')
    server=create_prediction_server(runtime=runtime,port=0)
    serving=threading.Thread(target=server.serve_forever)
    serving.start()
    try:
        with pytest.raises(HTTPError) as error:
            build_opener(ProxyHandler({})).open(f'http://127.0.0.1:{server.server_port}/healthz')
        assert error.value.code==503
        assert json.load(error.value)['lp_instance_isolation']['enabled'] is False
    finally:
        server.shutdown();server.server_close();serving.join(3)
        assert not serving.is_alive()
    for scenario in ('consumer-instance-health','consumer-disabled-background'):
        with ServiceChild(tmp_path/scenario,scenario=scenario) as child:
            child.release("metadata")
            child.start(expected_running=False)
            health=child.request('/healthz')
            assert health['production_owner'] is True and health['runtime_state']=='RUNNING'
            capability=health['lp_instance_isolation']
            if scenario!='consumer-instance-health':
                assert capability['enabled'] is False
                assert capability['reason']=='legacy_unbound'
                continue
            assert capability['enabled'] is True
            assert capability['schema_version']=='open_trader.lp.local_instance.v1'
            assert capability['instance_id']=='tokyo' and capability['account_id']==WALLET
            assert capability['venue']=='polymarket'
            assert capability['source_sha']==child.sha==health['git_sha']
            assert capability['config_path']==str(child.config_path)
            assert capability['config_sha256']==hashlib.sha256(child.config_path.read_bytes()).hexdigest()
            assert capability['runtime_root']==str(child.runtime_root)
            assert capability['code_root']==str(child.release_root)
            claim=json.loads(child.permit_path.read_text())
            assert capability['process_epoch']==claim['process_epoch']
            child.assert_owned()
            with sqlite3.connect(child.data_dir/'prediction_arbitrage/prediction_arbitrage.sqlite3') as connection:
                # Confirm the public capability corresponds to actual durable binding.
                assert connection.execute('SELECT instance_id FROM lp_instance_binding').fetchone()==('tokyo',)
            original=child.config_path.read_bytes()
            try:
                child.config_path.write_bytes(original+b'\n')
                changed=child.request('/healthz')['lp_instance_isolation']
                assert changed['enabled'] is False and changed['reason']=='identity_changed'
            finally:child.config_path.write_bytes(original)
            source=child.release_root/'src/open_trader/prediction_runtime.py'
            original_source=source.read_bytes()
            try:
                source.write_bytes(original_source+b'\n# offline source identity mismatch\n')
                changed=child.request('/healthz')['lp_instance_isolation']
                assert changed['enabled'] is False and changed['reason']=='identity_changed'
            finally:source.write_bytes(original_source)
            assert child.request('/healthz')['lp_instance_isolation']['enabled'] is True
            assert child.actual_posts()==0


def test_nonlocal_known_buy_retains_wallet_reserve_without_blocking_proven_local_new_buy(tmp_path,monkeypatch):
    from tests.test_lp_d2_manual_bridge import entry,OTHER_CID,OTHER_TOKEN
    execution,live,sdk,_,clock=_isolated_manual_runtime(tmp_path/'tokyo',monkeypatch,
        policy=CalculationPolicy(Decimal('100'),2))
    _,mini,mini_sdk,_,_=_isolated_manual_runtime(tmp_path/'mini',monkeypatch,instance='mini')
    try:
        a=_register_local(live,quantity='50',price='.40')
        b=_register_local(mini,quantity='100',price='.40')
        live=_restart(live,clock,policy=CalculationPolicy(Decimal('100'),2))
        from open_trader.prediction_arbitrage_execution import PredictionExecutionService
        from open_trader.polymarket_lp import PolymarketLPService
        from tests.test_lp_order_registration_contract import _Notifier
        execution=PredictionExecutionService(store=live.store,monitor=object(),trading=live.trading,
            notifier=_Notifier(),lock_path=tmp_path/'tokyo/restarted.lock',lp=PolymarketLPService(live.store,live.trading),lp_d2=live)
        execution._breaker_open=False
        sdk.orders=mini_sdk.orders=(_order_from_provenance(a),_order_from_provenance(b))
        live.run_once()
        snapshot=live.state.snapshot()
        assert snapshot.buy_reserved.value==Decimal('60')
        assert snapshot.new_capacity.value==Decimal('40')
        assert {o.key[3] for o in snapshot.active_orders}=={a['order_id'],b['order_id']}
        ownership=live.status()['instance_ownership']
        assert ownership['managed_order_ids']==[a['order_id']]
        assert ownership['unattributed_buy_ids']==[b['order_id']]
        posts=[]
        def post(signed):
            rows=live.store.lp_order_provenance(WALLET,'polymarket')
            row,=(r for r in rows if r['request_id']!=a['request_id'])
            posts.append(row)
            return dict(accepted=True,status='LIVE',order_id=row['order_id'],taking_amount='0')
        sdk.post_order=post
        payload=entry(clock,market_id='market-2',condition_id=OTHER_CID,token_id=OTHER_TOKEN,quantity='100')
        result=execution.lp_submit_entry(payload)
        assert result['state']=='entry_open' and result['submit_status']=='accepted',result
        assert len(posts)==1 and Decimal(posts[0]['price'])*Decimal(posts[0]['quantity'])==Decimal('29')
        assert Decimal(posts[0]['price'])*Decimal(posts[0]['quantity'])<=Decimal('40')
        assert execution.lp_submit_entry(payload)==result and len(posts)==1
        canceled=live.manual_cancel(order_ids=[a['order_id'],b['order_id']])
        assert canceled['canceled']==[a['order_id']]
        assert sdk.cancels==[(a['order_id'],)] and mini_sdk.cancels==[]
        assert len(mini.store.lp_order_provenance(WALLET,'polymarket'))==1
    finally:
        assert live.close(timeout=3) and mini.close(timeout=3)
        live.trading.close();mini.trading.close()


def test_nonlocal_financial_unknown_still_blocks_new_risk(tmp_path,monkeypatch):
    from tests.test_lp_d2_manual_bridge import entry,OTHER_CID,OTHER_TOKEN
    for fault in ('balance','orders','positions','terms'):
        execution,live,sdk,_,clock=_isolated_manual_runtime(tmp_path/fault,monkeypatch,
            policy=CalculationPolicy(Decimal('100'),2))
        _,mini,_,_,_=_isolated_manual_runtime(tmp_path/(fault+'-mini'),monkeypatch,instance='mini')
        try:
            b=_register_local(mini,quantity='100',price='.40')
            sdk.orders=(_order_from_provenance(b),)
            if fault=='balance':sdk.get_balance_allowance=lambda **kwargs:None
            elif fault=='orders':sdk.orders=None
            elif fault=='positions':sdk.positions=None
            else:sdk.orders=(sdk.orders[0].model_copy(update={'original_size':None}),)
            live.run_once()
            snapshot=live.state.snapshot()
            assert snapshot.new_capacity.state=='unknown'
            if fault in ('orders','terms'):assert snapshot.buy_reserved.state=='unknown'
            if fault=='positions':assert snapshot.inventory_cost.state=='unknown'
            result=execution.lp_submit_entry(entry(clock,market_id='market-2',condition_id=OTHER_CID,token_id=OTHER_TOKEN))
            assert result['state']=='rejected',result
            assert sdk.posts==[] and sdk.cancels==[]
            assert live.store.lp_order_provenance(WALLET,'polymarket')==()
            assert len(mini.store.lp_order_provenance(WALLET,'polymarket'))==1
        finally:
            assert live.close(timeout=3) and mini.close(timeout=3)
            live.trading.close();mini.trading.close()


def test_two_local_owners_never_cross_cancel_without_shared_provenance(tmp_path,monkeypatch):
    from tests.test_lp_d2_manual_bridge import entry
    from tests.test_lp_d2_integration import settle_control
    owners=[]
    try:
        for instance in ('mini','tokyo'):
            execution,live,sdk,public,clock=_isolated_manual_runtime(tmp_path/instance,monkeypatch,instance=instance)
            owners.append([execution,live,sdk,public,clock])
            def unknown(signed):raise TimeoutError('offline response lost after acceptance')
            sdk.post_order=unknown
            response=execution.lp_submit_entry(entry(clock))
            assert response['submit_status']=='unknown'
            row,=live.store.lp_order_provenance(WALLET,'polymarket')
            sell=_register_local(live,side='SELL',quantity='5',request_id='local-sell')
            owners[-1].extend([row,sell])
        rows=tuple(row for owner in owners for row in owner[-2:])
        assert len({r['order_id'] for r in rows})==4
        for owner in owners:
            execution,previous,sdk,public,clock,buy,sell=owner
            sdk.orders=tuple(_order_from_provenance(row) for row in rows)
            live=_restart(previous,clock,process=buy['instance_id']+'-restart')
            owner[1]=live
            live.run_once();live.run_protection_once()
            snapshot=live.state.snapshot()
            assert {o.key[3] for o in snapshot.active_orders}=={r['order_id'] for r in rows}
            assert {key[3] for key in snapshot.yields}=={buy['order_id']}
            assert {oid for base in snapshot.baselines.values() for oid in base.anchor_order_ids}<={buy['order_id']}
            assert {r['instance_id'] for r in live.store.lp_order_provenance(WALLET,'polymarket')}=={buy['instance_id']}
            live.request_running(True);settle_control(live);live.run_once()
            assert sdk.cancels==[(buy['order_id'],)]
            result=live.manual_cancel(order_ids=[row['order_id'] for row in rows])
            assert set(result['canceled'])<={buy['order_id'],sell['order_id']}
            assert sell['order_id'] in result['canceled']
            assert {oid for batch in sdk.cancels for oid in batch}<={buy['order_id'],sell['order_id']}
            assert live.store.lp_manual_command(WALLET,'manual-key')['response']['submit_status']=='unknown'
            assert sdk.posts==sdk.market_orders==[]
            assert live.status()['native_inflight']==0
    finally:
        for owner in owners:
            assert owner[1].close(timeout=3)
            owner[1].trading.close()


def _real_sdk_signing_adapter(monkeypatch):
    from eth_account import Account
    from polymarket import SecureClient
    from polymarket.models.clob import ApiKeyCreds
    from polymarket.clients import secure as secure_module
    account=Account.create()
    calls=[]
    class OfflineTransport:
        def __init__(self,**kwargs):pass
        def get_json(self,path,**kwargs):
            calls.append(path)
            if path=='/auth/api-keys':return {'apiKeys':['00000000-0000-0000-0000-000000000001']}
            if path=='/tick-size':return {'minimum_tick_size':'.01'}
            if path=='/neg-risk':return {'neg_risk':False}
            raise AssertionError('unexpected offline SDK read')
        def post_json(self,*args,**kwargs):raise AssertionError('signing fixture must never POST')
        def close(self):pass
    monkeypatch.setattr(secure_module,'SyncTransport',OfflineTransport)
    sdk=SecureClient.create(private_key='0x'+account.key.hex(),wallet=account.address,
        credentials=ApiKeyCreds(key='00000000-0000-0000-0000-000000000001',secret='b2ZmbGluZQ==',passphrase='offline'))
    adapter=PolymarketTradingClient(TradingConfig(account.address,account.address,lp_instance_id='tokyo'),sdk)
    return adapter,sdk,calls


def test_real_sdk_sell_preparation_uses_exact_normalized_quantity(monkeypatch):
    from eth_account.messages import encode_typed_data
    from eth_utils import keccak
    from polymarket import PRODUCTION
    from polymarket._internal.actions.orders.types import UnsignedOrder
    from polymarket._internal.actions.orders.typed_data import build_order_typed_data
    from open_trader.lp_order_ownership import verify_signed_order
    adapter,sdk,calls=_real_sdk_signing_adapter(monkeypatch)
    try:
        for protected in (False,True):
            prepare=(lambda q:adapter.lp_prepare_protected_sell(token_id='1234',quantity=q,min_price=Decimal('.29'))) if protected else (
                lambda q:adapter.lp_prepare_limit_order(token_id='1234',quantity=q,price=Decimal('.29'),side='SELL',post_only=True,expiration=None))
            prepared=prepare(Decimal('20.123456'))
            assert prepared.quantity==Decimal('20.12') and prepared.price==Decimal('.29')
            signed=prepared.signed_order
            assert signed.maker_amount==20120000 and signed.taker_amount==5834800
            assert signed.order_type==('FOK' if protected else 'GTC')
            assert signed.post_only is (not protected)
            unsigned=UnsignedOrder(chain_id=137,exchange_address=PRODUCTION.standard_exchange,
                maker=signed.maker,signer=signed.signer,token_id='1234',side='SELL',
                maker_amount=20120000,taker_amount=5834800,signature_type=signed.signature_type,
                salt=signed.salt,timestamp=signed.timestamp,metadata=signed.metadata,builder=signed.builder,
                expiration=0,order_type='FOK' if protected else 'GTC')
            encoded=encode_typed_data(full_message=build_order_typed_data(unsigned))
            assert prepared.order_id=='0x'+keccak(b'\x19'+encoded.version+encoded.header+encoded.body).hex()
            for wrong in (Decimal('20.123456'),Decimal('20.11')):
                with pytest.raises(ValueError,match='identity'):
                    verify_signed_order(signed,signer=adapter.config.signer_address,wallet=adapter.config.wallet_address,
                        environment=PRODUCTION,token_id='1234',side='SELL',quantity=wrong,price=Decimal('.29'),protected=protected)
            tiny=prepare(Decimal('.01'))
            assert tiny.quantity==Decimal('.01')
            assert tiny.signed_order.maker_amount==10000 and tiny.signed_order.taker_amount==2900
            with pytest.raises(ValueError):prepare(Decimal('.009'))
        assert '/tick-size' in calls and '/neg-risk' in calls
    finally:adapter.close()


def _real_sdk_exit_runtime(tmp_path,monkeypatch,*,mode='passive'):
    import hashlib
    from tests.test_lp_d2_manual_bridge import manual_runtime
    from tests.test_lp_order_registration_contract import TOKEN_ID,CONDITION_ID,_Notifier
    from open_trader.polymarket_lp import PolymarketLPService
    from open_trader.prediction_arbitrage_execution import PredictionExecutionService
    from open_trader.polymarket_lp_current_state import ValidatedStartAdmission
    _,previous,sdk,public,clock=manual_runtime(tmp_path,monkeypatch)
    assert previous.close(timeout=3)
    real,real_sdk,_=_real_sdk_signing_adapter(monkeypatch)
    adapter=previous.trading;adapter.config=real.config
    sdk.environment=real_sdk.environment
    sdk.create_limit_order=real_sdk.create_limit_order
    sdk.create_market_order=real_sdk.create_market_order
    token=str(int(TOKEN_ID,16));clock[0]=datetime.now(UTC)
    market=public.get_market;book=public.get_order_book;rewards=public.list_market_rewards
    def get_market(*,id):
        value=market(id=id)
        if id=='market-1':value=value.model_copy(update={'outcomes':value.outcomes.model_copy(
            update={'yes':value.outcomes.yes.model_copy(update={'token_id':token})})})
        return value
    def get_book(*,token_id):
        value=book(token_id=TOKEN_ID if token_id==token else token_id)
        return value.model_copy(update={'token_id':token_id})
    def current_rewards(**kwargs):
        return tuple(value.model_copy(update={'rewards_config':tuple(c.model_copy(update={
            'start_date':clock[0]-timedelta(days=1),'end_date':clock[0]+timedelta(days=1)})
            for c in value.rewards_config)}) for value in rewards(**kwargs))
    public.get_market=get_market;public.get_order_book=get_book;public.list_market_rewards=current_rewards
    if mode=='protected':public.bid=Decimal('.81');public.ask=Decimal('.83')
    store=previous.store
    settings_account=hashlib.sha256(adapter.config.wallet_address.casefold().encode()).hexdigest()
    store.lp_save_current_control(dict(account_id=settings_account,process_epoch='seed',request_id='paused',
        request_version=1,desired_running=False,requested_at=clock[0],actor=None))
    store.lp_save_price_history(CONDITION_ID,token,[],dict(state='known',amplitude=Decimal('.005'),
        checked_at=clock[0],valid_until=clock[0]+timedelta(hours=24)))
    lp=PolymarketLPService(store,adapter,clock=lambda:clock[0])
    live=LPD2LiveExecution(store,adapter,account_id=adapter.config.wallet_address,venue='polymarket',
        process_epoch='real-sdk-exit',policy=CalculationPolicy(None),utc_now=lambda:clock[0])
    live.initialize(ValidatedStartAdmission('real-sdk-exit','offline-start-permit'))
    execution=PredictionExecutionService(store=store,monitor=object(),trading=adapter,notifier=_Notifier(),
        lock_path=tmp_path/'real-execution.lock',lp=lp,lp_d2=live)
    execution._breaker_open=False
    posts=[]
    def post(signed):
        rows=store.lp_order_provenance(adapter.config.wallet_address,'polymarket')
        row,=(r for r in rows if r['protocol_fields']['salt']==signed.salt)
        attempt=next(iter(live.state.snapshot().sells.values()),None)
        attempt=(attempt.passive if signed.order_type=='GTC' else attempt.protected) if attempt and signed.side=='SELL' else None
        posts.append(dict(order_id=row['order_id'],side=signed.side,order_type=signed.order_type,
            quantity=Decimal(row['quantity']),price=Decimal(row['price']),maker_amount=signed.maker_amount,
            taker_amount=signed.taker_amount,request_id=row['request_id'],
            request_quantity=attempt.submit_quantity.value if attempt else None,
            residual=attempt.submit_residual_quantity.value if attempt else None))
        sdk.orders+=(_order_from_provenance(row).model_copy(update={'owner':adapter.config.wallet_address,
            'maker_address':adapter.config.wallet_address,'created_at':clock[0]}),)
        return dict(accepted=True,status='LIVE',order_id=row['order_id'],making_amount='0',taking_amount='0')
    sdk.orders=();sdk.post_order=post
    return execution,live,sdk,public,clock,token,posts,real


def _set_real_sdk_exit_fills(live,sdk,clock,token,*,bought,sold='0',buy_price='.29'):
    from tests.test_lp_order_registration_contract import CONDITION_ID,_maker_order,_trade
    clock[0]+=timedelta(seconds=1)
    wallet=live.account_id
    rows=live.store.lp_order_provenance(wallet,'polymarket')
    buys=[r for r in rows if r['side']=='BUY'];sells=[r for r in rows if r['side']=='SELL']
    updated=[];trades=[]
    for row,quantity in [(r,bought) for r in buys]+[(r,sold) for r in sells]:
        side=row['side'];price=buy_price if side=='BUY' else row['price']
        current=next(o for o in sdk.orders if o.id==row['order_id'])
        updated.append(current.model_copy(update={'size_matched':Decimal(quantity),'status':'CANCELED' if side=='BUY' else 'LIVE' if Decimal(quantity)==0 else 'MATCHED'}))
        if Decimal(quantity):
            maker=_maker_order(row['order_id'],side,quantity,price).model_copy(update={'maker_address':wallet,'owner':wallet,'token_id':token})
            trades.append(_trade('real-'+side,maker,size=quantity).model_copy(update={
                'token_id':token,'maker_address':wallet,'owner':wallet,'matched_at':clock[0],'updated_at':clock[0]}))
    sdk.orders=tuple(updated);sdk.trades=tuple(trades)
    sdk.positions=(dict(condition_id=CONDITION_ID,token_id=token,size=str(Decimal(bought)-Decimal(sold)),average_price=buy_price),)


def _observe_real_sdk_exit(live):
    for _ in range(3):
        live.run_once()
        for handle in live.run_protection_once():
            handle.result(timeout=3);handle._thread.join(3)
            assert handle.done() and not handle._thread.is_alive()


def test_fractional_local_exit_preserves_requested_quantity_and_residual(tmp_path,monkeypatch):
    from tests.test_lp_d2_manual_bridge import entry
    for mode in ('passive','protected'):
        execution,live,sdk,public,clock,token,posts,real=_real_sdk_exit_runtime(tmp_path/mode,monkeypatch,mode=mode)
        try:
            price='.80' if mode=='protected' else '.29'
            response=execution.lp_submit_entry(entry(clock,token_id=token,quantity='30',price=price))
            assert response['submit_status']=='accepted',response
            _set_real_sdk_exit_fills(live,sdk,clock,token,bought='20.123456',buy_price=price)
            public.bid=Decimal('.29') if mode=='protected' else Decimal('.28')
            public.ask=Decimal('.31') if mode=='protected' else Decimal('.29')
            _observe_real_sdk_exit(live)
            sell,=(r for r in posts if r['side']=='SELL')
            assert sell['quantity']==sell['request_quantity']==Decimal('20.12')
            assert sell['residual']==Decimal('20.123456')
            assert sell['maker_amount']==20120000 and sell['taker_amount']==(16096000 if mode=='protected' else 5834800)
            assert sell['price']==Decimal('.80' if mode=='protected' else '.29') and sell['order_type']=='GTC'
            snapshot=live.state.snapshot();state,=snapshot.sells.values()
            assert state.residual_quantity.value==Decimal('20.123456')
            attempt=state.passive
            assert attempt.request_id==sell['request_id'] and attempt.submit_quantity.value==Decimal('20.12')
            _set_real_sdk_exit_fills(live,sdk,clock,token,bought='20.123456',sold='20.12',buy_price=price)
            _observe_real_sdk_exit(live)
            snapshot=live.state.snapshot();state,=snapshot.sells.values();opening,=snapshot.openings.values()
            assert state.residual_quantity.state=='known' and state.residual_quantity.value==Decimal('.003456')
            assert snapshot.positions[0].quantity.value==Decimal('.003456') and opening.phase!='closed'
            assert len([r for r in posts if r['side']=='SELL'])==1
            assert len([r for r in live.store.lp_order_provenance(live.account_id,'polymarket') if r['side']=='SELL'])==1
            assert sdk.cancels==[] and live.status()['native_inflight']==0
        finally:
            assert live.close(timeout=3)
            live.trading.close();real.close()


def test_sell_dust_never_posts_or_closes_residual(tmp_path,monkeypatch):
    from tests.test_lp_d2_manual_bridge import entry
    for case in ('dust','protected_minimum'):
        mode='protected' if case=='protected_minimum' else 'passive'
        execution,live,sdk,public,clock,token,posts,real=_real_sdk_exit_runtime(tmp_path/case,monkeypatch,mode=mode)
        try:
            price='.80' if mode=='protected' else '.29'
            response=execution.lp_submit_entry(entry(clock,token_id=token,quantity='30',price=price))
            assert response['submit_status']=='accepted'
            residual='.009' if case=='dust' else '20.123456'
            _set_real_sdk_exit_fills(live,sdk,clock,token,bought=residual,buy_price=price)
            public.bid=Decimal('.29') if mode=='protected' else Decimal('.28')
            public.ask=Decimal('.31') if mode=='protected' else Decimal('.29')
            if case=='protected_minimum':
                original_book=public.get_order_book
                def shallow_book(**kwargs):
                    book=original_book(**kwargs)
                    return book.model_copy(update={'min_order_size':Decimal('20.123456'),
                        'bids':(book.bids[0].model_copy(update={'size':Decimal('20.123456')}),)})
                public.get_order_book=shallow_book
            _observe_real_sdk_exit(live)
            assert [r['side'] for r in posts]==['BUY']
            snapshot=live.state.snapshot();opening,=snapshot.openings.values();sell,=snapshot.sells.values()
            assert sell.residual_quantity.state=='known' and sell.residual_quantity.value==Decimal(residual)
            assert snapshot.positions[0].quantity.value==Decimal(residual) and opening.phase!='closed'
            assert sell.passive is None and sell.protected is None
            assert len(live.store.lp_order_provenance(live.account_id,'polymarket'))==1
            assert sdk.cancels==[] and live.status()['native_inflight']==0
        finally:
            assert live.close(timeout=3)
            live.trading.close();real.close()


def _instance_pool_budget_runtime(tmp_path,monkeypatch,*,foreign='0',role_variant='exact'):
    import hashlib
    from tests.test_lp_d2_integration import settle_control
    from tests.test_lp_order_registration_contract import TOKEN_ID,CONDITION_ID
    from open_trader.polymarket_lp_market_reader import SourceResult
    _,previous,sdk,public,clock=_isolated_manual_runtime(tmp_path,monkeypatch)
    tokens=tuple(str(100+i) for i in range(1,5))
    conditions=tuple('0x'+format(i,'064x') for i in range(1,5))
    markets=tuple('pool-'+str(i) for i in range(1,5))
    original_market=public.get_market;original_book=public.get_order_book;original_rewards=public.list_market_rewards
    def market(*,id):
        index=markets.index(id);value=original_market(id='market-1')
        return value.model_copy(update={'id':id,'condition_id':conditions[index],
            'outcomes':value.outcomes.model_copy(update={
                'yes':value.outcomes.yes.model_copy(update={'token_id':tokens[index]}),
                'no':value.outcomes.no.model_copy(update={'token_id':str(900+index)})})})
    def book(*,token_id):
        index=tokens.index(token_id);value=original_book(token_id=TOKEN_ID)
        return value.model_copy(update={'condition_id':conditions[index],'token_id':token_id})
    def rewards(*,condition_id,sponsored):
        return tuple(row.model_copy(update={'condition_id':condition_id}) for row in original_rewards(
            condition_id=CONDITION_ID,sponsored=sponsored))
    public.get_market=market;public.get_order_book=book;public.list_market_rewards=rewards
    public.list_markets=lambda **kwargs:[market(id=identity) for identity in markets]
    public.bid=Decimal('.31');public.ask=Decimal('.33')
    sdk.get_balance_allowance=lambda **kwargs:SimpleNamespace(balance='1000000000',allowances={sdk.environment.standard_exchange:'1000000000'})
    records=[]
    for index,quantity in ((0,'50'),(1,'200')):
        prepared=previous.trading.lp_prepare_limit_order(token_id=tokens[index],side='BUY',price=Decimal('.40'),
            quantity=Decimal(quantity),post_only=True,expiration=None)
        records.append(previous.store.lp_register_order_provenance(prepared.provenance(instance_id='tokyo',
            request_id='local-'+str(index),condition_id=conditions[index])))
    policy=CalculationPolicy(Decimal('100'),3,allocation_mode='observed_pools',trial_budget_usd='100',income_budget_usd='100')
    live=_restart(previous,clock,policy=policy)
    live.request_running(True);settle_control(live)
    live.run_once(SourceResult('empty-start',(),checked_at=clock[0]),pool='trial')
    sdk.orders=tuple(_order_from_provenance(row) for row in records)
    if Decimal(foreign):
        sdk.orders+=(_order_from_provenance(records[1],order_id='foreign-buy').model_copy(update={
            'condition_id':conditions[3],'token_id':tokens[3],'original_size':Decimal(foreign)/Decimal('.40')}),)
    roles={conditions[0]:dict(pool='trial',episode=records[0]['order_id']),
        conditions[1]:dict(pool='income',episode=records[1]['order_id'])}
    if role_variant=='stale':roles[conditions[1]]['episode']='stale-local-income'
    elif role_variant in ('unclassified','dashboard_label'):roles.pop(conditions[1])
    elif role_variant=='foreign_episode':roles[conditions[1]]['episode']='foreign-buy'
    elif role_variant=='wrong_terms':
        sdk.orders=(sdk.orders[0],sdk.orders[1].model_copy(update={'original_size':Decimal('201')}),*sdk.orders[2:])
    account=hashlib.sha256(WALLET.casefold().encode()).hexdigest()
    live.store.lp_save_observed_pool_roles(account,expected_revision=0,roles=roles)
    if role_variant in ('foreign_episode','shared_condition'):
        sdk.orders=tuple(row.model_copy(update={'condition_id':conditions[1],'token_id':tokens[1]}) if row.id=='foreign-buy' else row for row in sdk.orders)
    if role_variant=='dashboard_label':
        sdk.orders=(sdk.orders[0],{**sdk.orders[1].model_dump(mode='python'),
            'pool_affiliation':dict(state='income',order_id=records[1]['order_id'])},*sdk.orders[2:])
    candidate_book=live.trading.lp_order_books((tokens[2],))[tokens[2]]
    captured=SourceResult('new-trial-30',(
        dict(condition_id=conditions[2],market_id=markets[2],token_id=tokens[2],venue='polymarket',
            price=Decimal('.30'),quantity=Decimal('100'),order_type='GTC',expiration=None,post_only=True,send_options=(),
            direction=dict(market=dict(reward_min_size=Decimal('1'),reward_max_spread=Decimal('.10'),minimum_order_size=Decimal('1')),
                book=candidate_book,daily_pool_usd=Decimal('24'))),),checked_at=clock[0])
    posts=[]
    def post(signed):
        row,=(r for r in live.store.lp_order_provenance(WALLET,'polymarket') if r['protocol_fields']['salt']==signed.salt)
        posts.append(dict(order_id=row['order_id'],token_id=row['token_id'],quantity=Decimal(row['quantity']),price=Decimal(row['price'])))
        return dict(accepted=True,status='LIVE',order_id=row['order_id'],taking_amount='0')
    sdk.post_order=post
    return live,sdk,clock,captured,records,posts,tokens,conditions


def test_instance_observed_pools_keep_wallet_and_pool_budget_separate(tmp_path,monkeypatch):
    for foreign,reserve,capacity,trial_headroom in (('0','100','100','80'),('40','140','60','40'),('60','160','40','20')):
        live,sdk,clock,candidates,records,posts,tokens,conditions=_instance_pool_budget_runtime(tmp_path/foreign,monkeypatch,foreign=foreign)
        try:
            result=live.run_once(candidates,pool='trial')
            financial=result.acceptance.observation.calculation
            assert financial.buy_reserved==Decimal(reserve) and financial.new_capacity==Decimal(capacity)
            assert result.acceptance.observation.source_frame.buy_reserved==Decimal(reserve)
            assert {o.key[3] for o in live.state.snapshot().active_orders}>={row['order_id'] for row in records}
            assert ((conditions[2],tokens[2]) in result.selection.new_target_keys)==(foreign!='60')
            if foreign=='60':assert posts==[] and result.outcomes==()
            else:
                assert len(posts)==1 and posts[0]['token_id']==tokens[2]
                assert posts[0]['price']*posts[0]['quantity']==Decimal('30')<=Decimal(trial_headroom)
                assert len(result.outcomes)==1 and result.outcomes[0].status.value=='accepted'
            result=live.dispatch_rotation(result)
            for outcome in result.rotation_outcomes:
                if outcome.handle:
                    outcome.handle.result(timeout=3);outcome.handle._thread.join(3)
            assert sdk.cancels==[]
            assert set(result.selection.retained_order_ids)>={records[0]['order_id']}
            assert live.status()['native_inflight']==0
        finally:
            assert live.close(timeout=3)
            live.trading.close()


def test_instance_pool_budget_exemption_requires_exact_local_income(tmp_path,monkeypatch):
    for variant in ('stale','unclassified','dashboard_label','foreign_episode','shared_condition','wrong_terms'):
        foreign='60' if variant=='shared_condition' else '40'
        live,sdk,clock,candidates,records,posts,tokens,conditions=_instance_pool_budget_runtime(
            tmp_path/variant,monkeypatch,foreign=foreign,role_variant=variant)
        try:
            result=live.run_once(candidates,pool='trial')
            financial=result.acceptance.observation.calculation
            assert financial.buy_reserved is not None and financial.new_capacity is not None
            assert (conditions[2],tokens[2]) not in result.selection.new_target_keys
            assert posts==[] and result.outcomes==()
            dispatched=live.dispatch_rotation(result)
            for outcome in dispatched.rotation_outcomes:
                if outcome.handle:outcome.handle.result(timeout=3);outcome.handle._thread.join(3)
            assert not any('foreign-buy' in batch for batch in sdk.cancels)
            assert len(live.store.lp_order_provenance(WALLET,'polymarket'))==2
        finally:
            assert live.close(timeout=3);live.trading.close()
    for fault in ('balance','orders','positions','terms'):
        live,sdk,clock,candidates,records,posts,tokens,conditions=_instance_pool_budget_runtime(
            tmp_path/fault,monkeypatch,foreign='40')
        try:
            if fault=='balance':sdk.get_balance_allowance=lambda **kwargs:None
            elif fault=='orders':sdk.orders=None
            elif fault=='positions':sdk.positions=None
            else:sdk.orders=(sdk.orders[0],sdk.orders[1].model_copy(update={'original_size':None}),*sdk.orders[2:])
            result=live.run_once(candidates,pool='trial')
            assert live.state.snapshot().new_capacity.state=='unknown'
            assert posts==[] and result.outcomes==()
            assert not any('foreign-buy' in batch for batch in sdk.cancels)
            assert len(live.store.lp_order_provenance(WALLET,'polymarket'))==2
        finally:
            assert live.close(timeout=3);live.trading.close()
