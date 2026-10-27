"""Fixed, externally specified bid-level cancellation through the real D2 owner."""
from decimal import Decimal as D

import pytest

from tests.test_lp_d2_integration import _d2_protection_runtime


def observe(live, clock):
    clock[0] += 1
    live.run_once()
    attempts = live.run_protection_once()
    for attempt in attempts:
        assert attempt.result(timeout=3).status.value == 'accepted'
        attempt._thread.join(timeout=2)
        assert attempt.done()
    return live.state.snapshot()


def test_buy_two_upper_price_change_cancels(tmp_path, monkeypatch):
    live, sdk, public, clock = _d2_protection_runtime(tmp_path, monkeypatch,
        quantity='20', upper_bids=(('.31','100'),), buy_level=2)
    try:
        observe(live, clock)
        assert sdk.cancels == []
        public.extra_bids = (('.315','100'),)
        clock[0] += 1
        live.run_once()
        reads = (public.book_reads, sdk.order_reads)
        attempts = live.run_protection_once()
        for attempt in attempts:
            assert attempt.result(timeout=3).status.value == 'accepted'
            attempt._thread.join(timeout=2)
        assert sdk.cancels == [('entry-A',)]
        targets = [t for p in live.state.snapshot().progress.values() for t in p.cancel_targets]
        assert len(targets) == 1 and targets[0].reason == 'bid_level_changed'
        live.run_protection_once()
        assert sdk.cancels == [('entry-A',)]
        assert reads == (public.book_reads, sdk.order_reads)
        assert len(sdk.posts) == 1
    finally:
        assert live.close(timeout=3)
        live.trading.close()


@pytest.mark.parametrize('upper,depth,ask,cancels', [
    ((), '100', '.32', True),
    ((('.315','50'),('.31','100')), '100', '.32', True),
    ((('.31','99'),), '100', '.32', True),
    ((('.31','101'),), '100', '.32', True),
    ((('.31','100'),), '99', '.32', True),
    ((('.31','100'),), '101', '.32', True),
    ((('.31','100'),('.29','999')), '100', '.32', False),
    ((('.31','100'),), '100', '.33', False),
], ids=['upper-disappears','insert-above','upper-down','upper-up','own-down','own-up','lower-only','asks-only'])
def test_buy_two_depth_changes_cancel(tmp_path, monkeypatch, upper, depth, ask, cancels):
    live,sdk,public,clock = _d2_protection_runtime(tmp_path,monkeypatch,
        quantity='20',upper_bids=(('.31','100'),),buy_level=2,depth='100')
    try:
        first=observe(live,clock)
        assert sdk.cancels == []
        baseline=next(iter(first.baselines.values()))
        public.extra_bids,public.level,public.ask=upper,D(depth),D(ask)
        observe(live,clock)
        assert sdk.cancels == ([('entry-A',)] if cancels else [])
        assert live.state.snapshot().baselines[baseline.key] is baseline
    finally:
        assert live.close(timeout=3)
        live.trading.close()


@pytest.mark.parametrize('depth,cancels',[('99',True),('101',True),('100',False)])
def test_buy_one_quantity_change_cancels(tmp_path,monkeypatch,depth,cancels):
    live,sdk,public,clock=_d2_protection_runtime(tmp_path,monkeypatch,quantity='20',depth='100')
    try:
        observe(live,clock)
        assert sdk.cancels==[]
        public.level=D(depth)
        observe(live,clock)
        assert sdk.cancels==([('entry-A',)] if cancels else [])
    finally:
        assert live.close(timeout=3)
        live.trading.close()


@pytest.mark.parametrize('origin',['accepted','unknown-owner'])
@pytest.mark.parametrize('change',['price','quantity'])
def test_web_manual_first_baseline_changes_cancel(tmp_path,monkeypatch,origin,change):
    from polymarket.models.clob.order_book import OrderBookLevel
    from tests.test_lp_d2_manual_bridge import manual_runtime,entry
    from tests.test_lp_order_registration_contract import _open_order
    execution,live,sdk,public,clock=manual_runtime(tmp_path,monkeypatch)
    public.lower_bids=(OrderBookLevel(price=D('.31'),size=D('100')),)
    try:
        if origin=='accepted':
            result=execution.lp_submit_entry(entry(clock,price='.30'))
            assert result['state']=='entry_open'
            oid=result['entry_order_id']
        else:
            oid='recovered-manual'
            sdk.orders=(_open_order(oid,'BUY',price='.30',original='20'),)
        live.run_once();live.run_protection_once()
        assert sdk.cancels==[]
        baseline=next(iter(live.state.snapshot().baselines.values()))
        assert baseline.bid_level==2
        live.run_once();live.run_protection_once()
        assert sdk.cancels==[] and live.state.snapshot().baselines[baseline.key] is baseline
        public.lower_bids=(OrderBookLevel(price=D('.315' if change=='price' else '.31'),
            size=D('100' if change=='price' else '101')),)
        live.run_once()
        for attempt in live.run_protection_once():
            assert attempt.result(timeout=3).status.value=='accepted'
            attempt._thread.join(timeout=2)
        assert sdk.cancels==[(oid,)]
        assert live.state.snapshot().baselines[baseline.key] is baseline
        live.run_protection_once()
        assert sdk.cancels==[(oid,)]
    finally:
        assert live.close(timeout=3)
        live.trading.close()


@pytest.mark.parametrize('kind',['failed','own-price-missing'])
def test_unreadable_book_nine_ten_and_recovery(tmp_path,monkeypatch,kind):
    from open_trader.polymarket_lp_risk import evaluate_lp_bid_levels
    from datetime import UTC,datetime
    for book in (None,dict(received_at=datetime.now(UTC),condition_id='wrong',token_id='T',
            bids=[dict(price='.30',size='100')]),
            dict(received_at=datetime.now(UTC),condition_id='C',token_id='T',
            bids=[dict(price='.30',size='100'),dict(price='.31',size='bad')])):
        assert evaluate_lp_bid_levels(book,price=D('.30'),own_remaining=D('20'),
            bid_level=1,condition_id='C',token_id='T')['state']=='unknown'
    live,sdk,public,clock=_d2_protection_runtime(tmp_path,monkeypatch,quantity='20')
    def failures():
        return next(iter(live.state.snapshot().failure_states.values())).data_failures
    def unreadable(value):
        if kind=='failed':public.failed=value
        else:public.bid=D('.29' if value else '.30')
    try:
        observe(live,clock)
        unreadable(True)
        for i in range(1,4):
            observe(live,clock)
            assert failures()==i and sdk.cancels==[]
        unreadable(False)
        observe(live,clock)
        assert failures()==0 and sdk.cancels==[]
        unreadable(True)
        for i in range(1,10):
            observe(live,clock)
            assert failures()==i and sdk.cancels==[]
            live.run_protection_once()
            assert failures()==i and sdk.cancels==[]
        observe(live,clock)
        assert failures()==10 and sdk.cancels==[('entry-A',)]
        targets=[t for p in live.state.snapshot().progress.values() for t in p.cancel_targets]
        assert targets[0].reason=='queue_data_failure_limit'
        live.run_protection_once()
        assert sdk.cancels==[('entry-A',)]
    finally:
        assert live.close(timeout=3)
        live.trading.close()


@pytest.mark.parametrize('case',['frozen-submit','missing-submit','wrong-recovery','trial','income','unclassified'])
def test_baseline_keeps_submit_level_and_recovers_once(tmp_path,monkeypatch,case):
    import hashlib
    from tests.test_lp_order_registration_contract import _open_order,CONDITION_ID
    submitted=case in ('frozen-submit','missing-submit','wrong-recovery')
    live,sdk,public,clock=_d2_protection_runtime(tmp_path,monkeypatch,quantity='20',
        upper_bids=(('.31','100'),),buy_level=2,submit=submitted,
        submit_snapshot_missing=case in ('missing-submit','wrong-recovery'))
    try:
        account=hashlib.sha256(live.account_id.strip().casefold().encode()).hexdigest()
        if not submitted:
            sdk.orders=(_open_order('restarted','BUY',price='.30',original='20'),)
            # Durable ownership is an exact order episode, not a label alone.
            role=dict(pool=case if case in ('trial','income') else 'trial',
                episode='restarted' if case!='unclassified' else 'some-other-order')
            saved=live.store.lp_load_observed_pool_roles(account)
            live.store.lp_save_observed_pool_roles(account,expected_revision=saved['revision'],
                roles={CONDITION_ID:role})
            live.store.lp_save_current_settings(account,expected_config_version=0,
                changes=dict(budget_usd='10000',target_buy_count=3,buy_price_level=1,
                    allocation_mode='observed_pools',trial_budget_usd='10000',income_budget_usd='0'))
            live.load_settings()
        if case=='frozen-submit':
            original=next(iter(live.state.snapshot().baselines.values()))
            assert original.bid_level==2
            live.store.lp_save_current_settings(account,expected_config_version=0,
                changes=dict(budget_usd='10000',target_buy_count=1,buy_price_level=1))
            live.load_settings()
        elif case in ('missing-submit','wrong-recovery'):
            original=next(iter(live.state.snapshot().baselines.values()))
            assert original.bid_level==2 and original.bid_levels==()
            if case=='wrong-recovery':public.extra_bids=()
        observe(live,clock)
        baseline=next(iter(live.state.snapshot().baselines.values()))
        if case in ('trial','income','wrong-recovery'):
            assert sdk.cancels==[('entry-A' if submitted else 'restarted',)]
        else:
            assert sdk.cancels==[]
            assert baseline.bid_level==2
            if case=='missing-submit':assert baseline.bid_levels==((D('.31'),D('100')),(D('.30'),D('10000')))
            if case=='frozen-submit':assert baseline is original
            observe(live,clock)
            assert live.state.snapshot().baselines[baseline.key] is baseline
            public.extra_bids=(('.31','101'),)
            observe(live,clock)
            assert sdk.cancels==[('entry-A' if submitted else 'restarted',)]
    finally:
        assert live.close(timeout=3)
        live.trading.close()


def test_buy_three_setting_remains_rejected(tmp_path,monkeypatch):
    import time
    from tests.test_lp_d2_manual_bridge import manual_runtime
    execution,live,sdk,public,clock=manual_runtime(tmp_path,monkeypatch)
    def saved():
        deadline=time.monotonic()+3
        while live.settings_status()['pending'] and time.monotonic()<deadline:
            time.sleep(.001)
        assert not live.settings_status()['pending']
        return live.settings_status()['effective']
    try:
        live.load_settings()
        live.run_once()
        for level in (1,2):
            live.configure(dict(budget_usd='100',target_buy_count=1,buy_price_level=level))
            assert saved().buy_price_level==level
            live.run_once()
        before=live.settings_status()
        with pytest.raises(ValueError):
            live.configure(dict(budget_usd='200',target_buy_count=2,buy_price_level=3))
        assert live.settings_status()==before
        assert sdk.posts==[] and sdk.cancels==[]
    finally:
        assert live.close(timeout=3)
        live.trading.close()


@pytest.mark.parametrize('origin',['auto','manual'])
@pytest.mark.parametrize('external',['100','10'],ids=['external-ge-own','external-lt-own'])
def test_pre_acceptance_book_does_not_cancel_new_buy(tmp_path,monkeypatch,origin,external):
    from datetime import timedelta
    from polymarket.models.clob.order_book import OrderBookLevel
    from tests.test_lp_d2_manual_bridge import manual_runtime,entry
    from tests.test_lp_order_registration_contract import TOKEN_ID
    if origin=='auto':
        live,sdk,public,clock=_d2_protection_runtime(tmp_path,monkeypatch,
            quantity='20',submit=False,depth=external)
        post=sdk.post_order
        def accept(signed):
            clock[0]+=2
            return post(signed)
        sdk.post_order=accept
        book=live.trading.lp_order_books((TOKEN_ID,))[TOKEN_ID]
        from open_trader.polymarket_lp_market_reader import SourceResult
        from tests.test_lp_order_registration_contract import CONDITION_ID
        result=live.run_once(SourceResult('accepted-auto-fixture',(
            dict(condition_id=CONDITION_ID,market_id='market-1',token_id=TOKEN_ID,
                venue='polymarket',price=D('.30'),quantity=D('20'),order_type='GTC',
                expiration=None,post_only=True,send_options=(),direction=dict(
                    market=dict(reward_min_size=D('1'),reward_max_spread=D('.10')),
                    book=book,daily_pool_usd=D('24'))),),checked_at=book['received_at']))
        assert [o.status.value for o in result.outcomes]==['accepted']
        oid='entry-A'
        before=next(iter(live.state.snapshot().baselines.values()))
        acceptance=live.state.snapshot().orders[0].source.checked_at
        def tick():clock[0]+=.01
    else:
        execution,live,sdk,public,clock=manual_runtime(tmp_path,monkeypatch)
        public.lower_bids=(OrderBookLevel(price=D('.29'),size=D('1000')),)
        post=sdk.post_order
        def accept(signed):
            clock[0]+=timedelta(seconds=2)
            return post(signed)
        sdk.post_order=accept
        result=execution.lp_submit_entry(entry(clock,price='.30'))
        assert result['state']=='entry_open'
        oid=result['entry_order_id'];before=None
        acceptance=live.state.snapshot().orders[0].source.checked_at
        def tick():clock[0]+=timedelta(milliseconds=10)
    get_book=public.get_order_book
    valid=[False];moved=[False]
    def observed_book(*,token_id):
        original=get_book(token_id=token_id)
        total=D(external)+(D('20') if valid[0] else D('0'))+(D('1') if moved[0] else D('0'))
        return original.model_copy(update=dict(timestamp=acceptance+(timedelta(milliseconds=1) if valid[0] else -timedelta(seconds=1)),
            bids=(OrderBookLevel(price=D('.30'),size=total),
                  OrderBookLevel(price=D('.29'),size=D('1000')))))
    public.get_order_book=observed_book
    try:
        for _ in range(11):
            tick();live.run_once();live.run_protection_once()
            snapshot=live.state.snapshot()
            assert sdk.cancels==[]
            assert all(f.data_failures==0 for f in snapshot.failure_states.values())
            if before is None:assert not snapshot.baselines
            else:assert snapshot.baselines[before.key] is before
        valid[0]=True
        tick();live.run_once();live.run_protection_once()
        baseline=next(iter(live.state.snapshot().baselines.values()))
        assert baseline.bid_levels==((D('.30'),D(external)),)
        assert before is None or baseline is before
        assert sdk.cancels==[]
        if origin=='manual':
            duplicate=execution.lp_submit_augment(dict(session_id=result['session_id'],
                price='.30',quantity='20',idempotency_key='same-price-new-local'))
            assert duplicate==dict(state='rejected',reason='price_level_active')
            assert len(sdk.posts)==1 and live.state.snapshot().baselines[baseline.key] is baseline
        moved[0]=True
        tick();live.run_once()
        for handle in live.run_protection_once():
            assert handle.result(timeout=3).status.value=='accepted'
            handle._thread.join(timeout=2)
        assert sdk.cancels==[(oid,)]
        targets=[t for p in live.state.snapshot().progress.values() for t in p.cancel_targets]
        assert len(targets)==1 and targets[0].reason=='bid_level_changed'
        live.run_protection_once()
        assert sdk.cancels==[(oid,)]
    finally:
        assert live.close(timeout=3)
        live.trading.close()
