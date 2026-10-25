"""Existing candidate authority after financial recovery; all SDK edges offline."""
from datetime import timedelta
from decimal import Decimal
import time
import threading

from test_lp_async_source_contract import independent_watchdog
from tests.test_lp_candidate_current_authority import current_runtime, produce, expected_candidate
from tests.test_lp_d2_integration import settle_control


def test_candidate_admission_requires_fresh_financial_recovery(tmp_path, monkeypatch):
    with current_runtime(tmp_path, monkeypatch, target=3) as (
            store, producer, live, execution, sdk, clock):
        # Configure the existing two-pool contract without creating candidates
        # or orders. Settings/control are exercised through their public seams.
        execution.lp_auto_set_desired_running(False)
        settle_control(live)
        execution.lp_auto_configure(dict(income_budget_usd='100', expected_config_version=1))
        end = time.monotonic() + 3
        while live.settings_status()['pending']:
            assert time.monotonic() < end, 'offline settings completion watchdog'
            threading.Event().wait(.001)
        settle_control(live)
        state = execution.lp_auto_state()
        assert state['allocation_mode'] == 'observed_pools'
        assert state['trial_target_buy_count'] == state['income_target_buy_count'] == 3
        sdk.positions = None
        clock[0] += timedelta(seconds=31)
        execution.lp_auto_scheduled_check()
        assert live.state.snapshot().new_capacity.state == 'unknown'
        unknown = produce(producer)
        assert not producer.capture_qualified_pool().rows
        assert unknown['candidate_valid_count'] == 0
        assert live.candidate_current_reservations()[0]['amount'] is None
        assert sdk.posts == sdk.cancels == sdk.market_orders == []
        old = live.state.snapshot().observation
        sdk.positions = ()
        clock[0] += timedelta(seconds=31)
        execution.lp_auto_scheduled_check()
        fresh = live.state.snapshot()
        assert fresh.new_capacity.state == 'known'
        produce(producer)
        expected_candidate(producer)  # literal token, price .40, quantity 20
        assert Decimal(producer.candidate_snapshot()['funnel']['budget']['available_capital']) == 100
        assert live.candidate_current_reservations() == ()
        # Replay the sealed old observation through the public acceptance seam.
        accepted = live.state.accept(old.read_ticket, old.source_frame, old.calculation,
            evaluated_at=clock[0])
        assert accepted.status == 'stale'
        assert live.state.snapshot().observation.read_ticket == fresh.observation.read_ticket
        assert live.state.snapshot().new_capacity.state == 'known'
        assert sdk.posts == sdk.cancels == sdk.market_orders == []
