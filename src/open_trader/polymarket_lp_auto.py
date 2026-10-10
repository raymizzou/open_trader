"""One bounded automatic LP round, using the existing session execution lane.

The durable document is the sole automatic attribution/allocation ledger. Venue
trade economics stay in LP sessions; projections never treat wallet cash or
estimated rewards as strategy profit.
"""
from __future__ import annotations

import fcntl
import json
import logging
import hashlib
import uuid
import threading
from time import monotonic
from contextlib import contextmanager, nullcontext
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from decimal import Decimal, ROUND_CEILING
from pathlib import Path
from typing import Mapping

from .polymarket_lp_accounting import (account_position_quantity,
    default_account_pool_document, has_independent_unresolved_action,
    has_independent_unresolved_buy_action, reservation_is_covered,
    reservation_is_manually_released, reservation_is_released,
    submission_action_kind)
from .polymarket_lp_risk import (
    TERMINAL_ORDER_STATES, _account_after_reservations, _decimal as _money, _freshness, _levels,
    _maybe_decimal, _select_bid_level, _timestamp, evaluate_lp_entry, minimum_order_estimate,
)
from .daily_premarket import _notifier_channel, send_notification_with_results
from .notifications import CompositeNotifier, notification_delivery_episode
from .polymarket_lp_notification_batches import (
    ChannelDeliveryResult, matching_batch_channels, plan_notification_batches,
)

from .polymarket_trading import _lp_capture_read_log, _lp_causal_event, _lp_read_task

logger = logging.getLogger(__name__)
TIMING_DEFAULTS = dict(round_interval_seconds=60, api_retry_interval_seconds=60,
                       order_check_interval_seconds=10)
_PLAN_TRANSIENT_WAITS = {'execution_lock', 'market_read_capacity', 'market_read_timeout',
                         'market_read_in_progress', 'market_read_cooling_down', 'target_filled'}

ZERO = Decimal('0')


def _candidate_refresh_priority(cached, *, bid_level, now):
    """Cached arithmetic orders reads only; it never qualifies a BUY."""
    best = None
    for direction in cached.get('directions', ()):
        try:
            market = direction['market']
            minimum = _maybe_decimal(market.get('minimum_order_size'))
            reward_minimum = _maybe_decimal(market.get('reward_min_size'))
            if minimum is None or reward_minimum is None or min(minimum, reward_minimum) <= ZERO:
                continue
            price, _ = _select_bid_level(_levels(direction['book'].get('bids'), 'bids'), bid_level)
            quantity = (max(minimum, reward_minimum) / Decimal('.01')).to_integral_value(
                rounding=ROUND_CEILING) * Decimal('.01')
            estimate = minimum_order_estimate(direction, {'price': price, 'quantity': quantity}, now)
            hint = _maybe_decimal(estimate.get('yield_pct_per_hour')) if estimate.get('state') == 'known' else None
            if hint is not None:
                best = hint if best is None else max(best, hint)
        except (KeyError, TypeError, ValueError, ArithmeticError, AttributeError):
            continue
    return best


# Fixed codes only; arbitrary external reasons never enter round diagnostics.
_CANDIDATE_FILTER_REASONS = frozenset('''candidate_pool_expired qualification_facts_missing participating_market
screen_time_unknown market_facts_unknown market_identity_unknown market_metadata_time_unknown market_metadata_stale
market_fees_time_unknown market_fees_stale reward_time_unknown reward_data_stale reward_deadline_unknown reward_expired
reward_inactive reward_status_unknown reward_pool_unknown reward_pool_empty market_not_accepting_orders market_status_unknown
book_identity_mismatch book_freshness_stale book_freshness_unknown account_freshness_stale account_freshness_unknown
account_facts_unknown book_invalid book_crossed market_rules_unknown market_rules_invalid price_off_tick midpoint_unknown
midpoint_out_of_range reward_distance_invalid reward_score_zero account_auth_unknown account_identity_unknown
account_identity_mismatch market_already_participating account_funds_unknown balance_insufficient exit_liquidity_insufficient
exit_fee_unknown stress_loss_threshold book_unknown estimate_time_unknown entry_terms_invalid competition_upper_bound_nonpositive
ranking_changed ranking_yield_unknown history_summary_unknown history_summary_expired history_amplitude_exceeded
history_identity_mismatch event_in_progress event_recovery_pending event_start_time_unknown event_end_time_unknown
event_end_time_in_future post_event_screening_unknown market_read_capacity market_read_timeout market_read_cooling_down
market_read_in_progress event_timing_unknown event_starting_soon event_status_unknown
book_freshness_invalid account_freshness_invalid stress_loss_exceeded history_latest_refresh_failed
history_time_unknown history_amplitude_unknown ranking_freshness_invalid ranking_freshness_stale
bid_level_invalid second_bid_insufficient candidate_bid_level_changed'''.split())


def _candidate_filter_reasons(diagnostics, codes, *, field='reasons'):
    if diagnostics is None:
        return
    reasons = diagnostics[field]
    for code in codes[:32]:
        code = code if type(code) is str and code in _CANDIDATE_FILTER_REASONS else 'other'
        if code not in reasons and len(reasons) >= 31:
            code = 'other'
        reasons[code] = reasons.get(code, 0) + 1


def _candidate_filter_range(diagnostics, name, value, *, generation=False):
    if diagnostics is None:
        return
    bucket = diagnostics['facts'].setdefault(name, dict(min=None, max=None, unknown=0))
    if generation:
        value = value if type(value) is int and 0 <= value <= 2**63 - 1 else None
    else:
        stamp = _maybe_datetime(value)
        value = stamp.astimezone(UTC).isoformat() if stamp is not None else None
    if value is None:
        bucket['unknown'] += 1
    else:
        bucket['min'] = value if bucket['min'] is None else min(bucket['min'], value)
        bucket['max'] = value if bucket['max'] is None else max(bucket['max'], value)


class _AccountRoundLease:
    """Keep one round alive until its caller and every launched job release it."""

    def __init__(self, pool: "LPAutoPool", token: object) -> None:
        self.pool = pool
        self.token = token
        self.lock = threading.Lock()
        self.refs = 1
        self.closed = False

    def retain(self) -> None:
        with self.lock:
            if self.closed:
                raise RuntimeError("lp_account_round_ended")
            self.refs += 1

    def release(self) -> None:
        close = False
        with self.lock:
            self.refs -= 1
            if self.refs == 0:
                self.closed = True
                close = True
        if close:
            self.pool._end_account_round(self.token)


def _decimal(value, name='money'):
    return _money(value, name)


def _json(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    raise TypeError(type(value).__name__)


def _maybe_datetime(value: object) -> datetime | None:
    try:
        return _timestamp(value)
    except (TypeError, ValueError):
        return None


def _reconcile_retry_plan(
    error: object,
    now: datetime,
    previous: object = None,
    scheduled_at: datetime | None = None,
) -> tuple[datetime, str]:
    """Return the real future deadline and its source; never extend it to 60s."""
    candidates: list[tuple[datetime, str]] = []
    previous_at = _maybe_datetime(previous)
    if previous_at is not None and previous_at > now:
        candidates.append((previous_at, "existing_plan"))
    scheduled = _maybe_datetime(scheduled_at)
    if scheduled is not None and scheduled > now:
        # Provider rate limits and scheduler deadlines are lower bounds; a
        # normal 30-second plan must still display and execute at 30 seconds.
        candidates.append((scheduled, str(error or "scheduled")))
    if candidates:
        return max(candidates, key=lambda item: item[0])
    return now + timedelta(seconds=60), "fallback_minute"


class LPAutoPool:
    def __init__(self, execution):
        self.execution = execution
        self.lp = execution._lp
        self.store = execution._store
        self.send_path = Path(str(self.store.path) + '.lp-auto-send.lock')
        self._reconcile_jobs = {}
        self._reconcile_jobs_lock = threading.Lock()
        self._attention_delivery_lock = threading.Lock()
        self._attention_delivery_results = {}
        self._account_refresh_lock = threading.RLock()
        self._account_refresh_attempt = 0
        self._account_facts_wait = None
        self._current_account = None
        self.lp._facts_attention_summary = self.state
        self.lp._facts_attention_verifier = self.reconcile_attention

    def _begin_account_round(self) -> _AccountRoundLease | None:
        begin = getattr(self.lp.exchange, 'lp_account_round_begin', None)
        generation = getattr(self.store, 'lp_trade_generation', None)
        if not callable(begin) or not callable(generation):
            return None
        token = begin(generation)
        self.lp.track_lp_account_round(token)
        return _AccountRoundLease(self, token)

    def _end_account_round(self, token):
        if token is None:
            return
        self.lp.untrack_lp_account_round(token)
        end = getattr(self.lp.exchange, 'lp_account_round_end', None)
        if callable(end):
            end(token)

    def _now(self):
        return self.lp._now()

    def _stamp(self):
        return self._now().isoformat()

    def _default(self):
        return default_account_pool_document(self.store.path, self.execution._lp_account_id(), self._now())

    def _read(self):
        with self.store._read_connection() as c:
            row=c.execute('SELECT payload FROM lp_auto_pool WHERE singleton=1').fetchone()
            document = json.loads(row[0]) if row else self._default()
            document.setdefault('buy_price_level', 1)
            document.setdefault('trading_config_version', document['config_version'])
            for key, value in TIMING_DEFAULTS.items():
                document.setdefault(key, value)
            return document

    def buy_price_level(self):
        """Return the effective quote level, including legacy default one."""
        return self._read()['buy_price_level']

    def _update(self, fn, *, connection=None):
        # ponytail: one SQLite document serializes this single-account MVP;
        # split event rows if retained history makes document rewrites material.
        with (self.store._transaction() if connection is None else nullcontext(connection)) as c:
            row = c.execute('SELECT payload FROM lp_auto_pool WHERE singleton=1').fetchone()
            d = json.loads(row[0]) if row else self._default()
            d.setdefault('buy_price_level', 1)
            d.setdefault('trading_config_version', d['config_version'])
            for key, value in TIMING_DEFAULTS.items():
                d.setdefault(key, value)
            result = fn(d)
            c.execute('INSERT INTO lp_auto_pool(singleton,payload) VALUES(1,?) '
                      'ON CONFLICT(singleton) DO UPDATE SET payload=excluded.payload',
                      (json.dumps(d,default=_json),))
        return result

    @contextmanager
    def _send_barrier(self):
        with self.send_path.open('a+') as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def _event(self, d, intent, kind, identity=None, occurred_at=None, **facts):
        identity = identity or f"{intent['intent_id']}:{kind}"
        event = dict(event_id=identity, kind=kind, occurred_at=occurred_at,
                     observed_at=self._stamp(), account_id=d['account_id'],
                     auto_run_id=d['run_id'], run_id=d['run_id'],
                     **{k:intent.get(k) for k in ('intent_id','parent_intent_id','session_id','order_id','condition_id','token_id')},
                     side=intent.get('side','BUY'))
        event.update(facts)
        previous = d['events'].get(identity)
        if previous:
            event['observed_at'] = previous['observed_at']
            event['occurred_at'] = occurred_at or previous.get('occurred_at')
        d['events'][identity] = event

    def _receipt_uncertainty(self, d, binding, unknown):
        identity=f"receipt_unknown:{binding['order_id']}"
        previous=d['events'].get(identity)
        if unknown:
            self._event(d,binding,'unknown',identity=identity,reason='order_receipt_unknown')
            if previous and previous.get('resolved_at'):
                d['events'][identity]['observed_at']=self._stamp()
        elif previous:
            previous.setdefault('resolved_at',self._stamp())

    def _funds_fresh(self, intent):
        if intent.get('settled') or intent['state'] in ('aborted', 'rejected', 'reserved'):
            return True
        try:
            _freshness(intent.get('checked_at'), self._now(), 'financial_facts', max_age=Decimal(60))
            return True
        except ValueError:
            return False

    def _isolatable(self, intent):
        # Only the original bounded BUY belongs to this automatic allocation.
        # Additional BUY actions or identity conflicts need account-wide review.
        session = self.store.lp_session(intent['session_id'])
        if (not intent.get('condition_id') or not intent.get('token_id')
                or _maybe_decimal(intent.get('inventory_cost_usd', 0)) is None
                or _maybe_decimal(intent.get('reserved_usd')) is None
                or not session or session.get('order_identity_conflict')
                or intent.get('order_identity_conflict')
                or 'identity' in str(intent.get('reconcile_reason') or '')
                or intent.get('reconcile_reason') in {'owned_order_token_mismatch', 'owned_order_side_mismatch'}):
            return False
        return not any(a.get('side') == 'BUY' and a.get('role') != 'entry'
                       for a in self.store.lp_actions(intent['session_id']))

    def _account_projection_facts(self, d):
        """A covered reservation never returns, including after facts expire."""
        facts = d.get('account_financial_facts')
        waiting = self._account_facts_wait
        wait_reason = waiting['reason'] if waiting else None
        reasons = []
        if facts is None:
            return None, [], ([wait_reason] if wait_reason else
                ['account_financial_facts_unknown'] if any(reservation_is_manually_released(i, d['account_id']) for i in d['intents'].values()) else [])
        if not isinstance(facts, Mapping):
            return {}, [], [*([wait_reason] if wait_reason else []), 'account_financial_facts_unknown']
        wallet = str(facts.get('account_id') or '').strip().casefold()
        if (not wallet or hashlib.sha256(wallet.encode()).hexdigest() != d['account_id']
                or facts.get('pool_account_id') != d['account_id']):
            reasons.append('account_identity_mismatch')
        try:
            _freshness(facts.get('checked_at'), self._now(), 'account_financial_facts', max_age=60)
        except (TypeError, ValueError) as exc:
            reasons.append(str(exc))
        if (type(facts.get('trade_generation')) is not int
                or facts['trade_generation'] != self.store.lp_trade_generation()):
            reasons.append('account_financial_facts_changed')
        if facts.get('financial_status') != 'known':
            reasons.extend(facts.get('reason_codes') or ['account_financial_facts_unknown'])
        if _maybe_decimal(facts.get('inventory_cost_usd')) is None:
            reasons.append('account_financial_facts_unknown')
        buys = []
        seen = set()
        rows = facts.get('buys')
        if not isinstance(rows, (list, tuple)):
            return facts, [], [*([wait_reason] if wait_reason else []), *reasons, 'account_buy_facts_unknown']
        for row in rows:
            if not isinstance(row, Mapping):
                reasons.append('account_buy_facts_unknown')
                continue
            order_id = row.get('order_id')
            if (not order_id or not str(row.get('token_id') or '').strip() or row.get('side') != 'BUY'
                    or order_id in seen):
                reasons.append('account_buy_facts_unknown')
                continue
            seen.add(order_id)
            rotation = d.get('account_rotations', {}).get(order_id, {})
            buy = {**row, 'account_order': True}
            if (row.get('financial_status') != 'known' or row.get('state') == 'unknown'
                    or _maybe_decimal(row.get('reserved_usd')) is None):
                reasons.append('account_buy_facts_unknown')
            if row.get('state') not in ('active', 'canceling', 'unknown'):
                reasons.append('account_buy_facts_unknown')
                buy['state'] = 'unknown'
            if rotation and not rotation.get('rotation_settled_at'):
                # Keep current economic facts; the rotation record contains
                # only historical control metadata, never current principal.
                buy.update({key: value for key, value in rotation.items() if key.startswith('rotation_')}, state='canceling')
            buys.append(buy)
        if waiting and (reasons or not self._account_wait_superseded(waiting, facts)):
            reasons.insert(0, wait_reason)
        return facts, buys, list(dict.fromkeys(reasons))

    @staticmethod
    def _account_facts_identity(facts):
        return (facts.get('snapshot_id'), facts.get('trade_generation')) if isinstance(facts, Mapping) else None

    def _account_wait_superseded(self, waiting, facts):
        identity = self._account_facts_identity(facts)
        if not facts.get('snapshot_id') or identity == waiting['baseline']:
            return False
        if waiting['baseline'] is None:
            return True
        previous_generation = waiting['baseline'][1]
        newer = type(previous_generation) is int and identity[1] > previous_generation
        for key, value in waiting['baseline_times'].items():
            previous = _maybe_datetime(value)
            if previous is None:
                continue
            current = _maybe_datetime(facts.get(key))
            if current is None or current < previous:
                return False
            newer = newer or current > previous
        return newer

    def _unrepresented_intents(self, intents, account_buys, *, account_valid):
        """Keep audit rows, projecting only exposure not already priced by the API."""
        current_ids = {(b['order_id'], b['token_id']) for b in account_buys
                       if b.get('order_id') and b.get('token_id') and b.get('side') == 'BUY'} if account_valid and not getattr(self.lp, '_account_order_sync_error', None) else set()
        result = []
        independent_unknown = False
        for intent in intents:
            represented = (intent.get('order_id'), intent.get('token_id')) in current_ids
            if represented and intent.get('side', 'BUY') == 'BUY':
                session = self.store.lp_session(intent['session_id'])
                actions = self.store.lp_actions(intent['session_id'])
                independent = session is not None and has_independent_unresolved_buy_action(session, actions)
                independent_unknown = independent_unknown or independent
                represented = (session is not None and not session.get('order_identity_conflict')
                    and not intent.get('order_identity_conflict')
                    and 'identity' not in str(intent.get('reconcile_reason') or '')
                    and intent.get('reconcile_reason') not in {'owned_order_token_mismatch', 'owned_order_side_mismatch'}
                    and not self.lp.entry_send_inflight(intent['session_id']))
            else:
                represented = False
            if not represented:
                result.append(intent)
        return result, independent_unknown

    def _projection(self, d, *, include_intents=True):
        audit_intents = list(d['intents'].values())
        # Coverage is a one-way replacement, not a new interpretation of the
        # original submission. Its audit survives late callbacks unchanged.
        intents = [i for i in audit_intents if not reservation_is_released(i, d['account_id'])]
        account, account_buys, account_reasons = self._account_projection_facts(d)
        intents, independent_unknown = self._unrepresented_intents(intents, account_buys,
            account_valid=account is not None and not set(account_reasons)-{'account_send_inflight'})
        if account is not None:
            intents += account.get('pending_buy_actions') or []
        occupied = [i for i in intents if i['state'] not in ('terminal','rejected','aborted')]
        occupied += account_buys
        pending = [i for i in occupied if i['state'] in ('reserved','sending','unknown')]
        pending_review = [i for i in pending if i['state']=='unknown']
        canceling = [i for i in occupied if i['state']=='canceling']
        pnl = sum((_decimal(i.get('realized_pnl_usd',0)) for i in intents), ZERO)
        inventory_unknown = any(_maybe_decimal(i.get('inventory_cost_usd', 0)) is None for i in intents)
        inventory = sum((_maybe_decimal(i.get('inventory_cost_usd', 0)) or ZERO for i in intents), ZERO)
        manual_inventory_unknown = account is None and any(
            reservation_is_manually_released(i, d['account_id']) and _maybe_decimal(i.get('inventory_cost_usd')) is None
            for i in audit_intents)
        if account is None:
            # Historical loss audit stays report data; the configured budget is fixed.
            pnl += sum((min(ZERO, _maybe_decimal(i.get('realized_pnl_usd')) or ZERO) for i in audit_intents
                        if reservation_is_manually_released(i, d['account_id'])), ZERO)
            inventory += sum((_maybe_decimal(i.get('inventory_cost_usd')) or ZERO for i in audit_intents
                              if reservation_is_manually_released(i, d['account_id'])), ZERO)
        if account is not None:
            # Unknown components remain unknown; zeros here are display-only
            # lower bounds and cannot reach either spendability field.
            pnl += _maybe_decimal(account.get('realized_pnl_usd')) or ZERO
            inventory += _maybe_decimal(account.get('inventory_cost_usd')) or ZERO
        reserved = sum((_maybe_decimal(i.get('reserved_usd')) or ZERO for i in occupied), ZERO)
        reserved_unknown = any(_maybe_decimal(i.get('reserved_usd')) is None for i in occupied)
        allocated = sum((_decimal(a['amount_usd']) for a in d['allocations']), ZERO)
        # The configured budget caps current exposure. Realized PnL is report
        # data and never expands or shrinks the next order's spending limit.
        total = _decimal(d['budget_usd']) if d['budget_usd'] is not None else ZERO
        uncertain = [i for i in intents if i.get('financial_status') == 'unknown'
                     or not self._funds_fresh(i) or i['state'] == 'unknown' or i.get('submission_unknown')]
        financial_unknown = bool(uncertain or account_reasons or inventory_unknown or independent_unknown)
        isolated = [i for i in uncertain if self._isolatable(i)]
        # Hold the entire original principal even if old receipts released it.
        # Unconfirmed proceeds/profits cannot increase the spendable lower bound.
        extra_hold = sum((max(ZERO, _decimal(i['price']) * _decimal(i['quantity'])
                            - _decimal(i.get('inventory_cost_usd', 0)) - _decimal(i['reserved_usd']))
                          for i in isolated), ZERO)
        spendable = max(ZERO, total - inventory - reserved - extra_hold)

        reasons = list(account_reasons)
        if inventory_unknown:
            reasons.append('inventory_cost_unknown')
        sync_error = getattr(self.lp, '_account_order_sync_error', None)
        if sync_error:
            reasons.append(sync_error)
        if not self.execution.lp_mutation_allowed():
            reasons.append('circuit_breaker_open')
        manual_reason = 'not_enabled' if not d['ever_enabled'] else 'manually_paused' if not d['desired_running'] else None
        if d['budget_usd'] is None or _decimal(d['budget_usd'])==0:
            reasons.append('budget_zero_or_unset')
        if d['target_buy_count']==0:
            reasons.append('target_zero')
        if any(i['state']=='unknown' or i.get('submission_unknown') for i in intents):
            reasons.append('submission_unknown')
        if financial_unknown:
            reasons.append('financial_facts_unknown')
        if inventory>total:
            reasons.append('inventory_exceeds_budget')
        if d['account_id'] != self.execution._lp_account_id() or not d['account_id']:
            reasons.append('account_identity_unknown')
        admission_reasons = [r for r in reasons if r not in ('submission_unknown', 'financial_facts_unknown')]
        if independent_unknown or len(isolated) != len(uncertain):
            admission_reasons.append('unbounded_financial_uncertainty')
        funds = dict(spendable_usd=str(spendable) if not admission_reasons else None,
                     isolated_reserved_usd=str(extra_hold), total_usd=str(total), available_usd=None if financial_unknown else str(max(ZERO,total-inventory-reserved)),
                     inventory_cost_usd=(None if inventory_unknown or manual_inventory_unknown or account is not None and _maybe_decimal(account.get('inventory_cost_usd')) is None else str(inventory)),
                     buy_reserved_usd=None if reserved_unknown else str(reserved),
                     pending_reserved_usd=(None if any(_maybe_decimal(i.get('reserved_usd')) is None for i in pending)
                         else str(sum((_decimal(i['reserved_usd']) for i in pending), ZERO))),
                     realized_pnl_usd=(None if account is not None and _maybe_decimal(account.get('realized_pnl_usd')) is None else str(pnl)),
                     net_allocation_usd=str(allocated),
                     verified_rewards_usd='0', deficit_usd=str(max(ZERO,inventory+reserved-total)),
                     status='unknown' if financial_unknown else 'known',
                     source='account_verified_facts' if account is not None else 'lp_session_verified_trades',
                     as_of=account.get('checked_at') if account is not None else d['last_reconciled_at'])
        return dict(**{k:deepcopy(d[k]) for k in ('run_id','account_id','config_version','desired_running',
                    'ever_enabled','enabled_at','target_buy_count','budget_usd','buy_price_level',
                    'last_round','last_reconciled_at','updated_at')},
                    plan_wait=deepcopy(d.get('plan_wait')), active_plan=deepcopy(d.get('active_plan')),
                    trading_config_version=d.get('trading_config_version', d['config_version']),
                    **{key: d.get(key, value) for key, value in TIMING_DEFAULTS.items()},
                    auto_run_id=d['run_id'], budget_configured=d['budget_usd'] is not None,
                    pause_confirmed=not d['desired_running'], block_reasons=reasons,
                    admission_block_reasons=admission_reasons,
                    isolated_markets=sorted({i['condition_id'] for i in isolated}),
                    runtime_state='paused' if not d['desired_running'] else 'blocked' if admission_reasons else 'running',
                    reason=manual_reason or (reasons[0] if reasons else None), funds=funds,
                    slots=dict(active=len(occupied)-len(pending)-len(canceling),pending=len(pending),
                               pending_review=len(pending_review),canceling=len(canceling),
                               occupied=len(occupied)),
                    **({'intents': deepcopy(audit_intents), 'account_buys': deepcopy(account_buys)} if include_intents else {}))

    def state(self, *, include_intents=True):
        return self._projection(self._read(), include_intents=include_intents)

    def _manual_current_account_empty(self, document, session):
        """Current API evidence can retire a waived container, not its audit."""
        facts, _, reasons = self._account_projection_facts(document)
        token = str(session.get('token_id') or '')
        return (bool(token) and facts is not None and not reasons
            and not getattr(self.lp, '_account_order_sync_error', None)
            and facts.get('version') == 2 and facts.get('inventory_basis') == 'api_positions'
            and bool(facts.get('snapshot_id')) and account_position_quantity(facts, token) == ZERO
            and token not in facts.get('open_order_tokens', ()))

    def _manual_reservations(self, document, connection):
        wallet = str(getattr(getattr(self.lp.exchange, 'config', None), 'wallet_address', '') or '').strip().casefold()
        rows = []
        bindings = Counter(str(i.get('session_id') or '') for i in document['intents'].values())
        for iid, intent in document['intents'].items():
            sid = str(intent.get('session_id') or '')
            raw = connection.execute('SELECT * FROM lp_sessions WHERE session_id=?', (sid,)).fetchone()
            session = self.store._lp_row_result(raw) if raw else {}
            actions = self.store.lp_actions(sid, connection=connection) if raw else []
            marker = intent.get('manual_reservation_release')
            reason = None
            if (not wallet or document['account_id'] != self.execution._lp_account_id()
                    or hashlib.sha256(wallet.encode()).hexdigest() != document['account_id']):
                reason = 'account_identity_unknown'
            elif any(str(row.get(key) or '').strip().casefold() not in {'', wallet, document['account_id']}
                     for row in (intent, session) for key in ('account_id', 'wallet_address')):
                reason = 'foreign_account'
            elif reservation_is_manually_released(intent, document['account_id']):
                reason = 'already_released'
            elif reservation_is_covered(intent, document['account_id']):
                reason = 'account_covered'
            elif not raw:
                reason = 'session_missing'
            elif self.lp.entry_send_inflight(sid):
                reason = 'send_inflight'
            elif bindings[sid] != 1:
                reason = 'session_binding_conflict'
            elif (intent.get('state') not in {'unknown', 'reserved', 'sending'}
                  or session.get('submit_status') not in {None, '', 'pending', 'unknown', 'accepted_without_order_id'}):
                reason = 'not_unknown_entry'
            elif (intent.get('order_id') or self.store._lp_owned_session_ids(session)
                  or any(action.get('order_id') for action in actions if action.get('role') == 'entry')):
                reason = 'order_identity_known'
            elif has_independent_unresolved_action(session, actions):
                reason = 'independent_action_unresolved'
            amount = _maybe_decimal(intent.get('reserved_usd'))
            if reason is None and intent.get('reserved_usd') is not None and (amount is None or amount < ZERO):
                reason = 'reservation_amount_unknown'
            event = document['events'].get(f'{iid}:unknown', {})
            rows.append(dict(intent_id=iid, session_id=sid,
                amount_usd=str(amount) if amount is not None else None,
                **{key: intent.get(key) for key in ('market_id', 'condition_id', 'token_id')},
                original_unknown_reason=event.get('reason') or intent.get('reconcile_reason') or session.get('facts_error') or 'submission_unknown',
                eligible=reason is None, exclusion_reason=reason,
                manual_reservation_release=deepcopy(marker),
                reservation_version=hashlib.sha256(json.dumps({key: intent.get(key) for key in
                    ('session_id', 'created_at', 'config_version', 'state', 'reserved_usd', 'price', 'quantity', 'order_id', 'account_id')},
                    sort_keys=True, default=_json).encode()).hexdigest(),
                session_revision=int(json.loads(raw['payload']).get('_lp_revision', 0)) if raw else None))
        return rows

    def reservations(self):
        # Selection and its session fences come from one read-only snapshot.
        with self.store._read_connection() as connection:
            connection.execute('BEGIN')
            row = connection.execute('SELECT payload FROM lp_auto_pool WHERE singleton=1').fetchone()
            document = json.loads(row[0]) if row else self._default()
            reservations = self._manual_reservations(document, connection)
        return dict(reservations=reservations, account_id=document['account_id'], desired_running=document['desired_running'])

    def release_reservations(self, payload, *, audit=None):
        single = 'intent_id' in payload
        expected = {'confirm', 'reason', 'intent_id' if single else 'all_releasable'}
        if set(payload) != expected or payload.get('confirm') is not True:
            raise ValueError('manual_release_selection_or_confirmation_invalid')
        if single:
            if not isinstance(payload['intent_id'], str) or not payload['intent_id'].strip():
                raise ValueError('intent_id_invalid')
        elif payload['all_releasable'] is not True:
            raise ValueError('all_releasable_must_be_true')
        reason = payload['reason']
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 500:
            raise ValueError('manual_release_reason_invalid')
        selection = self.reservations()['reservations']
        selected = {row['intent_id']: row for row in selection
                    if not single or row['intent_id'] == payload['intent_id']}
        released, already, skipped = [], [], []
        if single and not selected:
            skipped.append(dict(intent_id=payload['intent_id'], reason='intent_not_found', amount_usd=None))
        wallet = str(getattr(getattr(self.lp.exchange, 'config', None), 'wallet_address', '') or '').strip().casefold()
        # Same lock order as auto entry admission; no network or venue action.
        with self._send_barrier():
            lock = self.execution._acquire_global_lock()
            if lock is None:
                raise ValueError('execution_lock')
            try:
                with self.lp._mutex, self.store._transaction() as connection:
                    def apply(document):
                        current = {row['intent_id']: row for row in self._manual_reservations(document, connection)}
                        for iid, chosen in selected.items():
                            row = current.get(iid)
                            result = {key: (row or chosen).get(key) for key in ('intent_id', 'session_id', 'amount_usd')}
                            if row and row['exclusion_reason'] == 'already_released':
                                already.append(result)
                                continue
                            exclusion = chosen['exclusion_reason'] or (row['exclusion_reason'] if row else 'intent_not_found')
                            if exclusion is None and (row['session_id'], row['session_revision']) != (chosen['session_id'], chosen['session_revision']):
                                exclusion = 'session_changed'
                            if exclusion is None and row['reservation_version'] != chosen['reservation_version']:
                                exclusion = 'reservation_changed'
                            if exclusion:
                                skipped.append(dict(**result, reason=exclusion))
                                continue
                            intent = document['intents'][iid]
                            marker = dict(version=1, state='released', intent_id=iid, session_id=row['session_id'],
                                account_id=wallet, pool_account_id=document['account_id'], released_at=self._stamp(),
                                amount_usd=row['amount_usd'], original_reserved_usd=intent.get('reserved_usd'),
                                original_unknown_reason=row['original_unknown_reason'], reason=reason.strip(),
                                selection={'intent_id': iid} if single else {'all_releasable': True},
                                actor=deepcopy(audit or {'actor': 'local_operator'}))
                            intent['manual_reservation_release'] = marker
                            raw = connection.execute('SELECT * FROM lp_sessions WHERE session_id=?', (row['session_id'],)).fetchone()
                            session = json.loads(raw['payload'])
                            marker['original_session_state'] = raw['state']
                            marker['original_submit_status'] = session.get('submit_status')
                            marker['original_entry_actions'] = [a for a in self.store.lp_actions(row['session_id'], connection=connection)
                                if submission_action_kind(a) == 'entry']
                            session['manual_reservation_release'] = marker
                            session['_lp_revision'] = int(session.get('_lp_revision', 0)) + 1
                            session['_lp_trade_revision'] = int(session.get('_lp_trade_revision', 0)) + 1
                            state = raw['state']
                            local_empty = not any((_maybe_decimal(session.get(key)) or ZERO) > ZERO
                                for key in ('residual_quantity', 'buy_filled_quantity'))
                            api_empty = self._manual_current_account_empty(document, session)
                            if local_empty or api_empty:
                                # Retire only the request container; preserve all
                                # quantities and reuse only real published API facts.
                                session['manual_release_retired'] = dict(reason='operator_waived_provisional_hold', released_at=marker['released_at'])
                                if api_empty and not local_empty:
                                    facts = document['account_financial_facts']
                                    session['account_coverage_retired'] = dict(reason='account_observation_no_exposure',
                                        snapshot_id=facts['snapshot_id'], checked_at=facts['checked_at'])
                                state = 'complete'
                            connection.execute('UPDATE lp_sessions SET state=?,payload=? WHERE session_id=?',
                                (state, json.dumps(session, default=_json), row['session_id']))
                            self._event(document, intent, 'manual_reservation_release', **marker)
                            released.append(result)
                    self._update(apply, connection=connection)
            finally:
                self.execution._release_global_lock(lock)
        return dict(released=released, already_released=already, skipped=skipped,
                    released_amount_usd=(None if any(row['amount_usd'] is None for row in released)
                        else str(sum((Decimal(row['amount_usd']) for row in released), ZERO))), state=self.state())

    def configure(self, payload, *, audit=None):
        if (not isinstance(payload, dict) or not payload or
                set(payload)-{'budget_usd','target_buy_count','buy_price_level','expected_config_version', *TIMING_DEFAULTS}):
            raise ValueError('auto_config_invalid')
        timing = {key: payload[key] for key in TIMING_DEFAULTS if key in payload}
        if any(type(value) is not int or value <= 0 for value in timing.values()):
            raise ValueError('auto_timing_must_be_positive_integer_seconds')
        trading = bool(set(payload) & {'budget_usd', 'target_buy_count', 'buy_price_level'})
        if not trading:
            if not timing:
                raise ValueError('auto_config_invalid')
            def update_timing(d):
                if not d['account_id'] or d['account_id'] != self.execution._lp_account_id():
                    raise ValueError('account_identity_mismatch')
                if payload.get('expected_config_version', d['config_version']) != d['config_version']:
                    raise ValueError('config_version_changed')
                d.update(timing, config_version=d['config_version'] + 1, updated_at=self._stamp())
            self._update(update_timing)
            return self.state()
        budget = _decimal(payload.get('budget_usd'), 'budget_usd')
        target = payload.get('target_buy_count')
        if budget<0 or isinstance(target,bool) or not isinstance(target,int) or not 0<=target:
            raise ValueError('auto_config_invalid')
        level = payload.get('buy_price_level', 1)
        if type(level) is not int or level not in (1, 2):
            raise ValueError('auto_config_invalid')
        level_supplied = 'buy_price_level' in payload
        def apply(d):
            state=self._projection(d)
            if not d['account_id'] or d['account_id']!=self.execution._lp_account_id():
                raise ValueError('account_identity_mismatch')
            initial_account_setup = (d['config_version'] == 0 and d['budget_usd'] is None
                and not d['ever_enabled'] and d.get('account_financial_facts') is not None
                and not any(i['state'] not in ('terminal', 'rejected', 'aborted')
                    and not reservation_is_released(i, d['account_id']) for i in d['intents'].values()))
            if d['desired_running'] or (state['slots']['occupied'] and not initial_account_setup):
                raise ValueError('pause_and_finish_automatic_buys_before_configuring')
            if payload.get('expected_config_version',d['config_version'])!=d['config_version']:
                raise ValueError('config_version_changed')
            if state['funds']['status']!='known':
                raise ValueError('financial_facts_unknown')
            d['config_version']+=1
            d['trading_config_version'] += 1
            d['allocations'].append(dict(source_id=f"config:{d['config_version']}",
                amount_usd=str(budget-_decimal(state['funds']['total_usd'])), occurred_at=self._stamp(),audit=audit))
            d.update(budget_usd=str(budget),target_buy_count=target,
                     buy_price_level=level if level_supplied else d.get('buy_price_level', 1),
                     updated_at=self._stamp())
            d.update(timing)
        with self._send_barrier():
            self._update(apply)
        return self.state()

    def set_desired_running(self, running, *, audit=None):
        if not isinstance(running,bool):
            raise ValueError('desired_running_invalid')
        def apply(d):
            if running and (not d['account_id'] or d['account_id']!=self.execution._lp_account_id()):
                raise ValueError('account_identity_mismatch')
            d.update(desired_running=running,updated_at=self._stamp())
            if running:
                d['ever_enabled']=True
                d['enabled_at']=d['enabled_at'] or self._stamp()
        with self._send_barrier():
            self._update(apply)
        return self.state()

    def _excluded(self, condition_id, *, ignore=None):
        for session in self.store.lp_sessions():
            if session.get('session_id')==ignore or session.get('condition_id')!=condition_id:
                continue
            if (reservation_is_released(session, self.execution._lp_account_id())
                    and not self.lp._has_unresolved_submission(session)):
                local_empty = session.get('manual_release_retired') and not self.lp._session_order_ids(session) and not any(
                    (_maybe_decimal(session.get(key)) or ZERO) > ZERO
                    for key in ('residual_quantity', 'buy_filled_quantity'))
                account_empty = False
                if session.get('state') == 'complete' and session.get('account_coverage_retired'):
                    account_empty = self._manual_current_account_empty(self._read(), session)
                independent = has_independent_unresolved_action(session,
                    self.store.lp_actions(str(session['session_id'])))
                if (local_empty or account_empty) and not independent:
                    continue
            if session.get('state') not in ('complete','entry_rejected'):
                history=self.lp._order_history(session)
                buys=[h for h in history.values() if h.get('side')=='BUY']
                if not (buys and all(h.get('status') in TERMINAL_ORDER_STATES for h in buys)
                        and session.get('position_reconciled') is True
                        and _decimal(session.get('residual_quantity',0))==0):
                    return True
            if _decimal(session.get('buy_filled_quantity',0))>_decimal(session.get('sold_quantity',0)):
                return True
        return False

    def candidates(self, *, releasing=(), diagnostics=None, bid_level=None):
        """Consume the entire qualified pool before exclusion, never the UI top ten."""
        from .polymarket_lp import _candidate_pool_row_expired, _candidate_yield_sort_key
        if bid_level is None:
            bid_level = self._read().get('buy_price_level', 1)
        self.lp._evict_excluded_candidates()
        # Requalify before stale source gates remove a market from ranking.
        # Maintenance owns the ten-market bound, in-flight fence and retries;
        # ranking still evaluates the returned facts at the configured level.
        from .polymarket_lp import _candidate_head_source_values, _candidate_source_expired
        now = self._now()
        with self.lp._candidate_state_lock:
            stale_conditions = tuple(
                key for key, value in self.lp._candidate_qualification_facts.items()
                if key in self.lp._candidate_pool
                and not _candidate_pool_row_expired(self.lp._candidate_pool[key], now)
                and any(_candidate_source_expired(stamp, now)
                        for stamp in _candidate_head_source_values(value)[1:])
            )
            stale_facts = {key: deepcopy(self.lp._candidate_qualification_facts[key])
                           for key in stale_conditions}
        if stale_conditions:
            priorities = {key: _candidate_refresh_priority(value, bid_level=bid_level, now=now)
                          for key, value in stale_facts.items()}
            self.lp.refresh_candidate_recommendations(
                condition_ids=stale_conditions, refresh_priorities=priorities)
        with self.lp._candidate_state_lock:
            facts={}
            expired=set()
            pool_checked_at = None
            pool_check_min = pool_check_max = None
            global_recovery_generation = self.lp._current_candidate_global_generation()
            for key,value in self.lp._candidate_qualification_facts.items():
                if key not in self.lp._candidate_pool:
                    continue
                if (
                    global_recovery_generation is None
                    or self.lp._candidate_global_generation(
                        value.get("global_recovery_generation")
                    )
                    != global_recovery_generation
                ):
                    continue
                pool_checked_at = self._now()
                if diagnostics is not None:
                    pool_check_min = pool_checked_at if pool_check_min is None else min(pool_check_min, pool_checked_at)
                    pool_check_max = pool_checked_at if pool_check_max is None else max(pool_check_max, pool_checked_at)
                if _candidate_pool_row_expired(self.lp._candidate_pool[key], pool_checked_at):
                    expired.add(key)
                else:
                    facts[key] = deepcopy(value)
            updated_at = {key:self.lp._candidate_pool[key].get('updated_at') for key in facts}
            pool_times = {key: {field: row.get(field) for field in ('updated_at', 'expires_at')}
                          for key,row in self.lp._candidate_pool.items()} if diagnostics is not None else {}
        if diagnostics is not None:
            if pool_check_min is not None:
                _candidate_filter_range(diagnostics, 'pool_checked_at', pool_check_min)
                _candidate_filter_range(diagnostics, 'pool_checked_at', pool_check_max)
            # Missing-facts rows have no original expiry check. Inventory them
            # at this snapshot's existing reference time, without admitting any.
            reference = pool_checked_at or _maybe_datetime(diagnostics['round_started_at'])
            diagnostics['state'] = 'evaluated'
            diagnostics['counts'] = dict.fromkeys(('pool_total', 'pool_expired', 'pool_unexpired', 'missing_facts', 'facts_present',
                'participating_markets', 'evaluated_markets', 'evaluated_directions', 'qualification_rejected',
                'qualification_unknown', 'eligible_directions', 'estimate_unknown', 'qualified_directions', 'recheck_removed'), 0)
            counts = diagnostics['counts']
            counts['pool_total'] = len(pool_times)
            for key,row in pool_times.items():
                _candidate_filter_range(diagnostics, 'pool_updated_at', row['updated_at'])
                _candidate_filter_range(diagnostics, 'pool_expires_at', row['expires_at'])
                if key in expired or key not in facts and reference is not None and _candidate_pool_row_expired(row, reference):
                    counts['pool_expired'] += 1
                    _candidate_filter_reasons(diagnostics, ['candidate_pool_expired'])
                elif key not in facts:
                    counts['missing_facts'] += 1
                    _candidate_filter_reasons(diagnostics, ['qualification_facts_missing'])
                else:
                    counts['facts_present'] += 1
            counts['pool_unexpired'] = counts['pool_total'] - counts['pool_expired']
        result=[]
        for condition_id,cached in facts.items():
            if self._excluded(condition_id):
                if diagnostics is not None:
                    counts['participating_markets'] += 1
                    _candidate_filter_reasons(diagnostics, ['participating_market'])
                continue
            if diagnostics is not None:
                counts['evaluated_markets'] += 1
            for direction in cached.get('directions',[]):
                if not self.lp._candidate_allowed(((condition_id, str(direction.get('market', {}).get('token_id') or '')),)):
                    continue
                current_account = self._current_account
                account = current_account or cached.get('account') or {}
                source = 'current' if current_account else 'candidate' if account else 'unknown'
                ranking_account = self._ranking_account(account, releasing)
                now = self._now()
                reservations = self._ranking_reservations(releasing)
                evaluated=evaluate_lp_entry(direction,account=ranking_account,
                    now=now,reservations=reservations,candidate=True,bid_level=bid_level)
                if diagnostics is not None:
                    counts['evaluated_directions'] += 1
                    diagnostics['account_sources'][source] += 1
                    _candidate_filter_range(diagnostics, 'evaluation_used_at', now)
                    _candidate_filter_range(diagnostics, 'account_at', account.get('checked_at'))
                    _candidate_filter_range(diagnostics, 'account_generation', account.get('trade_generation'), generation=True)
                    market = direction.get('market') if isinstance(direction, Mapping) else None
                    book = direction.get('book') if isinstance(direction, Mapping) else None
                    for field,value in (('reward_at', direction.get('reward_checked_at') if isinstance(direction, Mapping) else None),
                        ('metadata_at', market.get('metadata_checked_at') if isinstance(market, Mapping) else None),
                        ('fees_at', market.get('fees_checked_at') if isinstance(market, Mapping) else None),
                        ('book_at', book.get('received_at') if isinstance(book, Mapping) else None)):
                        _candidate_filter_range(diagnostics, field, value)
                if evaluated.get('state')!='eligible':
                    if diagnostics is not None:
                        counts['qualification_rejected' if evaluated.get('state') == 'rejected' else 'qualification_unknown'] += 1
                        _candidate_filter_reasons(diagnostics, evaluated.get('reason_codes') or ['other'])
                    continue
                if diagnostics is not None:
                    counts['eligible_directions'] += 1
                guidance=evaluated['guidance']
                estimate_at = self._now()
                estimate=minimum_order_estimate(direction,guidance,estimate_at)
                _candidate_filter_range(diagnostics, 'estimate_used_at', estimate_at)
                if estimate['state']!='known':
                    if diagnostics is not None:
                        counts['estimate_unknown'] += 1
                        _candidate_filter_reasons(diagnostics, estimate.get('reason_codes') or ['other'])
                    continue
                result.append({**guidance,'minimum_order_estimate':estimate,
                    'estimated_yield_raw':estimate['yield_pct_per_hour'],
                    'updated_at':updated_at[condition_id]})
        if diagnostics is not None:
            counts['qualified_directions'] = len(result)
        return sorted(result,key=lambda r:(*_candidate_yield_sort_key(r), str(r['token_id'])))

    def _ranking_account(self, account, releasing):
        if not isinstance(account.get('open_orders'), (list, tuple)):
            return dict(account)
        ids = {i['order_id'] for i in releasing}
        return {**account, 'open_orders': [o for o in account.get('open_orders', [])
                if self.lp._order_id(o) not in ids]}

    def _ranking_reservations(self, releasing):
        ids = {i['order_id'] for i in releasing}
        return tuple(r for r in self.lp._candidate_reservations() if r['order_id'] not in ids)

    @staticmethod
    def _buy_identity(row):
        return ('account', row['order_id']) if row.get('account_order') else ('intent', row.get('intent_id'))

    @staticmethod
    def _resting_buy(row):
        return bool(row.get('intent_id') or row.get('account_order'))

    def _rotation_submission_evidence(self, intent, session):
        if not session:
            return True, {}
        actions = self.store.lp_actions(session['session_id'])
        inputs = dict(
            entry_send_inflight=self.lp.entry_send_inflight(session['session_id']),
            independent_unresolved_action=has_independent_unresolved_action(session, actions),
            session_submission_unresolved=self.lp._has_unresolved_submission(session),
            entry_stage_unfinished=session.get('submit_stage') in {'preparing', 'sending'},
        )
        if inputs['entry_send_inflight'] or inputs['independent_unresolved_action']:
            return True, inputs
        if not inputs['session_submission_unresolved'] and not inputs['entry_stage_unfinished']:
            return False, inputs
        # A completed exact entry receipt can resolve this lifecycle's old
        # stage only after current account coverage and order ownership agree.
        # Evaluate a copy; the original submission audit remains unchanged.
        order_id = str(intent.get('order_id') or '')
        inputs['entry_coverage_matches'] = bool(
            intent.get('account_order') and reservation_is_covered(session)
            and order_id and session.get('entry_order_id') == order_id)
        if not inputs['entry_coverage_matches']:
            return True, inputs
        inputs['entry_receipt_verified'] = any(
            submission_action_kind(action) == 'entry'
            and action.get('order_id') == order_id
            and action.get('token_id') == session.get('token_id')
            and action.get('side') == 'BUY'
            and action.get('state') in {'accepted', 'complete'}
            and action.get('submit_stage') == 'receipt_received'
            and bool(action.get('submit_receipt_at'))
            and bool(action.get('submit_finished_at'))
            for action in actions
        )
        if not inputs['entry_receipt_verified']:
            return True, inputs
        return self.lp._has_unresolved_submission({**session, 'submit_stage': 'receipt_received'}), inputs

    def _capture_rotation_diagnostic(self, diagnostics, intent, session, account, *,
                                     outcome, reason, predicate, inputs):
        """Freeze the used inputs; reuse the bounded, error-isolated log sink."""
        try:
            def identity(value):
                return hashlib.sha256(str(value).encode()).hexdigest()[:16] if value else None

            def stamp(value):
                parsed = _maybe_datetime(value)
                return parsed.isoformat() if parsed is not None else None

            account = account or {}
            session = session or {}
            snapshot = dict(
                event='lp_rotation_guard', outcome=outcome, reason=reason,
                round_id=identity((diagnostics or {}).get('round_id')),
                first_failing_predicate=predicate,
                session_id=identity(session.get('session_id') or intent.get('session_id')),
                order_id=identity(intent.get('order_id')),
                account_snapshot_id=identity(account.get('snapshot_id')),
                account_checked_at=stamp(account.get('checked_at')),
                account_read_started_at=stamp(account.get('read_started_at')),
                account_read_ended_at=stamp(account.get('read_ended_at')),
                account_generation=account.get('trade_generation'),
                session_revision=session.get('_lp_revision'),
                submit_stage=session.get('submit_stage') if session.get('submit_stage') in {
                    'preparing', 'sending', 'send_unknown', 'receipt_received', 'prepare_failed',
                    'exchange_rejected', 'pre_send_rejected'} else None,
                predicate_inputs=dict(inputs),
            )
            if diagnostics is not None:
                records = diagnostics.setdefault('rotation_guards', [])
                if len(records) >= 32:
                    return
                records.append(deepcopy(snapshot))
            _lp_capture_read_log(lambda snapshot=snapshot: logger.info(
                'lp_rotation_guard facts=%s', json.dumps(snapshot, sort_keys=True, separators=(',', ':'))))
        except Exception:
            pass

    def _rotation_session(self, intent, *, diagnostics=None, account=None):
        session = self.store.lp_session(intent['session_id'])
        account_order = intent.get('account_order')
        history = self.lp._order_history(session or {})
        order_ids = self.lp._session_order_ids(session or {})
        submission_unresolved, submission_inputs = self._rotation_submission_evidence(intent, session)
        predicates = {
            'intent_not_active': intent['state'] != 'active',
            'session_not_entry_open': not session or session.get('state') != 'entry_open',
            'order_cancel_requested': bool(session and self.lp._order_cancel_requested(session, intent['order_id'])),
            'stop_requested': bool(session and session.get('stop_requested')),
            'stop_loss_latched': bool(session and session.get('stop_loss_latched')),
            'order_identity_conflict': bool(session and session.get('order_identity_conflict')),
            'entry_send_inflight': submission_inputs.get('entry_send_inflight', False),
            'independent_unresolved_action': submission_inputs.get('independent_unresolved_action', False),
            'unresolved_submission': submission_unresolved,
            'buy_filled_nonzero': bool(session and _decimal(session.get('buy_filled_quantity', 0))),
            'account_fill_not_zero': bool(account_order and _maybe_decimal(intent.get('filled_quantity')) != ZERO),
            'market_identity_mismatch': not session or any(str(session.get(k)) != str(intent[k]) for k in ('condition_id', 'token_id')),
            'order_not_owned': intent['order_id'] not in order_ids,
            'entry_identity_mismatch': bool(not account_order and session and (
                session.get('entry_order_id') != intent['order_id'] or order_ids != [intent['order_id']])),
            'order_not_live': history.get(intent['order_id'], {}).get('status') != 'LIVE',
        }
        predicate = next((name for name, failed in predicates.items() if failed), None)
        reason = 'rotation_awaiting_reconciliation' if predicate else None
        if not predicate:
            predicates['review_deadline'] = (session.get('review_at') is not None or not account_order) and self._now() >= _timestamp(session.get('review_at'))
            if predicates['review_deadline']:
                predicate = reason = 'review_deadline'
            else:
                predicates['rotation_protection_active'] = any(
                    b.get('state') not in ('registered', 'monitoring')
                    for b in self.lp._queue_protection_levels(session).values())
                if predicates['rotation_protection_active']:
                    predicate = reason = 'rotation_protection_active'
        if diagnostics is not None:
            self._capture_rotation_diagnostic(diagnostics, intent, session, account,
                outcome='blocked' if predicate else 'recovered' if session.get('submit_stage') in {'preparing', 'sending'} else 'eligible',
                reason=reason, predicate=predicate, inputs={**predicates, **submission_inputs})
        if reason:
            raise ValueError(reason)
        return session

    def _configured_level_departure(self, intent, direction, account, bid_level):
        """Prove the resting quote's rank without qualifying a replacement."""
        book = direction['book']
        if (book.get('condition_id', book.get('market')) != intent['condition_id']
                or book.get('token_id', book.get('asset_id')) != intent['token_id']):
            raise ValueError('book_identity_mismatch')
        _freshness(book.get('received_at'), self._now(), 'book_freshness', max_age=60)
        prices = sorted({price for price, _ in _levels(book.get('bids'), 'bids')}, reverse=True)
        price = _decimal(intent['price'])
        if price not in prices or prices.index(price) + 1 == bid_level:
            return False
        # Departure is a cancellation decision, not new entry admission. It
        # still requires the same complete, authenticated account and a valid
        # uncrossed book; missing/unknown facts do not prove a departure.
        if (account.get('authenticated') is not True
                or account.get('open_orders_complete') is not True
                or account.get('positions_complete') is not True
                or not isinstance(account.get('positions'), (list, tuple))
                or _maybe_decimal(account.get('balance')) is None
                or _maybe_decimal(account.get('allowance')) is None):
            raise ValueError('account_facts_unknown')
        asks = _levels(book.get('asks'), 'asks')
        if not asks or prices[0] >= min(p for p, _ in asks):
            raise ValueError('book_invalid')
        return True

    def _ranked_buys(self, state, *, diagnostics=None):
        """Rank actual BUY IDs and candidates on the same yield basis."""
        account, account_buys, account_reasons = self._account_projection_facts(self._read())
        account_facts = account
        intents, _ = self._unrepresented_intents(state['intents'], account_buys,
            account_valid=account is not None and account.get('financial_status') == 'known' and not account_reasons)
        active = [i for i in intents if not reservation_is_released(i, state['account_id'])
                  and i['state'] not in ('terminal', 'rejected', 'aborted')]
        active += state.get('account_buys', [])
        occupied = list(active)
        bid_level = state.get('buy_price_level', 1)
        occupied_count = len(active)
        blocked = []
        rankable = []
        state_reasons = {'reserved': 'submission_pending', 'sending': 'submission_pending',
                         'unknown': 'submission_unknown', 'canceling': 'rotation_awaiting_reconciliation'}
        for intent in active:
            if intent['state'] != 'active':
                blocked.append({'condition_id': intent['condition_id'], 'token_id': intent['token_id'],
                                'reason': intent.get('reconcile_reason') or state_reasons[intent['state']]})
                self._capture_rotation_diagnostic(diagnostics, intent, None, account_facts,
                    outcome='blocked', reason=state_reasons[intent['state']], predicate='intent_not_active',
                    inputs={'intent_not_active': True})
                continue
            if intent.get('financial_status') != 'known':
                blocked.append({'condition_id': intent['condition_id'], 'token_id': intent['token_id'],
                                'reason': 'financial_facts_unknown'})
                self._capture_rotation_diagnostic(diagnostics, intent, None, account_facts,
                    outcome='blocked', reason='financial_facts_unknown', predicate='financial_facts_unknown',
                    inputs={'financial_status_known': False})
                continue
            try:
                _freshness(intent.get('checked_at'), self._now(), 'financial_facts', max_age=Decimal(60))
            except ValueError as exc:
                blocked.append({'condition_id': intent['condition_id'], 'token_id': intent['token_id'],
                                'reason': str(exc)})
                self._capture_rotation_diagnostic(diagnostics, intent, None, account_facts,
                    outcome='blocked', reason=str(exc), predicate=str(exc),
                    inputs={'financial_facts_fresh': False})
                continue
            rankable.append(intent)
        active = rankable
        rows = []
        drifted = []
        for intent in active:
            try:
                self._rotation_session(intent, diagnostics=diagnostics, account=account_facts)
                facts = self.lp._read_candidate_facts(intent, wait_for_capacity=True)
                account, direction = facts['account'], facts['direction']
                wallet = str(account.get('wallet_address') or '').strip().casefold()
                if not wallet or hashlib.sha256(wallet.encode()).hexdigest() != state['account_id']:
                    raise ValueError('account_identity_mismatch')
                current = [o for o in (account.get('open_orders') or []) if self.lp._order_id(o) == intent['order_id']]
                if (len(current) != 1 or current[0].get('side') != 'BUY'
                        or str(current[0].get('status')).upper() != 'LIVE'
                        or str(current[0].get('token_id')) != intent['token_id']
                        or _maybe_decimal(current[0].get('size_matched')) != ZERO
                        or _maybe_decimal(current[0].get('price')) != _decimal(intent['price'])
                        or self.lp._queue_row_remaining(current[0]) != _decimal(intent['quantity'])):
                    raise ValueError('rotation_order_changed')
                if self._configured_level_departure(intent, direction, account, bid_level):
                    # Factual departure cancels the current quote; it does not
                    # require a reward estimate or authorize a replacement.
                    row = {**intent, 'ranking_account': account,
                           'ranking_book_at': direction['book']['received_at'], 'configuration_drift': True}
                    self._ranking_fresh(row)
                    rows.append(row)
                    drifted.append(row)
                    continue
                evaluated = evaluate_lp_entry(direction, account=self._ranking_account(account, active),
                    now=self._now(), reservations=self._ranking_reservations(active), candidate=True,
                    bid_level=bid_level)
                if evaluated.get('state') != 'eligible':
                    raise ValueError('rotation_yield_unknown')
                estimate = minimum_order_estimate(direction, intent, self._now(), resting_quantity=_decimal(intent['quantity']))
                if estimate['state'] != 'known':
                    raise ValueError('rotation_yield_unknown')
                row = {**intent, 'minimum_order_estimate': estimate,
                       'ranking_account': account, 'ranking_book_at': direction['book']['received_at'],
                       'ranking_reward_at': direction['reward_checked_at']}
                self._ranking_fresh(row)
                rows.append(row)
            except ValueError as exc:
                if 'identity' in str(exc):
                    raise
                blocked.append({'condition_id': intent['condition_id'], 'token_id': intent['token_id'], 'reason': str(exc)})
                continue
        active = [i for i in active if any(self._buy_identity(r) == self._buy_identity(i) for r in rows)]
        retained = [{**i, 'retained_constraint': True,
                     'minimum_order_estimate': dict(state='unknown', capital_usd=i.get('reserved_usd'))}
                    for i in occupied if self._buy_identity(i) not in {self._buy_identity(r) for r in active}]
        slots = state['target_buy_count'] - (occupied_count - len(active))
        if slots <= 0:
            if (state['funds'].get('source') == 'account_verified_facts'
                    and occupied_count > state['target_buy_count']):
                # Protected/partially filled actual BUYs keep their slots.
                # Validated unfilled extras still leave through the same
                # exact-ID rotation lane even with no replacement capacity.
                return [], rows, [], blocked
            return [], [], [], blocked
        candidates = self.candidates(releasing=active, diagnostics=diagnostics, bid_level=bid_level)
        rows = [r for r in rows if not r.get('configuration_drift')]
        rows.extend(candidates)
        refreshed = {(r['condition_id'], r['token_id']) for r in rows if self._resting_buy(r)}
        while True:
            rows.sort(key=lambda r: (-_decimal(r['minimum_order_estimate']['yield_pct_per_hour']),
                not self._resting_buy(r), str(r['condition_id']), str(r['token_id']), str(r.get('order_id') or '')))
            unique = {}
            for row in rows:
                # Two actual IDs consume two slots even on one token. New
                # candidate duplicates still obey one-market admission.
                key = self._buy_identity(row) if row.get('account_order') else ('market', row['condition_id'])
                unique.setdefault(key, row)
            available = _decimal(state['funds']['spendable_usd']) + sum(
                (_decimal(i['reserved_usd']) for i in active), ZERO)
            for row in unique.values():
                if 'ranking_account' not in row:
                    continue
                account = _account_after_reservations(self._ranking_account(row['ranking_account'], active),
                                                     self._ranking_reservations(active))
                if account is None:
                    raise ValueError('account_facts_unknown')
                available = min(available, _decimal(account['balance']), _decimal(account['allowance']))
            targets = []
            budget_skipped = []
            remaining = available
            for row in unique.values():
                if len(targets) >= slots:
                    break
                capital = _decimal(row['minimum_order_estimate']['capital_usd'])
                if capital > remaining:
                    budget_skipped.append({'condition_id': row['condition_id'], 'token_id': row['token_id'],
                                           'reason': 'rotation_budget_insufficient'})
                    continue
                targets.append(row)
                remaining -= capital
            pending = next((r for r in targets if (r['condition_id'], r['token_id']) not in refreshed), None)
            if pending is None:
                break
            try:
                facts = self.lp._read_candidate_facts(pending, wait_for_capacity=True)
                account, direction = facts['account'], facts['direction']
                evaluated = evaluate_lp_entry(direction, account=self._ranking_account(account, active),
                    now=self._now(), reservations=self._ranking_reservations(active), candidate=True,
                    bid_level=bid_level)
                if evaluated.get('state') != 'eligible':
                    if evaluated.get('state') == 'rejected':
                        rows.remove(pending)
                        candidates.remove(pending)
                        if diagnostics is not None:
                            diagnostics['counts']['recheck_removed'] += 1
                            _candidate_filter_reasons(diagnostics, evaluated.get('reason_codes') or ['other'], field='recheck_reasons')
                        continue
                    raise ValueError('ranking_changed')
                self.lp._require_lp_history(pending, now=self._now())
                estimate = minimum_order_estimate(direction, evaluated['guidance'], self._now())
                if estimate['state'] != 'known':
                    raise ValueError('ranking_yield_unknown')
                pending.update(evaluated['guidance'], minimum_order_estimate=estimate, ranking_account=account,
                               ranking_book_at=direction['book']['received_at'], ranking_reward_at=direction['reward_checked_at'])
                self._ranking_fresh(pending)
                refreshed.add((pending['condition_id'], pending['token_id']))
            except ValueError as exc:
                if 'identity' in str(exc):
                    raise
                blocked.append({'condition_id': pending['condition_id'], 'token_id': pending['token_id'], 'reason': str(exc)})
                rows.remove(pending)
                candidates.remove(pending)
                if diagnostics is not None:
                    diagnostics['counts']['recheck_removed'] += 1
                    _candidate_filter_reasons(diagnostics, [str(exc)], field='recheck_reasons')
        blocked.extend(budget_skipped)
        for row in targets:
            self._ranking_fresh(row)
        selected = {self._buy_identity(t) for t in targets if self._resting_buy(t)}
        victims = drifted + [r for r in rows if self._resting_buy(r) and self._buy_identity(r) not in selected]
        if victims:
            capital = sum((_decimal(r['minimum_order_estimate']['capital_usd']) for r in targets), ZERO)
            available = _decimal(state['funds']['spendable_usd']) + sum((_decimal(i['reserved_usd']) for i in active), ZERO)
            for row in targets:
                account = _account_after_reservations(self._ranking_account(row['ranking_account'], active),
                                                     self._ranking_reservations(active))
                if account is None:
                    raise ValueError('account_facts_unknown')
                available = min(available, _decimal(account['balance']), _decimal(account['allowance']))
            if capital > available:
                raise ValueError('top_yield_funds_insufficient')
        candidates.sort(key=lambda r: (-_decimal(r['minimum_order_estimate']['yield_pct_per_hour']), str(r['condition_id']), str(r['token_id'])))
        return [*retained, *targets], victims, candidates, blocked

    def _ranking_fresh(self, row):
        wallet = str(row['ranking_account'].get('wallet_address') or '').strip().casefold()
        if not wallet or hashlib.sha256(wallet.encode()).hexdigest() != self.execution._lp_account_id():
            raise ValueError('account_identity_mismatch')
        stamps = [row['ranking_account'].get('checked_at'), row['ranking_book_at']]
        if not row.get('configuration_drift'):
            stamps.append(row['ranking_reward_at'])
        for stamp in stamps:
            _freshness(stamp, self._now(), 'ranking_freshness', max_age=60)

    def _rotate_out(self, victims, targets, version, *, diagnostics=None):
        """Validate and fence the whole batch before any exact-ID cancel."""
        from .polymarket_lp import expiration_for_review
        from .polymarket_lp_views import _next_review_at
        validated = []
        for row in victims:
            # A frozen selection may wait longer than its observation TTL.
            # Revalidate current safety facts without changing its quote/ID.
            facts = self.lp._read_candidate_facts(row, account=self._current_account, wait_for_capacity=True)
            account, direction = facts['account'], facts['direction']
            orders = [o for o in (account.get('open_orders') or []) if self.lp._order_id(o) == row['order_id']]
            if (len(orders) != 1 or orders[0].get('side') != 'BUY'
                    or orders[0].get('status') != 'LIVE' or orders[0].get('token_id') != row['token_id']
                    or _maybe_decimal(orders[0].get('price')) != _decimal(row['price'])
                    or _maybe_decimal(orders[0].get('size_matched')) != ZERO
                    or self.lp._queue_row_remaining(orders[0]) != _decimal(row['quantity'])):
                raise ValueError('rotation_order_changed')
            _freshness(direction['market'].get('metadata_checked_at'), self._now(), 'metadata_freshness', max_age=60)
            _freshness(direction['market'].get('fees_checked_at'), self._now(), 'fees_freshness', max_age=60)
            validated.append({**row, 'ranking_account': account,
                'ranking_book_at': direction['book']['received_at'],
                'ranking_reward_at': direction.get('reward_checked_at')})
        victims = validated
        with self._send_barrier():
            lock = self.execution._acquire_global_lock()
            if lock is None:
                raise ValueError('execution_lock')
            try:
                with self.lp._mutex:
                    state = self.state()
                    if not state['desired_running'] or state['admission_block_reasons'] or state['trading_config_version'] != version:
                        raise ValueError(state['reason'] or 'config_version_changed')
                    if self.store.active_execution() is not None:
                        raise ValueError('active_execution')
                    expiration_for_review(_next_review_at(self._now()), now=self._now())
                    for target in victims:
                        self._ranking_fresh(target)
                        if self._resting_buy(target):
                            self._rotation_session(target, diagnostics=diagnostics, account=target['ranking_account'])
                    capital = sum((_decimal(t['minimum_order_estimate']['capital_usd']) for t in targets
                                   if not t.get('retained_constraint')), ZERO)
                    released_ids = {self._buy_identity(r) for r in [*victims, *targets] if self._resting_buy(r)}
                    available = _decimal(state['funds']['spendable_usd']) + sum(
                        (_decimal(i['reserved_usd']) for i in [*state['intents'], *state.get('account_buys', [])]
                         if not reservation_is_released(i, state['account_id']) and self._buy_identity(i) in released_ids), ZERO)
                    if capital > available:
                        raise ValueError('top_yield_funds_insufficient')
                    def record(d):
                        for row in victims:
                            intent = (d.setdefault('account_rotations', {}).setdefault(row['order_id'], {
                                          k: deepcopy(v) for k, v in row.items()
                                          if k not in ('ranking_account', 'ranking_book_at', 'ranking_reward_at', 'minimum_order_estimate')})
                                      if row.get('account_order') else d['intents'][row['intent_id']])
                            intent.update(state='canceling', filled_quantity_at_rotation=str(row.get('filled_quantity', 0)),
                                          rotation_requested_at=self._stamp(),
                                          rotation_last_attempt_at=self._stamp(), rotation_cancel_acknowledged=False)
                            if not row.get('account_order'):
                                self._event(d, intent, 'rotation_requested', occurred_at=self._stamp(),
                                    target_conditions=[t['condition_id'] for t in targets],
                                    previous_yield=(row.get('minimum_order_estimate') or {}).get('yield_pct_per_hour'))
                    self._update(record)
                    attempts = self.lp.begin_order_cancel([r['order_id'] for r in victims])
                    for row in victims:
                        self._mark_rotation_cancel_requested(row)
            finally:
                self.execution._release_global_lock(lock)
            actions = []
            for row in victims:
                acknowledged = self._send_rotation_cancel(row, [a for a in attempts if a[2]['order_id'] == row['order_id']])
                actions.append(dict(condition_id=row['condition_id'], order_id=row['order_id'], state='canceling',
                                    reason='yield_rotation' if acknowledged else 'rotation_cancel_unknown'))
            return actions

    @staticmethod
    def _rotation_record(document, row):
        if row.get('account_order'):
            return document['account_rotations'][row['order_id']]
        return document['intents'][row['intent_id']]

    def _mark_rotation_cancel_requested(self, row):
        session = self.store.lp_session(row['session_id'])
        if session.get('entry_order_id') == row['order_id']:
            patch = {'entry_cancel_requested': True}
        else:
            patch = {'augment_cancel_requested': sorted(set(session.get('augment_cancel_requested') or []) | {row['order_id']})}
        if row.get('account_order'):
            # A per-ID yield cancel need not end the same-price protection
            # episode. Keep its baseline and gate anchored to a surviving BUY.
            # The whole cancel batch is already registered, so another victim
            # cannot become the replacement anchor before its flag is written.
            cancel_ids = {row['order_id']}
            for action in self.store.lp_actions(row['session_id']):
                if (action.get('state') in ('pending', 'unknown', 'accepted')
                        and submission_action_kind(action) == 'cancel'):
                    if action.get('order_id'):
                        cancel_ids.add(str(action['order_id']))
                    targets = action.get('targets') or ()
                    cancel_ids.update([targets] if isinstance(targets, str) else map(str, targets))
            pending_session = {**session, **patch}
            history = self.lp._order_history(session)
            buckets = self.lp._queue_protection_levels(session)
            changed = False
            for bucket in buckets.values():
                if bucket.get('order_id') != row['order_id'] or bucket.get('state') not in ('registered', 'monitoring'):
                    continue
                price = _maybe_decimal(bucket.get('baseline_price'))
                survivors = sorted(oid for oid, order in history.items()
                    if oid not in cancel_ids and not self.lp._order_cancel_requested(pending_session, oid)
                    and order.get('side') == 'BUY' and order.get('status') == 'LIVE'
                    and str(order.get('token_id')) == str(session.get('token_id'))
                    and price is not None and _maybe_decimal(order.get('price')) == price
                    and _maybe_decimal(order.get('size_matched')) == ZERO
                    and (self.lp._queue_row_remaining(order) or ZERO) > ZERO)
                if survivors:
                    bucket['order_id'] = survivors[0]
                    changed = True
            if changed:
                patch['queue_protection'] = {**session['queue_protection'], 'version': 2, 'levels': buckets}
        session = self.store.lp_update_session(row['session_id'], patch=patch)
        self.lp._mark_group_buckets_canceling(session, 'yield_rotation')

    def _send_rotation_cancel(self, intent, attempts):
        # Caller holds the send barrier; no global lock, LP mutex or DB transaction.
        try:
            acknowledged = self.lp._cancel_order(intent['order_id'], attempts=attempts)
        except Exception:
            acknowledged = False
        self._update(lambda d: self._rotation_record(d, intent).update(rotation_cancel_acknowledged=acknowledged))
        return acknowledged

    def _retry_rotation_cancel(self, intent):
        """At most one retry per new authoritative LIVE receipt; never free a slot."""
        try:
            account = self.lp.exchange.lp_account_snapshot()
        except Exception:
            return
        try:
            if not isinstance(account, dict):
                return
            receipts = account.get('open_orders')
            if (not isinstance(receipts, (list, tuple))
                    or any(not isinstance(o, dict) for o in receipts)):
                return
            wallet = str(account.get('wallet_address') or '').strip().casefold()
            checked_at = _timestamp(account.get('checked_at'))
            _freshness(checked_at, self._now(), 'account_freshness', max_age=60)
            orders = [o for o in receipts if self.lp._order_id(o) == intent['order_id']]
            if (not wallet or hashlib.sha256(wallet.encode()).hexdigest() != self.execution._lp_account_id()
                    or account.get('authenticated') is not True or account.get('open_orders_complete') is not True
                    or account.get('positions_complete') is not True or len(orders) != 1):
                return
            order = orders[0]
            matched = _maybe_decimal(order.get('size_matched'))
            if (order.get('status') != 'LIVE' or order.get('side') != 'BUY'
                    or order.get('token_id') != intent['token_id'] or order.get('fill_quantity_known') is False
                    or _maybe_decimal(order.get('original_size')) != _decimal(intent['quantity'])
                    or _maybe_decimal(order.get('price')) != _decimal(intent['price'])
                    or matched is None or not ZERO <= matched < _decimal(intent['quantity'])):
                return
            with self._send_barrier():
                lock = self.execution._acquire_global_lock()
                if lock is None:
                    return
                try:
                    with self.lp._mutex:
                        _freshness(checked_at, self._now(), 'account_freshness', max_age=60)
                        current = self._rotation_record(self._read(), intent)
                        session = self.store.lp_session(intent['session_id'])
                        if (current.get('rotation_settled_at')
                                or current['state'] in ('terminal', 'aborted', 'rejected') or not session
                                or not self.state()['desired_running']
                                or session.get('stop_requested') or session.get('stop_loss_latched')
                                or session.get('order_identity_conflict')
                                or self.lp.entry_send_inflight(intent['session_id'])
                                or has_independent_unresolved_buy_action(session, self.store.lp_actions(intent['session_id']))
                                or any(b.get('state') not in ('registered', 'monitoring')
                                       and not (b.get('state') == 'canceling' and b.get('cancel_reason') == 'yield_rotation')
                                       for b in self.lp._queue_protection_levels(session).values())
                                or any(session.get(k) != intent[k] for k in ('condition_id', 'token_id'))
                                or intent['order_id'] not in self.lp._session_order_ids(session)
                                or self.lp._order_history(session).get(intent['order_id'], {}).get('status') != 'LIVE'
                                or checked_at <= _timestamp(current['rotation_last_attempt_at'])):
                            return
                        self._update(lambda d: self._rotation_record(d, intent).update(rotation_last_attempt_at=self._stamp()))
                        attempts = self.lp.begin_order_cancel((intent['order_id'],))
                        self._mark_rotation_cancel_requested(intent)
                finally:
                    self.execution._release_global_lock(lock)
                self._send_rotation_cancel(intent, attempts)
        except (ValueError, RuntimeError, OSError):
            return

    def _settle_rotations(self):
        retries = []
        def apply(d):
            reason = None
            for intent in d['intents'].values():
                if reservation_is_released(intent, d['account_id']):
                    if intent.get('rotation_requested_at') and not intent.get('rotation_settled_at') and intent.get('order_id'):
                        # Preserve an already requested exact-ID cancellation
                        # when its financial reservation becomes covered.
                        rotation = {k: deepcopy(v) for k, v in intent.items()
                                    if k in ('order_id', 'session_id', 'condition_id', 'token_id', 'price', 'quantity',
                                             'filled_quantity', 'state') or k.startswith('rotation_')}
                        rotation['account_order'] = True
                        d.setdefault('account_rotations', {}).setdefault(intent['order_id'], rotation)
                    continue
                if not intent.get('rotation_requested_at') or intent.get('rotation_settled_at'):
                    continue
                if intent['state'] != 'terminal' or intent.get('financial_status') != 'known':
                    reason = reason or 'rotation_awaiting_reconciliation'
                    if intent['state'] not in ('terminal', 'aborted', 'rejected'):
                        retries.append(deepcopy(intent))
                    continue
                intent['rotation_settled_at'] = self._stamp()
                filled = _decimal(intent.get('filled_quantity', 0))
                self._event(d, intent, 'rotation_ended', occurred_at=self._stamp(), filled_quantity=str(filled))
                if filled:
                    reason = 'rotation_filled'
            account, buys, account_reasons = self._account_projection_facts(d)
            live_ids = {row['order_id'] for row in buys}
            for order_id, rotation in d.get('account_rotations', {}).items():
                if rotation.get('rotation_settled_at'):
                    continue
                if account is None or account_reasons or order_id in live_ids:
                    reason = reason or 'rotation_awaiting_reconciliation'
                    if order_id in live_ids and not account_reasons:
                        retries.append(deepcopy(rotation))
                    continue
                # Complete account absence settles the slot; account inventory
                # retains any capital from fills. A fill aborts this replacement
                # round, without asking the inventory lane to sell anything.
                session = self.store.lp_session(rotation['session_id'])
                history = self.lp._order_history(session or {}).get(order_id, {})
                filled = _maybe_decimal((account.get('order_fills') or {}).get(order_id))
                if filled is None:
                    filled = _maybe_decimal(history.get('size_matched'))
                if filled is None:
                    reason = reason or 'rotation_awaiting_reconciliation'
                    continue
                rotation.update(state='terminal', rotation_settled_at=self._stamp(), filled_quantity=str(filled))
                if filled > _decimal(rotation.get('filled_quantity_at_rotation', 0)):
                    reason = 'rotation_filled'
            return reason
        reason = self._update(apply)
        for intent in retries:
            self._retry_rotation_cancel(intent)
        return reason

    def _deliver_attention(self, intent_id: str, *, recovery: bool = False) -> None:
        """Freeze channel envelopes before I/O and retain results until DB ack."""
        with self._attention_delivery_lock:
            document = self._read()
            selected = document['intents'].get(intent_id)
            due = 'attention_recovery_due' if recovery else 'attention_due'
            if (not selected or selected.get('account_baseline_archive')
                    or reservation_is_released(selected, document['account_id']) or not selected.get(due)):
                return
            reason = selected.get('reconcile_error') or selected.get('reconcile_reason') or 'unknown'
            if not recovery and self.lp._attention_internal_wait(reason):
                return
            group_key = 'attention_recovery_delivery_group' if recovery else 'attention_delivery_group'
            group = selected.get(group_key)
            batch_key = 'attention_recovery_delivery_batches' if recovery else 'attention_delivery_batches'
            attempt_key = 'attention_recovery_attempted_channels' if recovery else 'attention_attempted_channels'
            success_key = 'attention_recovery_delivered_channels' if recovery else 'attention_delivered_channels'

            def channel_state(intent):
                episode = str(intent.get('attention_episode') or intent.get('attention_since'))
                key = (intent['intent_id'], recovery, episode)
                notifier = self.execution._notifier
                targets = notifier._notifiers if isinstance(notifier, CompositeNotifier) else (notifier,)
                attempted = set(intent.get(attempt_key) or ())
                delivered = set(intent.get(success_key) or ())
                cached = self._attention_delivery_results.get(key)
                if recovery:
                    attempted = set(intent.get('attention_delivered_channels') or attempted
                        or (cached[0] if cached else ()) or {_notifier_channel(target) for target in targets})
                    delivered.intersection_update(attempted)
                elif not attempted:
                    attempted = {_notifier_channel(target) for target in targets}
                if cached:
                    matching = matching_batch_channels(cached, intent.get(batch_key) or {})
                    if matching is None:
                        if not recovery:
                            attempted.update(cached[0])
                        delivered.update(cached[1] & attempted)
                    else:
                        delivered.update(cached[1] & attempted & matching)
                return key, attempted, delivered
            rows = []
            for intent in document['intents'].values():
                if (intent.get('account_baseline_archive') or reservation_is_released(intent, document['account_id'])
                        or not intent.get(due) or not (intent.get('attention_episode') or intent.get('attention_since'))):
                    continue
                if group and intent.get(group_key) != group:
                    continue
                if not group and intent.get(group_key):
                    continue
                if not recovery and (intent.get('reconcile_error') or intent.get('reconcile_reason') or 'unknown') != reason:
                    continue
                retry = intent.get('attention_send_retry_at')
                if retry and self._now() < _timestamp(retry, name='attention_retry'):
                    continue
                if recovery:
                    known = intent.get('financial_status') == 'known' and not intent.get('reconcile_error') and not intent.get('reconcile_reason')
                    proof = self.store.lp_session(intent['session_id']) or {}
                    observation = {**intent, **{k: v for k, v in proof.items() if k.startswith('attention_verif')}}
                    ready, patch = self.lp._attention_recovery_ready(observation, prefix='attention', financial_known=known)
                    if patch:
                        def remember(d):
                            current = d['intents'].get(intent['intent_id'])
                            if (current and not current.get('account_baseline_archive') and not reservation_is_released(current, d['account_id'])
                                    and current.get('attention_episode') == intent.get('attention_episode')):
                                current.update(patch)
                        self._update(remember)
                    if not ready:
                        continue
                rows.append(intent)
            if not rows:
                return
            group = group or uuid.uuid5(uuid.NAMESPACE_URL, str(recovery) + ':' + ':'.join(
                sorted(str(i['intent_id']) + ':' + str(i.get('attention_episode') or i.get('attention_since')) for i in rows)
            )).hex
            # Identity lookup may lazily initialize the metadata cache. Resolve
            # it before taking the document's SQLite write transaction.
            display_proofs = {row['intent_id']: self.store.lp_session(row['session_id']) or {} for row in rows}
            displays = {row['intent_id']: self.lp._queue_protection_identity(
                {**row, **display_proofs[row['intent_id']]}) for row in rows}
            def claim(d):
                claimed = []
                if d['account_id'] != document['account_id'] or d['account_id'] != self.execution._lp_account_id():
                    return [], {}, []
                for before in rows:
                    current = d['intents'].get(before['intent_id'])
                    episode = before.get('attention_episode') or before.get('attention_since')
                    if (not current or current.get('account_baseline_archive') or reservation_is_released(current, d['account_id']) or not current.get(due)
                            or (current.get('attention_episode') or current.get('attention_since')) != episode
                            or any(current.get(name) != before.get(name) for name in ('session_id', 'condition_id', 'token_id'))):
                        continue
                    retry = current.get('attention_send_retry_at')
                    if (current.get(group_key) != before.get(group_key)
                            or (retry and self._now() < _timestamp(retry, name='attention_claim_retry'))):
                        continue
                    proof = self.store.lp_session(current['session_id']) or {}
                    if (not proof or proof.get('account_baseline_archive') or proof.get('state') == 'account_baseline_archived'
                            or proof.get('account_id') != display_proofs[before['intent_id']].get('account_id')
                            or any(proof.get(name) not in (None, current.get(name)) for name in ('condition_id', 'token_id'))):
                        continue
                    if recovery and (current.get('financial_status') != 'known' or current.get('reconcile_error') or current.get('reconcile_reason')):
                        continue
                    if recovery:
                        proof = self.store.lp_session(current['session_id']) or {}
                        observation = {**current, **{k: v for k, v in proof.items() if k.startswith('attention_verif')}}
                        ready, _ = self.lp._attention_recovery_ready(observation, prefix='attention', financial_known=True)
                        if proof.get('account_baseline_archive') or not ready:
                            continue
                    if not recovery and (current.get('reconcile_error') or current.get('reconcile_reason') or 'unknown') != reason:
                        continue
                    if not recovery and self.lp._attention_internal_wait(current.get('reconcile_error') or current.get('reconcile_reason')):
                        continue
                    claimed.append(deepcopy(current))
                if not claimed:
                    return [], {}, []
                sessions = {}
                def session_proof(sid):
                    if sid not in sessions:
                        sessions[sid] = self.store.lp_session(sid) or {}
                    return sessions[sid]

                def member_for(row):
                    proof = session_proof(row['session_id'])
                    display = displays[row['intent_id']]
                    return {
                        'id': row['intent_id'], 'session_id': row['session_id'],
                        'episode': str(row.get('attention_episode') or row.get('attention_since')),
                        'account_id': d['account_id'], 'session_account_id': proof.get('account_id'),
                        'condition_id': row.get('condition_id'),
                        'token_id': row.get('token_id'), 'reason': reason,
                        'render': {'market_title': display['title'], 'market_url': display['url'],
                            'outcome': display['outcome'], 'condition_id': display['condition_id'],
                            'token_id': display['token_id'],
                            'queue_protection': {'data_failures': (proof.get('queue_protection') or {}).get('data_failures', 0)}},
                    }

                def member_state(member, batch, channel):
                    current = d['intents'].get(member['id'])
                    proof = session_proof(member['session_id'])
                    if (not current or not proof or current.get('account_baseline_archive') or reservation_is_released(current, d['account_id']) or proof.get('account_baseline_archive')
                            or proof.get('state') == 'account_baseline_archived'
                            or d['account_id'] != member['account_id']
                            or self.execution._lp_account_id() != member['account_id']
                            or proof.get('account_id') != member.get('session_account_id')
                            or any(current.get(name) != member.get(name) for name in ('session_id', 'condition_id', 'token_id'))
                            or any(proof.get(name) not in (None, member.get(name)) for name in ('condition_id', 'token_id'))):
                        return 'retire'
                    episode = current.get('attention_episode') or current.get('attention_since')
                    saved = (current.get(batch_key) or {}).get(channel) or {}
                    acknowledged = (recovery and not episode and not current.get(due)
                                    and saved.get('id') == batch['id'])
                    if str(episode) != member['episode'] and not acknowledged:
                        return 'retire'
                    if recovery:
                        if (current.get('financial_status') != 'known' or current.get('reconcile_error')
                                or current.get('reconcile_reason') or proof.get('state') == 'needs_attention'):
                            return 'defer'
                        if acknowledged:
                            return 'acknowledged'
                        _, required, delivered = channel_state(current)
                        if channel not in required:
                            return 'retire'
                        if channel in delivered:
                            return 'acknowledged'
                        observation = {**current, **{k: v for k, v in proof.items() if k.startswith('attention_verif')}}
                        ready, _ = self.lp._attention_recovery_ready(observation, prefix='attention', financial_known=True)
                        return 'valid' if ready else 'defer'
                    current_reason = current.get('reconcile_error') or current.get('reconcile_reason') or 'unknown'
                    if current.get('attention_recovered_at') or current_reason != member['reason']:
                        return 'retire'
                    _, _, delivered = channel_state(current)
                    return 'acknowledged' if channel in delivered else 'valid'

                def render(members):
                    title, message, voice = self.lp._attention_notice(
                        [member['render'] for member in members], recovery=recovery,
                        reason=members[0]['reason'], funds=True)
                    if not recovery and len(members) == 1:
                        title = 'LP 核对持续失败'
                    return title, message, voice

                outcomes = {}
                pending = {}
                for row in claimed:
                    key, attempted, delivered = channel_state(row)
                    outcomes[key] = (attempted, delivered)
                    pending[row['intent_id']] = attempted - delivered
                deliveries, assignments = plan_notification_batches(
                    rows={row['intent_id']: row for row in claimed}, pending=pending,
                    stored={iid: row.get(batch_key) or {} for iid, row in d['intents'].items()},
                    member_for=member_for, member_state=member_state, render=render)
                for iid, batches in assignments.items():
                    current = d['intents'][iid]
                    current[batch_key] = {**(current.get(batch_key) or {}), **deepcopy(batches)}
                for before in claimed:
                    current = d['intents'][before['intent_id']]
                    episode = before.get('attention_episode') or before.get('attention_since')
                    current.update(attention_episode=str(episode), attention_sending=True,
                        attention_send_retry_at=(self._now()+timedelta(seconds=60)).isoformat())
                    current[group_key] = group
                    key = (before['intent_id'], recovery, str(episode))
                    attempted, delivered = outcomes[key]
                    outcomes[key] = ChannelDeliveryResult(attempted, delivered, batch_ids={
                        channel: batch['id'] for channel, batch in (current.get(batch_key) or {}).items()
                        if channel in attempted})
                return claimed, outcomes, deliveries
            rows, outcomes, deliveries = self._update(claim)
            if not rows:
                return
            for key, outcome in outcomes.items():
                self._attention_delivery_results[key] = outcome
            delivery_unknown = False
            for delivery in deliveries:
                batch = delivery['batch']
                attempts = ()
                try:
                    with notification_delivery_episode('lp-auto:' + batch['id']):
                        attempts = send_notification_with_results(self.execution._notifier,
                            batch['title'], batch['message'], channels=delivery['channels'])
                except Exception:
                    delivery_unknown = True
                for key, (attempted, delivered_channels) in outcomes.items():
                    relevant = [attempt for attempt in attempts
                                if key[0] in delivery['recipients'].get(attempt.channel, ())]
                    attempted.update(attempt.channel for attempt in relevant)
                    delivered_channels.update(attempt.channel for attempt in relevant if attempt.success)
            self._finish_attention_delivery(outcomes, recovery=recovery, delivery_unknown=delivery_unknown)

    def _finish_attention_delivery(self, outcomes, *, recovery, delivery_unknown=False):
        def apply(d):
            for (current_id, _, episode), result in outcomes.items():
                attempted, delivered_channels = result
                attempted_key = 'attention_recovery_attempted_channels' if recovery else 'attention_attempted_channels'
                delivered_key = 'attention_recovery_delivered_channels' if recovery else 'attention_delivered_channels'
                intent=d['intents'].get(current_id)
                if (intent is None or intent.get('account_baseline_archive') or reservation_is_released(intent, d['account_id'])
                        or intent.get('attention_episode') != str(episode)):
                    continue
                batch_key = 'attention_recovery_delivery_batches' if recovery else 'attention_delivery_batches'
                matching = matching_batch_channels(result, intent.get(batch_key) or {})
                if matching is not None and result.batch_ids:
                    if not matching:
                        continue
                    delivered_channels = set(intent.get(delivered_key) or ()) | (delivered_channels & matching)
                if recovery and intent.get('attention_delivered_channels'):
                    attempted = set(intent['attention_delivered_channels'])
                    delivered_channels = delivered_channels & attempted
                delivered = bool(attempted) and delivered_channels >= attempted
                intent['attention_sending']=False
                intent[attempted_key]=sorted(attempted)
                intent[delivered_key]=sorted(delivered_channels)
                if delivery_unknown:
                    intent['attention_delivery_unknown']=True
                if recovery:
                    intent['attention_recovery_due']=not delivered
                    if delivered:
                        for key in ('attention_since','attention_notified','attention_due','attention_episode',
                                    'attention_recovery_due','attention_send_error','attention_send_retry_at',
                                    'attention_delivery_unknown',
                                    'attention_attempted_channels','attention_delivered_channels',
                                    'attention_recovery_attempted_channels','attention_recovery_delivered_channels',
                                    'attention_recovered_at', 'attention_delivery_group',
                                    'attention_recovery_delivery_group', 'attention_recovery_ready_since',
                                    'attention_recovery_first_checked_at'):
                            intent.pop(key,None)
                    else:
                        intent['attention_send_error']='notification_delivery_failed'
                else:
                    if intent.get('attention_recovered_at'):
                        intent['attention_due']=False
                        intent['attention_recovery_due']=bool(delivered_channels)
                        intent.pop('attention_send_retry_at',None)
                        continue
                    intent['attention_due']=not delivered
                    if delivered:
                        intent['attention_notified']=True
                        intent.pop('attention_send_error',None)
                        intent.pop('attention_delivery_unknown',None)
                        if (
                            intent.get('financial_status') == 'known'
                            and not intent.get('reconcile_error')
                            and not intent.get('reconcile_reason')
                        ):
                            intent['attention_recovery_due']=True
                    else:
                        intent['attention_notified']=False
                        intent['attention_send_error']='notification_delivery_failed'
        self._update(apply)
        for key in outcomes:
            self._attention_delivery_results.pop(key, None)

    def flush_attention(self, session_id: str | None = None) -> None:
        """Deliver persisted notices only after their fact transaction commits."""
        document=self._read()
        acknowledged = False
        for key, result in list(self._attention_delivery_results.items()):
            intent_id, recovery, episode = key
            current = document['intents'].get(intent_id)
            if (not current or current.get('account_baseline_archive') or reservation_is_released(current, document['account_id'])
                    or current.get('attention_episode') != episode):
                self._attention_delivery_results.pop(key, None)
                continue
            if session_id is not None and current.get('session_id') != session_id:
                continue
            retry = current.get('attention_send_retry_at')
            if not retry or self._now() >= _timestamp(retry, name='attention_ack_retry'):
                self._finish_attention_delivery({key: result}, recovery=recovery)
                acknowledged = True
        if acknowledged:
            document=self._read()
        intents=document['intents'].values()
        selected=[i for i in intents if not i.get('account_baseline_archive') and not reservation_is_released(i, document['account_id'])
                  and (session_id is None or i.get('session_id')==session_id)]
        for intent in selected:
            if intent.get('attention_recovery_due'):
                self._deliver_attention(intent['intent_id'],recovery=True)
            elif intent.get('attention_due'):
                self._deliver_attention(intent['intent_id'])
                current = self._read()['intents'].get(intent['intent_id'], {})
                if current.get('attention_recovery_due'):
                    self._deliver_attention(intent['intent_id'],recovery=True)

    def _mark_attention(self, i: dict, error: object, session: Mapping[str, object], now: datetime) -> None:
        if (
            i.get('attention_recovery_due')
            or i.get('attention_recovered_at')
        ) and i.get('attention_episode'):
            # A genuinely recovered fault followed by a new failure is
            # a new episode.  Reset its timer/channels; the blocked old
            # recovery completion is fenced by the previous identity.
            for key in (
                'attention_episode', 'attention_notified', 'attention_due',
                'attention_sending', 'attention_send_error',
                'attention_send_retry_at', 'attention_delivery_unknown',
                'attention_attempted_channels', 'attention_delivered_channels',
                'attention_recovery_attempted_channels',
                'attention_recovery_delivered_channels',
                'attention_recovered_at', 'attention_delivery_group',
                'attention_recovery_delivery_group', 'attention_recovery_ready_since',
                'attention_recovery_first_checked_at',
            ):
                i.pop(key, None)
            i['attention_since'] = now.isoformat()
            i['attention_episode'] = f"fault:{now.isoformat()}"
            i['attention_notified'] = False
            i['attention_due'] = False
        i.pop('attention_recovery_due',None)
        reason=str(error or 'unknown')
        previous=i.get('reconcile_retry_at') if i.get('reconcile_error')==reason else None
        snapshot={}
        if reason == 'account_read_cooling_down':
            scheduled=_maybe_datetime(session.get('reconcile_retry_at'))
        elif reason == 'market_read_cooling_down':
            scheduled=self.lp.market_read_retry_at(str(session.get('condition_id') or ''))
        else:
            scheduler=getattr(self.execution,'_lp_auto_scheduler',None)
            snapshot=scheduler.snapshot() if scheduler is not None and callable(getattr(scheduler,'snapshot',None)) else {}
            try:
                scheduled=_timestamp(snapshot.get('next_check_at'),name='scheduler_next_check_at') if snapshot.get('next_check_at') else None
            except ValueError:
                scheduled=None
        retry_at,retry_source=_reconcile_retry_plan(reason,now,previous,scheduled)
        if scheduled is None and snapshot.get('check_in_progress') is True:
            retry_at,retry_source=None,'scheduler_check_in_progress'
        if reason in {'facts_read_capacity', 'facts_read_in_progress'}:
            retry_at, retry_source = None, reason
        i['reconcile_retry_source']=retry_source
        if reason != 'execution_lock':
            i['financial_status']='unknown'
        i['publication_pending']=True
        i['reconcile_reason']=reason
        i['reconcile_error']=reason
        if retry_at is None:
            i.pop('reconcile_retry_at',None)
        else:
            i['reconcile_retry_at']=retry_at.isoformat()
        i['manual_attention']=reason in {'account_identity_mismatch','credential_invalid'}
        i.setdefault('attention_since',now.isoformat())
        i.setdefault('attention_episode',i['attention_since'])
        i.setdefault('attention_notified',False)
        if (now-_timestamp(i['attention_since'],name='attention_since')).total_seconds() >= 300:
            send_retry=i.get('attention_send_retry_at')
            due_now = not i.get('attention_notified') and (
                not send_retry or now >= _timestamp(send_retry,name='attention_send_retry_at'))
            i['attention_due']=bool(due_now or i.get('attention_sending')) and not self.lp._attention_internal_wait(reason)

    def _record_session(self, intent_id, session, *, error=None, connection=None):
        if connection is None:
            # Receipt callers may race a cancel after reading the session.
            # Derive their ledger update from the same SQLite write snapshot.
            with self.store._transaction() as connection:
                row = connection.execute('SELECT * FROM lp_sessions WHERE session_id=?',
                                         (session['session_id'],)).fetchone()
                current = self.store._lp_row_result(row) if row else session
                result = self._record_session(intent_id, current, connection=connection,
                                              error=error if row else 'session_missing')
            # Never wait on notification channels while a facts worker or the
            # ledger write is occupied; the durable due flag wakes the LP lane.
            self.lp._schedule_session_attention(str(current['session_id']))
            return result
        def apply(d):
            i=d['intents'][intent_id]
            if reservation_is_released(i, d['account_id']):
                return
            now=self._now()
            if error:
                if i.get('settled') and i.get('financial_status')=='known':
                    return
                self._mark_attention(i,error,session,now)
                return
            i['attention_due']=False
            if session.get('order_identity_conflict'):
                i.update(state='unknown',financial_status='unknown',reconcile_reason='order_identity_conflict',
                    order_identity_conflict=session['order_identity_conflict'])
                self._mark_attention(i,'order_identity_conflict',session,now)
                self._event(d,i,'unknown',**session['order_identity_conflict'])
                return
            order_id=session.get('entry_order_id')
            if order_id:
                if any(other.get('order_id')==order_id and other['intent_id']!=intent_id for other in d['intents'].values()):
                    i.update(state='unknown',financial_status='unknown',reconcile_reason='duplicate_order_identity')
                    self._mark_attention(i,'duplicate_order_identity',session,now)
                    i['manual_attention']=True
                    return
                i['order_id']=order_id
            status=session.get('submit_status')
            if session.get('state')=='entry_rejected':
                i.update(state='rejected',reserved_usd='0',financial_status='known')
                self._event(d,i,'rejected',occurred_at=session.get('submit_receipt_at'))
                return
            if not order_id:
                actions=self.store.lp_actions(str(session['session_id']), connection=connection)
                entry=next((a for a in actions if a['action_key']==self.lp._action_key(
                    str(session['session_id']), 'entry-submit')), {})
                # No venue ID is expected while this process still prepares
                # the exact entry. The action can lag the session's POST marker.
                if (i['state']=='reserved' and session['session_id']==i['session_id']
                        and self.lp.entry_send_inflight(i['session_id'])
                        and session.get('state')=='entry_submit_pending'
                        and session.get('post_started') in (None, False)
                        and session.get('submit_stage') in (None, 'preparing')
                        and entry.get('state')=='pending' and entry.get('role')=='entry'
                        and entry.get('side')=='BUY' and not entry.get('order_id')
                        and entry.get('token_id')==session.get('token_id')==i['token_id']
                        and entry.get('submit_stage')=='preparing' and entry.get('post_started') is False
                        and not has_independent_unresolved_action(session, actions)):
                    return
                i.update(state='unknown',financial_status='unknown',reconcile_reason='missing_reliable_order_id')
                self._mark_attention(i,'missing_reliable_order_id',session,now)
                self._event(d,i,'unknown')
                return
            history=self.lp._order_history(session)
            entry=history.get(order_id,{})
            order_state=str(entry.get('status') or '').upper()
            if order_state in ('', 'UNKNOWN'):
                i.update(state='unknown', financial_status='unknown', reconcile_reason='order_receipt_unknown')
                self._mark_attention(i,'order_receipt_unknown',session,now)
                self._receipt_uncertainty(d,i,True)
                return
            terminal=order_state in TERMINAL_ORDER_STATES
            quantity=_decimal(session.get('buy_filled_quantity',0))
            cost=_decimal(session.get('buy_cost',0))
            sold=_decimal(session.get('sold_quantity',0))
            revenue=_decimal(session.get('sold_revenue',0))
            buy_fees=_maybe_decimal(session.get('buy_fees')) if quantity else ZERO
            sell_fees=_maybe_decimal(session.get('sell_fees')) if sold else ZERO
            known=(buy_fees is not None and sell_fees is not None and session.get('position_reconciled') is True)
            # A fresh, unfilled accepted order needs no position/fee inference.
            if quantity==0 and sold==0 and not session.get('account_checked_at') and order_state=='LIVE':
                known=True
            actions=self.store.lp_actions(str(session['session_id']), connection=connection)
            pending_cancel = any(a.get('state') in ('pending', 'unknown')
                                 and submission_action_kind(a) == 'cancel' for a in actions)
            i['submission_unknown']=pending_cancel or self.lp._has_unresolved_submission(session) or any(
                str(owned.get('status') or 'UNKNOWN').upper()=='UNKNOWN' for owned in history.values())
            known=known and not i['submission_unknown'] and not session.get('facts_error')
            if known:
                acquired=cost+buy_fees
                released=acquired*sold/quantity if quantity else ZERO
                i.update(realized_pnl_usd=str(revenue-sell_fees-released),inventory_cost_usd=str(acquired-released),financial_status='known')
            else:
                i['financial_status']='unknown'
            if known:
                i['reserved_usd']=str(ZERO if terminal else max(ZERO,_decimal(i['quantity'])-quantity)*_decimal(i['price']))
                i['state']='terminal' if terminal else 'canceling' if session.get('entry_cancel_requested') else 'active'
            else:
                # A terminal BUY receipt alone cannot account for unknown
                # fills/fees/positions. Keep its last reservation and slot.
                i['state']='canceling' if session.get('entry_cancel_requested') else 'unknown'
            i['filled_quantity']=str(quantity)
            reason = (None if known else session.get('facts_error')
                or next((h['read_error'] for h in history.values() if h.get('read_error') and h.get('status') == 'UNKNOWN'), None)
                or session.get('financial_block_reason')
                or ('order_receipt_unknown' if i['submission_unknown'] else 'position_or_fee_unknown'))
            i['reconcile_reason'] = reason
            i['reconcile_error'] = reason
            i['publication_pending'] = bool(reason)
            recovered=bool(i.get('attention_since')) and (
                bool(i.get('attention_notified'))
                or bool(i.get('attention_delivered_channels'))
            ) and known and reason is None
            if reason:
                self._mark_attention(i,reason,session,now)
            else:
                for key in ('reconcile_retry_at','manual_attention','attention_send_error','attention_send_retry_at'):
                    i.pop(key,None)
                i['attention_recovery_due']=recovered
                if i.get('attention_since'):
                    i['attention_recovered_at']=now.isoformat()
                else:
                    i.pop('attention_recovered_at',None)
            bindings={order_id:i}
            for action in actions:
                oid=action.get('order_id')
                if submission_action_kind(action) in {'entry', 'cancel'}:
                    continue
                owned=history.get(oid,{})
                child={**i,'intent_id':f"action:{action['action_key']}",
                    'parent_intent_id':i['intent_id'],'order_id':oid or None,
                    'side':action.get('side') or owned.get('side') or 'SELL'}
                if oid:
                    bindings[oid]=child
                values=dict(price=action.get('price',action.get('min_price',owned.get('price'))),
                    quantity=action.get('quantity',owned.get('original_size',owned.get('quantity'))))
                self._event(d,child,'intent',occurred_at=action.get('submit_requested_at') or action.get('created_at'),**values)
                status=action.get('state')
                kind='unknown' if status=='unknown' or (status=='accepted' and not oid) else status
                if kind in ('accepted','rejected','unknown'):
                    self._event(d,child,kind,identity=f'accepted:{oid}' if kind=='accepted' else None,
                        occurred_at=action.get('submit_receipt_at') or action.get('updated_at'),**values)
            for oid,owned in history.items():
                if oid not in bindings:
                    bindings[oid]={**i,'intent_id':f'order:{oid}','parent_intent_id':i['intent_id'],
                        'order_id':oid,'side':owned.get('side')}
                    self._event(d,bindings[oid],'intent',quantity=owned.get('quantity'),price=owned.get('price'))
                bound=bindings[oid]
                self._receipt_uncertainty(d,bound,str(owned.get('status') or 'UNKNOWN').upper()=='UNKNOWN')
                accepted=next((a for a in actions if a.get('order_id')==oid and a.get('state')=='accepted'
                    and submission_action_kind(a) != 'cancel'), {})
                if str(owned.get('status') or '').upper() not in ('','UNKNOWN','REJECTED'):
                    self._event(d,bound,'accepted',identity=f'accepted:{oid}',
                        occurred_at=accepted.get('submit_receipt_at') or accepted.get('updated_at'),
                        price=owned.get('price',owned.get('min_price')),quantity=owned.get('original_size',owned.get('quantity')))
                if str(owned.get('status') or '').upper() in ('CANCELED','CANCELLED','EXPIRED'):
                    self._event(d,bound,'cancel',identity=f'cancel:{oid}',occurred_at=owned.get('updated_at'),status=owned['status'])
            if session.get('entry_cancel_requested'):
                self._event(d,i,'cancel_requested',identity=f'cancel_requested:{order_id}')
            for action in actions:
                oid=action.get('order_id')
                if oid in bindings and submission_action_kind(action) == 'cancel':
                    self._event(d,bindings[oid],'cancel_requested',identity=f'cancel_requested:{oid}',
                        occurred_at=action.get('submit_requested_at') or action.get('created_at'))
            # Official trade identities retain true event time. Receipt-only
            # coverage remains one explicit unknown-time remainder per order;
            # late streams replace that remainder instead of adding to it.
            trades = session.get('trade_events') or []
            for trade in trades:
                self._event(d,bindings.get(trade['order_id'],i),'fill',identity=f"fill:{trade['trade_id']}:{trade['order_id']}",
                    occurred_at=trade.get('matched_at'), order_id=trade['order_id'],
                    trade_id=trade['trade_id'],side=trade['side'],quantity=trade['quantity'],
                    price=trade['price'],fee=trade.get('fee'),source='confirmed_trade')
            for oid, fact in (session.get('verified_order_fills') or {}).items():
                recorded=sum((_decimal(t['quantity']) for t in trades if t['order_id']==oid),ZERO)
                remainder=_decimal(fact.get('quantity',0))-recorded
                identity=f'fill:receipt:{oid}'
                if remainder>0:
                    self._event(d,bindings.get(oid,i),'fill',identity=identity,occurred_at=None,
                        order_id=oid,side=fact['side'],quantity=str(remainder),
                        price=fact.get('price'),fee=None,source='order_receipt_remainder')
                else:
                    d['events'].pop(identity,None)
            i['settled'] = (known and terminal and session.get('orders_terminal') is True
                            and quantity == sold and _decimal(session.get('residual_quantity', 0)) == 0)
            i['report_pending'] = any(e.get('session_id') == session['session_id']
                and e.get('kind') == 'fill' and not e.get('occurred_at') for e in d['events'].values())
            i['checked_at'] = session.get('facts_checked_at') or session.get('submit_receipt_at') or i['created_at']
        self._update(apply,connection=connection)

    def publish_session(self, session, *, connection, error=None):
        row=connection.execute('SELECT payload FROM lp_auto_pool WHERE singleton=1').fetchone()
        if row is None:
            return False
        document=json.loads(row[0])
        intent=next((i for i in document['intents'].values() if i['session_id']==session['session_id']),None)
        if intent is None:
            return False
        def identity(i):
            return (i.get('financial_status'), i['state'], self._funds_fresh(i),
                    i.get('reserved_usd'), i.get('inventory_cost_usd'), i.get('realized_pnl_usd'))
        before=identity(intent)
        self._record_session(intent['intent_id'],session,error=error,connection=connection)
        updated=json.loads(connection.execute('SELECT payload FROM lp_auto_pool WHERE singleton=1').fetchone()[0])
        intent=updated['intents'][intent['intent_id']]
        return not error and before != identity(intent)

    def _refresh_account_facts(self, *, account_round=None):
        from .polymarket_lp import LpAccountRoundInvalid, LpObservationWait
        reader = getattr(self.lp.exchange, 'lp_account_snapshot_shared', None)
        if not callable(reader):
            return None
        with self._account_refresh_lock:
            self._account_refresh_attempt += 1
            attempt = self._account_refresh_attempt
            prior = self._read().get('account_financial_facts')
            baseline = self._account_facts_identity(prior)
            baseline_times = {key: prior.get(key) for key in ('read_started_at', 'read_ended_at', 'checked_at')} if isinstance(prior, Mapping) else {}

        def failed(reason, *, waiting=False):
            with self._account_refresh_lock:
                if attempt == self._account_refresh_attempt:
                    self._current_account = None
                    if waiting:
                        # Immutable state lets read-only projections compare
                        # durable publication without waiting for network I/O.
                        self._account_facts_wait = dict(attempt=attempt, baseline=baseline, baseline_times=baseline_times, reason=reason)
                    else:
                        self.lp._account_order_sync_error = reason
            return False

        try:
            snapshot = (self.lp.exchange.lp_account_snapshot(account_round=account_round)
                        if account_round is not None else
                        reader(max_age_seconds=0, trade_generation_provider=self.store.lp_trade_generation))
            with self._account_refresh_lock:
                if attempt != self._account_refresh_attempt:
                    return False
                # Serialize publication with attempt admission, not the account
                # read. A late old read cannot publish over a newer wait.
                result = self.lp.register_account_snapshot(snapshot)
                if result.get('state') != 'registered':
                    reason = result.get('reason') or 'account_order_sync_unknown'
                    return failed(reason, waiting=reason in ('account_round_invalid', 'session_changed'))
                self._account_facts_wait = None
                self._current_account = snapshot
                return True
        except (LpAccountRoundInvalid, LpObservationWait) as exc:
            # The next scheduled read or a newer valid dashboard publication
            # owns recovery. Never retry internal invalidation in this round.
            reason = 'account_round_invalid' if isinstance(exc, LpAccountRoundInvalid) else str(exc)
            return failed(reason, waiting=True)
        except Exception:
            return failed('account_order_sync_unknown')

    def _reconcile_unknown(self, *, reuse=False, bounded=False):
        lease = self._begin_account_round()
        try:
            return self._reconcile_unknown_in_round(reuse=reuse, bounded=bounded, lease=lease)
        finally:
            # Launched jobs retain the round until their own publication ends.
            if lease is not None:
                lease.release()

    def _reconcile_unknown_in_round(self, *, reuse, bounded, lease):
        refreshed = self._refresh_account_facts(account_round=lease.token if lease is not None else None)
        d=self._read()
        if not d['account_id'] or d['account_id']!=self.execution._lp_account_id():
            return self._projection(d)
        intents = [i for i in d['intents'].values() if not i.get('account_baseline_archive') and not reservation_is_released(i, d['account_id'])
                   and (not i.get('settled') or i.get('attention_recovery_due'))]
        if bounded:
            # Persistent per-intent jobs: a slow read never owns the auto-round
            # barrier, and later rounds cannot stack workers for that session.
            deadline = monotonic() + 1.0
            remaining = sorted(intents, key=lambda i: i.get('checked_at') or i['created_at'])
            while remaining or self._reconcile_jobs:
                with self._reconcile_jobs_lock:
                    self._reconcile_jobs = {key: job for key, job in self._reconcile_jobs.items() if not job.done()}
                    remaining = [i for i in remaining if i['intent_id'] not in self._reconcile_jobs]
                    while remaining and len(self._reconcile_jobs) < 2:
                        intent = remaining.pop(0)
                        future = Future()
                        self._reconcile_jobs[intent['intent_id']] = future
                        if lease is not None:
                            lease.retain()
                        def reconcile(i=intent, result=future, round_lease=lease):
                            try:
                                token = round_lease.token if round_lease is not None else None
                                self._reconcile_intent(i, reuse=reuse, account_round=token)
                            except Exception as exc:
                                session = self.store.lp_session(i['session_id'])
                                if session:
                                    self._record_session(i['intent_id'], session, error=type(exc).__name__)
                            finally:
                                result.set_result(None)
                                if round_lease is not None:
                                    round_lease.release()
                        threading.Thread(target=reconcile, daemon=True, name='lp-auto-facts').start()
                    futures = list(self._reconcile_jobs.values())
                if not futures or monotonic() >= deadline:
                    break
                wait(futures, timeout=max(0, deadline - monotonic()), return_when=FIRST_COMPLETED)
        else:
            with ThreadPoolExecutor(max_workers=2) as workers:
                futures = [
                    workers.submit(
                        self._reconcile_intent,
                        i,
                        reuse=reuse,
                        account_round=lease.token if lease is not None else None,
                    )
                    for i in intents
                ]
                for future in futures:
                    future.result()
        def finish(doc):
            doc['last_checked_at']=self._stamp()
            if all(i.get('financial_status')=='known' and i['state']!='unknown' for i in doc['intents'].values()):
                doc['last_reconciled_at']=self._stamp()
        self._update(finish)
        if (refreshed is True and intents
                and 'account_financial_facts_changed' in self._account_projection_facts(self._read())[2]):
            # Reconcile may commit new receipts after the initial account
            # snapshot. Only that changed fence calls for a second read.
            self._refresh_account_facts()
        return self.state()

    def reconcile_attention(self, *, apply_lock=None, session_id=None):
        d = self._read()
        if not d['account_id'] or d['account_id'] != self.execution._lp_account_id():
            return
        for intent in d['intents'].values():
            if (intent.get('settled') and intent.get('attention_recovery_due') and not intent.get('account_baseline_archive')
                    and not reservation_is_released(intent, d['account_id'])):
                if session_id is None:
                    self.lp._schedule_session_attention(intent['session_id'], apply_lock=apply_lock)
                elif intent['session_id'] == session_id:
                    self.lp.verify_session_recovery(session_id, apply_lock=apply_lock or (
                        self.execution._acquire_global_lock, self.execution._release_global_lock))
                    return

    def reconcile_reports(self):
        # Reuse the natural-day report worker. One historical read at a time
        # leaves the other shared read slot available to active sessions.
        if not self.lp._report_lock.acquire(blocking=False):
            return
        try:
            d = self._read()
            if not d['account_id'] or d['account_id'] != self.execution._lp_account_id():
                return
            for intent in d['intents'].values():
                if intent.get('settled') and intent.get('report_pending'):
                    self._reconcile_intent(intent, reuse=True)
        finally:
            self.lp._report_lock.release()
            self.reconcile_attention(apply_lock=(self.execution._acquire_global_lock, self.execution._release_global_lock))

    def _reconcile_intent(self, i, *, reuse, account_round=None):
        if i.get('account_baseline_archive') or reservation_is_released(i, self.execution._lp_account_id()):
            return
        if i.get('settled') and i.get('attention_recovery_due'):
            self.lp.verify_session_recovery(i['session_id'], account_round=account_round,
                apply_lock=(self.execution._acquire_global_lock, self.execution._release_global_lock))
            if not i.get('report_pending'):
                return
        if i['state'] in ('aborted','rejected'):
            return
        session=self.store.lp_session(i['session_id'])
        if session is None:
            untouched = (i['state'] == 'reserved' and not i.get('order_id')
                         and not i.get('rotation_requested_at') and not self.store.lp_actions(i['session_id']))
            def missing(doc):
                intent = doc['intents'][i['intent_id']]
                reason=('rotation_session_missing' if intent.get('rotation_requested_at')
                        else 'session_missing')
                if untouched:
                    intent.update(state='aborted', reserved_usd='0', financial_status='known')
                else:
                    intent.update(state='unknown', financial_status='unknown', reconcile_reason=reason)
                    self._mark_attention(intent,reason,{'session_id':i['session_id']},self._now())
                    intent['manual_attention']=True
            self._update(missing)
            return
        if reuse and i.get('financial_status') == 'known' and not session.get('facts_error'):
            if i.get('settled'):
                if not i.get('report_pending'):
                    return
                stamp = session.get('report_checked_at') or session.get('facts_checked_at')
                if stamp and (self._now() - _timestamp(stamp, name='report_checked_at')).total_seconds() < 300:
                    return
            elif session.get('facts_checked_at') and self._funds_fresh(i):
                # Reuse the just-published tick result; the minute scheduler
                # still polls even while the 60-second funds lease is valid.
                try:
                    _freshness(session['facts_checked_at'], self._now(), 'shared_facts')
                    return
                except ValueError:
                    pass
        if session.get('order_identity_conflict'):
            self._record_session(i['intent_id'],session)
            return
        self.lp.reconcile_facts(
            i['session_id'],
            report_only=i.get('settled', False),
            account_round=account_round,
            apply_lock=(self.execution._acquire_global_lock, self.execution._release_global_lock))

    def _guard(self, intent_id, snapshot):
        d=self._read()
        i=d['intents'][intent_id]
        state=self._projection(d)
        session=self.store.lp_session(i['session_id'])
        if not session or session.get('stop_requested') or session.get('entry_cancel_requested') or session.get('stop_loss_latched'):
            raise ValueError('session_stopped_before_send')
        if i['state']!='reserved':
            raise ValueError('intent_not_reserved')
        if not d['desired_running']:
            raise ValueError('manually_paused')
        if d['trading_config_version'] != i.get('trading_config_version', i['config_version']):
            raise ValueError('config_version_changed')
        reasons=state['admission_block_reasons']
        if reasons:
            raise ValueError(reasons[0] if reasons else 'submission_unknown')
        if state['slots']['occupied']>d['target_buy_count']:
            raise ValueError('target_filled')
        if _decimal(state['funds']['deficit_usd'])>0:
            raise ValueError('strategy_funds_insufficient')
        if self._excluded(i['condition_id'],ignore=i['session_id']):
            raise ValueError('market_already_participating')
        # Candidate validation has already checked complete fresh account
        # positions/orders/cash/allowance. Recheck its freshness after signing.
        wallet=str(snapshot['account'].get('wallet_address') or '').strip().casefold()
        if not wallet or hashlib.sha256(wallet.encode()).hexdigest()!=d['account_id']:
            raise ValueError('account_identity_mismatch')
        generation = snapshot['account'].get('trade_generation')
        if generation is not None and generation != self.store.lp_trade_generation():
            raise ValueError('account_financial_facts_changed')
        _freshness(snapshot['account'].get('checked_at'),self._now(),'account_freshness',max_age=60)
        _freshness(snapshot['book'].get('received_at'),self._now(),'book_freshness',max_age=60)

    def _check_candidate_rank(self, row, snapshot, peers):
        # Qualification and execution guards still run on fresh facts. A
        # change in relative yield belongs to the next business round.
        return

    def _account_refresh_failure_reason(self):
        return (getattr(self.lp, '_account_order_sync_error', None)
            or (self._account_facts_wait or {}).get('reason')
            or (self.state()['admission_block_reasons'] or ['account_unknown'])[0])

    def _submit(self, row, round_id, index, version, *, peers=()):
        # Network preparation never owns the existing protection/apply lane.
        if self._refresh_account_facts() is False:
            reason = self._account_refresh_failure_reason()
            return {"state": "rejected", "reason": reason}, reason
        state = self.state()
        if state['slots']['occupied'] >= state['target_buy_count']:
            raise ValueError('target_filled')
        account = self._current_account
        _lp_causal_event("auto_presend_account_use", account=account, used_at=self._now())
        snapshot=self.lp._read_candidate_snapshot(
            row, now=self._now(), account=account,
            bid_level=row.get('bid_level', state.get('buy_price_level', 1)),
        )
        lock=self.execution._acquire_global_lock()
        if lock is None:
            raise ValueError('execution_lock')
        self.lp._mutex.acquire()
        released=False
        def release():
            nonlocal released
            if not released:
                released=True
                self.lp._mutex.release()
                self.execution._release_global_lock(lock)
        try:
            return self._submit_prepared(row,round_id,index,version,snapshot,release,peers=peers)
        finally:
            release()

    def _submit_prepared(self, row, round_id, index, version, snapshot, release, *, peers=()):
        from .polymarket_lp import expiration_for_review
        from .polymarket_lp_views import _next_review_at
        if self.store.active_execution() is not None:
            raise ValueError('active_execution')
        self._check_candidate_rank(row, snapshot, peers)
        fresh=self.lp._fresh_candidate_row(row,snapshot,now=self._now())
        if any(_decimal(fresh[k]) != _decimal(row[k]) for k in ('price', 'quantity')):
            raise ValueError('candidate_changed')
        request=self.lp._normalize_request({
            **fresh,
            'candidate_policy':'best_bid_minimum',
            'candidate_bid_level': fresh.get('bid_level', row.get('bid_level', 1)),
            'review_at':_next_review_at(self._now()),
        })
        facts=self.lp._validate_snapshot(request,snapshot,now=self._now(),reservations=self.lp._candidate_reservations())
        self.lp._require_lp_history(request, now=self._now())
        if self._excluded(str(row['condition_id'])):
            raise ValueError('market_already_participating')
        intent_id=f'{round_id}:{index}'
        session_id=uuid.uuid4().hex
        amount=request['price']*request['quantity']
        def reserve(d):
            state=self._projection(d)
            if intent_id in d['intents']:
                raise ValueError('intent_already_reserved')
            if not d['desired_running'] or state['admission_block_reasons'] or d['trading_config_version']!=version:
                raise ValueError(state['reason'] or 'config_version_changed')
            if state['slots']['occupied']>=d['target_buy_count']:
                raise ValueError('target_filled')
            if amount>_decimal(state['funds']['spendable_usd']):
                raise ValueError('strategy_funds_insufficient')
            i=dict(intent_id=intent_id,session_id=session_id,order_id=None,config_version=d['config_version'],
                   trading_config_version=version,
                   state='reserved',reserved_usd=str(amount),inventory_cost_usd='0',realized_pnl_usd='0',financial_status='known',
                   **{k:str(request[k]) for k in ('condition_id','market_id','token_id','outcome','price','quantity')},created_at=self._stamp())
            d['intents'][intent_id]=i
            self._event(d,i,'intent',occurred_at=self._stamp(),quantity=i['quantity'],price=i['price'])
        self._update(reserve)
        account_read_failure = None
        def post(signed, mark_post_started):
            nonlocal account_read_failure
            from .polymarket_lp import AutoEntryNotSent
            try:
                if self._refresh_account_facts() is False:
                    account_read_failure = self._account_refresh_failure_reason()
                    raise ValueError(account_read_failure)
                account = self._current_account
                _lp_causal_event("auto_send_account_use", account=account, used_at=self._now())
                latest = self.lp._read_candidate_snapshot(request, now=self._now(), ignore_session_id=session_id,
                    account=account)
                self._check_candidate_rank(row, latest, peers)
                eligible = self.lp._fresh_candidate_row(request, latest, now=self._now())
                if any(_decimal(eligible[k]) != request[k] for k in ('price','quantity')):
                    raise ValueError('candidate_changed')
                self.lp._validate_snapshot(request, latest, now=self._now(),
                    reservations=self.lp._candidate_reservations(ignore_session_id=session_id))
                self.lp._require_lp_history(request, now=self._now())
            except ValueError as exc:
                raise AutoEntryNotSent(str(exc)) from exc
            with self._send_barrier():
                lock=self.execution._acquire_global_lock()
                if lock is None:
                    raise AutoEntryNotSent('execution_lock')
                try:
                    with self.lp._mutex:
                        try:
                            self._guard(intent_id,latest)
                            if self.store.active_execution() is not None:
                                raise ValueError('active_execution')
                        except ValueError as exc:
                            raise AutoEntryNotSent(str(exc)) from exc
                        self._update(lambda d:d['intents'][intent_id].update(state='sending', financial_status='unknown', reconcile_reason='submission_pending'))
                finally:
                    self.execution._release_global_lock(lock)
                return self.lp._post_limit(signed, on_post_started=mark_post_started, side='BUY')
        result=self.lp._entry_execute(request=request,snapshot=snapshot,facts=facts,now=self._now(),
            key=f'lp-auto:{intent_id}',expiration=expiration_for_review(request['review_at'],now=self._now()),
            session_id=session_id,post=post,release_preparation_lock=release,
            apply_lock=(self.execution._acquire_global_lock,self.execution._release_global_lock))
        session=self.store.lp_session(session_id)
        if session:
            self._record_session(intent_id,session)
        else:
            self._update(lambda d:d['intents'][intent_id].update(state='aborted',reserved_usd='0'))
        return result, account_read_failure

    @contextmanager
    def _round_barrier(self):
        handle=Path(str(self.store.path)+'.lp-auto-round.lock').open('a+')
        locked=False
        try:
            try:
                fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
                locked=True
            except BlockingIOError:
                pass
            yield locked
        finally:
            if locked:
                fcntl.flock(handle,fcntl.LOCK_UN)
            handle.close()

    def reconcile_unknown(self):
        with self._round_barrier() as locked:
            return self._reconcile_unknown() if locked else {**self.state(),'round_reason':'round_in_progress'}

    def run_once(self, *, round_id=None, reuse_facts=False):
        with _lp_read_task("auto_round"), self._round_barrier() as locked:
            if not locked:
                return {**self.state(),'round_reason':'round_in_progress'}
            return self._run_once(round_id=round_id, reuse_facts=reuse_facts)

    def _plan_wait(self, kind):
        self._update(lambda d: self._set_plan_wait(d, kind))

    def _set_plan_wait(self, d, kind, *, started_at=None):
        fields = {'round': ('round_interval_seconds', 60),
                  'api': ('api_retry_interval_seconds', 60),
                  'order': ('order_check_interval_seconds', 10)}
        field, default = fields[kind]
        now = _maybe_datetime(started_at) or self._now()
        d['plan_wait'] = dict(kind=kind, started_at=now.isoformat(),
            deadline=(now + timedelta(seconds=d.get(field, default))).isoformat())

    def _save_plan(self, plan, *, reason=None, blocked=(), wait_kind=None):
        def apply(d):
            d['active_plan'] = deepcopy(plan) if not plan.get('completed_at') else None
            d['last_round'] = dict(round_id=plan['round_id'], started_at=plan['started_at'],
                checked_at=self._stamp(), completed_at=plan.get('completed_at'),
                config_version=plan['config_version'], actions=deepcopy(plan['actions']),
                targets=[{k: deepcopy(r.get(k)) for k in
                    ('condition_id', 'token_id', 'price', 'quantity', 'minimum_order_estimate',
                     'order_id', 'session_id', 'retained_constraint')} for r in plan['targets']],
                candidates=plan.get('candidates', [])[:10], candidate_count=len(plan.get('candidates', [])),
                candidate_filter=deepcopy(plan.get('diagnostics', {})), blocked=list(blocked),
                reason=reason or ('target_filled' if self._projection(d)['slots']['occupied'] >= d['target_buy_count']
                    else 'candidates_or_funds_insufficient'))
            if wait_kind:
                self._set_plan_wait(d, wait_kind, started_at=plan.get('completed_at'))
        self._update(apply)

    @staticmethod
    def _defer_unsent(plan, action, reason, session_id):
        attempts = action.setdefault('attempts', [])
        attempts.append(dict(request_id=action.get('request_id', action['action_id']),
            session_id=session_id, state='not_sent', reason=reason))
        action.update(state='pending', wait_reason=reason,
            request_index=f'{action["index"]}:retry:{len(attempts)}',
            request_id=f'{plan["round_id"]}:{action["index"]}:retry:{len(attempts)}')
        action.pop('session_id', None)
        action.pop('order_id', None)

    def _run_once(self, *, round_id=None, reuse_facts=False):
        d = self._read()
        waiting = d.get('plan_wait')
        if waiting and self._now() < _timestamp(waiting['deadline']):
            return self.state()
        self._update(lambda doc: doc.update(plan_wait=None))
        plan = d.get('active_plan')
        if callable(getattr(self.lp.exchange, 'lp_account_snapshot_shared', None)):
            refreshed = self._refresh_account_facts()
        else:
            self._reconcile_unknown(reuse=reuse_facts, bounded=True)
            refreshed = None
        state = self.state()
        if refreshed is False or state['admission_block_reasons']:
            reason = self._account_refresh_failure_reason() if refreshed is False else state['admission_block_reasons'][0]
            if plan:
                self._save_plan(plan, reason=reason, wait_kind='api')
            else:
                def pause_planning(doc):
                    doc['last_round'] = dict(round_id=round_id, checked_at=self._stamp(),
                        targets=[], actions=[], blocked=[], candidates=[], candidate_count=0, reason=reason)
                    self._set_plan_wait(doc, 'api')
                self._update(pause_planning)
            return self.state()
        rotation_reason = self._settle_rotations()
        d = self._read()
        if not state['desired_running'] or not self.execution.lp_mutation_allowed():
            self._plan_wait('order' if plan else 'round')
            return self.state()
        if plan is None:
            round_id = round_id or uuid.uuid4().hex
            if not isinstance(round_id, str) or not round_id or len(round_id) > 128:
                raise ValueError('round_id_invalid')
            if round_id in d['rounds']:
                return self.state()
            diagnostics = dict(round_id=round_id, state='not_evaluated', counts={}, reasons={},
                recheck_reasons={}, facts={}, account_sources=dict(current=0, candidate=0, unknown=0),
                round_started_at=self._stamp())
            try:
                targets, victims, candidates, blocked = self._ranked_buys(self.state(), diagnostics=diagnostics)
            except ValueError as exc:
                self._update(lambda doc: doc.update(last_round=dict(round_id=round_id,
                    checked_at=self._stamp(), actions=[], targets=[], blocked=[], reason=str(exc))))
                self._plan_wait('api')
                return self.state()
            plan = dict(round_id=round_id, started_at=self._stamp(), config_version=d['config_version'],
                trading_config_version=d['trading_config_version'],
                facts_identity=self._account_facts_identity(d.get('account_financial_facts')),
                targets=targets, victims=victims, candidates=candidates, diagnostics=diagnostics, blocked=blocked,
                actions=[dict(kind='cancel', action_id=f'{round_id}:cancel:{r["order_id"]}',
                    condition_id=r['condition_id'], order_id=r['order_id'], session_id=r['session_id'], state='pending') for r in victims]
                    + [dict(kind='buy', action_id=f'{round_id}:{index}', index=index,
                        condition_id=r['condition_id'], token_id=r['token_id'], state='pending')
                       for index, r in enumerate(targets) if not self._resting_buy(r)])
            self._update(lambda doc: doc['rounds'].update({round_id: dict(started_at=plan['started_at'])}))
            self._save_plan(plan, blocked=blocked)
        # A durable action identity is written before a request is prepared.
        # Restart never regenerates targets or substitutes another candidate.
        account, buys, account_reasons = self._account_projection_facts(d)
        current_ids = {row['order_id'] for row in buys}
        for action in plan['actions']:
            if action['kind'] != 'cancel' or action['state'] in ('success', 'rejected'):
                continue
            if account is not None and not account_reasons and action['order_id'] not in current_ids:
                # Complete current absence settles an exit even if a lock or
                # protection action prevented its local cancel registration.
                # Inventory remains priced by the same authoritative facts.
                action.update(state='success', confirmed_at=self._stamp())
                continue
            if action['state'] != 'pending':
                continue
            rotation = d.get('account_rotations', {}).get(action['order_id']) or next(
                (i for i in d['intents'].values() if i.get('order_id') == action['order_id']), {})
            if rotation.get('rotation_requested_at'):
                # Registration is durable before the first send. Recovery uses
                # newer authoritative facts, never blindly replays the batch.
                action['state'] = 'canceling'
        cancels = [a for a in plan['actions'] if a['kind'] == 'cancel' and a['state'] == 'pending']
        if cancels:
            try:
                pending_ids = {action['order_id'] for action in cancels}
                victims = [row for row in plan['victims'] if row['order_id'] in pending_ids]
                results = self._rotate_out(victims, plan['targets'], plan.get('trading_config_version', plan['config_version']),
                    diagnostics=plan['diagnostics'])
                for action, result in zip(cancels, results):
                    action.update(result)
            except ValueError as exc:
                self._save_plan(plan, reason=str(exc), blocked=plan['blocked'], wait_kind='order')
                return self.state()
        document = self._read()
        account, buys, reasons = self._account_projection_facts(document)
        live_ids = {r['order_id'] for r in buys}
        for action in plan['actions']:
            if action['kind'] == 'cancel':
                if action['state'] == 'canceling' and account is not None and not reasons and action['order_id'] not in live_ids:
                    action.update(state='success', confirmed_at=self._stamp())
                elif action['state'] == 'canceling' and account is None:
                    intent = next((i for i in document['intents'].values() if i.get('order_id') == action['order_id']), {})
                    if intent.get('state') == 'terminal' and intent.get('financial_status') == 'known' and self._funds_fresh(intent):
                        action.update(state='success', confirmed_at=self._stamp())
                continue
            if action['state'] in ('success', 'rejected'):
                continue
            if account is None and rotation_reason == 'rotation_filled':
                action.update(state='rejected', reason='rotation_filled')
                continue
            existing = document['intents'].get(action.get('request_id', action['action_id']))
            if existing:
                action.update(session_id=existing['session_id'], order_id=existing.get('order_id'))
                if existing['state'] in ('aborted', 'rejected'):
                    session = self.store.lp_session(existing['session_id']) or {}
                    reason = session.get('reason') or session.get('submit_error') or 'pre_send_rejected'
                    if session.get('submit_stage') == 'pre_send_rejected' and reason in _PLAN_TRANSIENT_WAITS:
                        self._defer_unsent(plan, action, reason, existing['session_id'])
                    else:
                        action.update(state='rejected', reason=reason)
                elif ((account is not None and not reasons and (existing.get('order_id') in live_ids
                        or reservation_is_released(existing, document['account_id'])))
                        or account is None and existing.get('state') == 'active'
                        and existing.get('financial_status') == 'known' and self._funds_fresh(existing)):
                    action.update(state='success', confirmed_at=self._stamp())
                else:
                    action['state'] = 'unknown'
                continue
            current_state = self.state()
            if (current_state['slots']['occupied'] >= d['target_buy_count']
                    and (cancels or 'account_financial_facts_changed' not in current_state['block_reasons'])):
                continue
            row = plan['targets'][action['index']]
            try:
                result, failure = self._submit(row, plan['round_id'], action.get('request_index', action['index']),
                    plan.get('trading_config_version', plan['config_version']))
                action.update(session_id=result.get('session_id'), order_id=result.get('entry_order_id'),
                              request_state=result.get('state'))
                if failure:
                    if result.get('submit_stage') == 'pre_send_rejected':
                        self._defer_unsent(plan, action, failure, result.get('session_id'))
                    self._save_plan(plan, reason=failure, blocked=plan['blocked'], wait_kind='api')
                    return self.state()
                result_reason = result.get('reason') or result.get('submit_error')
                if (result.get('submit_stage') == 'pre_send_rejected' and result_reason in _PLAN_TRANSIENT_WAITS):
                    self._defer_unsent(plan, action, result_reason, result.get('session_id'))
                else:
                    action.update(state='rejected' if result.get('state') == 'entry_rejected' else 'unknown',
                        reason=result_reason)
            except ValueError as exc:
                reason = str(exc)
                if reason in _PLAN_TRANSIENT_WAITS:
                    action['wait_reason'] = reason
                else:
                    action.update(state='rejected', reason=reason)
            self._save_plan(plan, blocked=plan['blocked'])
            if (not callable(getattr(self.lp.exchange, 'lp_account_snapshot_shared', None))
                    and self.state()['admission_block_reasons']):
                self._plan_wait('api')
                return self.state()
        if any(a['kind'] == 'buy' and a['state'] == 'unknown' for a in plan['actions']):
            refreshed = self._refresh_account_facts()
            document = self._read()
            account, buys, reasons = self._account_projection_facts(document)
            live_ids = {r['order_id'] for r in buys}
            if refreshed is False or reasons:
                self._save_plan(plan, reason=self._account_refresh_failure_reason(), blocked=plan['blocked'], wait_kind='api')
                return self.state()
            for action in plan['actions']:
                if action['kind'] != 'buy' or action['state'] != 'unknown':
                    continue
                intent = document['intents'].get(action.get('request_id', action['action_id']))
                if intent and (intent.get('order_id') in live_ids or reservation_is_released(intent, document['account_id'])
                        or account is None and intent.get('state') == 'active'
                        and intent.get('financial_status') == 'known' and self._funds_fresh(intent)):
                    action.update(state='success', order_id=intent.get('order_id'),
                        session_id=intent['session_id'], confirmed_at=self._stamp())
        complete = all(a['state'] in ('success', 'rejected') for a in plan['actions'])
        if complete:
            plan['completed_at'] = self._stamp()
        reason = (rotation_reason if complete and rotation_reason == 'rotation_filled' else
            plan['blocked'][0]['reason'] if complete and not plan['actions'] and plan['blocked']
            and self.state()['slots']['occupied'] >= d['target_buy_count'] else
            None if complete else 'rotation_awaiting_reconciliation')
        self._save_plan(plan, reason=reason, blocked=plan['blocked'], wait_kind='round' if complete else 'order')
        return self.state()

    def report_facts(self, period_start=None, period_end=None):
        d=self._read()
        state=self._projection(d)
        sessions=[session for i in d['intents'].values()
                  if (session:=self.store.lp_session(i['session_id'])) is not None]
        events=list(d['events'].values())
        account_mode = d.get('account_financial_facts') is not None
        financial=dict(status='unknown',reason=('account_historical_boundary_unavailable' if account_mode
                           else 'historical_financial_boundary_unavailable'),
                       realized_pnl_usd=None,inventory_cost_usd=None,inventory_quantity=None,
                       as_of=None,source='account_verified_facts' if account_mode else 'lp_session_verified_trades')
        if period_start is not None and period_end is not None:
            start,end=_timestamp(period_start),_timestamp(period_end)
            if start>end:
                raise ValueError('report_period_invalid')
            fills=[e for e in events if e['kind']=='fill']
            # Current account exposure has no account-wide dated boundary
            # ledger yet. Intent-only events cannot prove a zero period.
            complete=not account_mode and all(e.get('occurred_at') and e.get('fee') is not None for e in fills)
            complete=complete and state['funds']['status']=='known' and not any(i['state']=='unknown' for i in d['intents'].values())
            if complete:
                def at(cutoff):
                    pnl,inventory,quantity=ZERO,ZERO,ZERO
                    inventories=[]
                    for session in sessions:
                        selected=[f for f in fills if f['session_id']==session['session_id'] and _timestamp(f['occurred_at'])<cutoff]
                        buys=[f for f in selected if f['side']=='BUY']
                        sells=[f for f in selected if f['side']=='SELL']
                        bought=sum((_decimal(f['quantity']) for f in buys),ZERO)
                        sold=sum((_decimal(f['quantity']) for f in sells),ZERO)
                        if sold>bought:
                            raise ValueError('fill_order_inconsistent')
                        cost=sum((_decimal(f['quantity'])*_decimal(f['price'])+_decimal(f['fee']) for f in buys),ZERO)
                        revenue=sum((_decimal(f['quantity'])*_decimal(f['price'])-_decimal(f['fee']) for f in sells),ZERO)
                        consumed=cost*sold/bought if bought else ZERO
                        pnl+=revenue-consumed
                        inventory+=cost-consumed
                        quantity+=bought-sold
                        if bought>sold:
                            inventories.append(dict(session_id=session['session_id'],condition_id=session['condition_id'],
                                token_id=session['token_id'],quantity=str(bought-sold),cost_usd=str(cost-consumed)))
                    return pnl,inventory,quantity,inventories
                try:
                    prior=at(start)
                    final=at(end)
                    financial.update(status='known',reason=None,realized_pnl_usd=str(final[0]-prior[0]),
                        inventory_cost_usd=str(final[1]),inventory_quantity=str(final[2]),inventories=final[3],as_of=end.isoformat())
                except ValueError as exc:
                    financial['reason']=str(exc)
            else:
                financial['reason']=('account_historical_boundary_unavailable' if account_mode
                                     else 'trade_time_fee_or_reconciliation_unknown')
        return dict(account_id=d['account_id'],auto_run_id=d['run_id'],state=state,
                    events=events,sessions=sessions,funds=state['funds'],
                    as_of=d['last_reconciled_at'],financial_period=financial)
