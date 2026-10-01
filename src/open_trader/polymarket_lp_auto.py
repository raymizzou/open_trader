"""One bounded automatic LP round, using the existing session execution lane.

The durable document is the sole automatic attribution/allocation ledger. Venue
trade economics stay in LP sessions; projections never treat wallet cash or
estimated rewards as strategy profit.
"""
from __future__ import annotations

import fcntl
import json
import hashlib
import uuid
import threading
from time import monotonic
from contextlib import contextmanager, nullcontext
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Mapping

from .polymarket_lp_risk import (
    TERMINAL_ORDER_STATES, _account_after_reservations, _decimal as _money, _freshness, _levels,
    _maybe_decimal, _timestamp, evaluate_lp_entry, estimate_lp_target_share_yield,
)
from .daily_premarket import send_notification_with_results

ZERO = Decimal('0')


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


def minimum_order_estimate(direction, guidance, now, *, resting_quantity=ZERO):
    """Reuse reward weights, recompute the share at the actual legal quantity."""
    market = direction['market']
    estimate = estimate_lp_target_share_yield(
        direction['book'], price=_decimal(guidance['price']),
        reward_min_size=_decimal(market['reward_min_size']),
        reward_max_spread=_decimal(market['reward_max_spread']),
        daily_pool_usd=_decimal(direction.get('daily_pool_usd')), now=now,
    )
    price, quantity = _decimal(guidance['price']), _decimal(guidance['quantity'])
    # Callers already validate fresh, complete current entry facts. Only these
    # conclusive failures of the actual resting quote mean zero, not unknown.
    if resting_quantity and ((estimate['state'] == 'known' and quantity < _decimal(market['reward_min_size']))
            or set(estimate.get('reason_codes', [])) in ({'reward_distance_invalid'}, {'reward_score_zero'})):
        return dict(state='known', basis='resting_non_scoring_order', quantity=quantity,
                    price=price, capital_usd=price*quantity, hourly_reward_usd=ZERO,
                    yield_pct_per_hour=ZERO, checked_at=now)
    if estimate['state'] != 'known':
        return estimate
    weight = (1 - abs(price - estimate['midpoint']) / _decimal(market['reward_max_spread'])) ** 2
    competition = estimate['competition_upper_bound']
    if resting_quantity:
        bids = _levels(direction['book'].get('bids'), 'bids')
        if sum((size for p, size in bids if p == price), ZERO) < resting_quantity:
            return dict(state='unknown', reason_codes=['own_order_depth_unknown'])
        # Keep the observed reward midpoint; remove our score from competition,
        # since public depth already contains this resting order.
        scores = []
        for side in ('bids', 'asks'):
            scores.append(sum((size * (1 - abs(p - estimate['midpoint']) /
                _decimal(market['reward_max_spread'])) ** 2
                for p, size in _levels(direction['book'].get(side), side)
                if abs(p - estimate['midpoint']) < _decimal(market['reward_max_spread'])), ZERO))
        scores[0] -= resting_quantity * weight
        competition = min(scores) + abs(scores[0] - scores[1]) / 3
    own_score = quantity * weight / 3
    share = own_score / (competition + own_score)
    reward = _decimal(direction['daily_pool_usd']) * share / 24
    return dict(state='known', basis='resting_scoring_order' if resting_quantity else 'minimum_scoring_order', quantity=quantity,
                price=price, capital_usd=price*quantity, hourly_reward_usd=reward,
                yield_pct_per_hour=reward/(price*quantity)*100, checked_at=now)


class LPAutoPool:
    def __init__(self, execution):
        self.execution = execution
        self.lp = execution._lp
        self.store = execution._store
        self.send_path = Path(str(self.store.path) + '.lp-auto-send.lock')
        self._reconcile_jobs = {}
        self._reconcile_jobs_lock = threading.Lock()
        self._attention_delivery_lock = threading.Lock()

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
        return dict(run_id=uuid.uuid5(uuid.NAMESPACE_URL, str(self.store.path.resolve()) + str(self.execution._lp_account_id())).hex, account_id=self.execution._lp_account_id(),
                    config_version=0, desired_running=False, ever_enabled=False,
                    enabled_at=None, target_buy_count=0, budget_usd=None,
                    allocations=[], intents={}, events={}, rounds={},
                    last_round={}, last_reconciled_at=None, updated_at=self._stamp())

    def _read(self):
        with self.store._read_connection() as c:
            row=c.execute('SELECT payload FROM lp_auto_pool WHERE singleton=1').fetchone()
            return json.loads(row[0]) if row else self._default()

    def _update(self, fn, *, connection=None):
        # ponytail: one SQLite document serializes this single-account MVP;
        # split event rows if retained history makes document rewrites material.
        with (self.store._transaction() if connection is None else nullcontext(connection)) as c:
            row = c.execute('SELECT payload FROM lp_auto_pool WHERE singleton=1').fetchone()
            d = json.loads(row[0]) if row else self._default()
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
        if (not session or session.get('order_identity_conflict')
                or intent.get('order_identity_conflict')
                or 'identity' in str(intent.get('reconcile_reason') or '')
                or intent.get('reconcile_reason') in {'owned_order_token_mismatch', 'owned_order_side_mismatch'}):
            return False
        return not any(a.get('side') == 'BUY' and a.get('role') != 'entry'
                       for a in self.store.lp_actions(intent['session_id']))

    def _projection(self, d, *, include_intents=True):
        intents = list(d['intents'].values())
        occupied = [i for i in intents if i['state'] not in ('terminal','rejected','aborted')]
        pending = [i for i in occupied if i['state'] in ('reserved','sending','unknown')]
        pending_review = [i for i in pending if i['state']=='unknown']
        canceling = [i for i in occupied if i['state']=='canceling']
        pnl = sum((_decimal(i.get('realized_pnl_usd',0)) for i in intents), ZERO)
        inventory = sum((_decimal(i.get('inventory_cost_usd',0)) for i in intents), ZERO)
        reserved = sum((_decimal(i['reserved_usd']) for i in occupied), ZERO)
        allocated = sum((_decimal(a['amount_usd']) for a in d['allocations']), ZERO)
        total = allocated + pnl
        uncertain = [i for i in intents if i.get('financial_status') == 'unknown'
                     or not self._funds_fresh(i) or i['state'] == 'unknown' or i.get('submission_unknown')]
        financial_unknown = bool(uncertain)
        isolated = [i for i in uncertain if self._isolatable(i)]
        # Hold the entire original principal even if old receipts released it.
        # Unconfirmed proceeds/profits cannot increase the spendable lower bound.
        extra_hold = sum((max(ZERO, _decimal(i['price']) * _decimal(i['quantity'])
                            - _decimal(i.get('inventory_cost_usd', 0)) - _decimal(i['reserved_usd']))
                          + max(ZERO, _decimal(i.get('realized_pnl_usd', 0))) for i in isolated), ZERO)
        spendable = max(ZERO, total - inventory - reserved - extra_hold)

        reasons = []
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
        if len(isolated) != len(uncertain):
            admission_reasons.append('unbounded_financial_uncertainty')
        funds = dict(spendable_usd=str(spendable) if not admission_reasons else None,
                     isolated_reserved_usd=str(extra_hold), total_usd=str(total), available_usd=None if financial_unknown else str(max(ZERO,total-inventory-reserved)),
                     inventory_cost_usd=str(inventory), buy_reserved_usd=str(reserved),
                     pending_reserved_usd=str(sum((_decimal(i['reserved_usd']) for i in pending), ZERO)),
                     realized_pnl_usd=str(pnl), net_allocation_usd=str(allocated),
                     verified_rewards_usd='0', deficit_usd=str(max(ZERO,inventory+reserved-total)),
                     status='unknown' if financial_unknown else 'known',
                     source='lp_session_verified_trades', as_of=d['last_reconciled_at'])
        return dict(**{k:deepcopy(d[k]) for k in ('run_id','account_id','config_version','desired_running',
                    'ever_enabled','enabled_at','target_buy_count','budget_usd','last_round','last_reconciled_at','updated_at')},
                    auto_run_id=d['run_id'], budget_configured=d['budget_usd'] is not None,
                    pause_confirmed=not d['desired_running'], block_reasons=reasons,
                    admission_block_reasons=admission_reasons,
                    isolated_markets=sorted({i['condition_id'] for i in isolated}),
                    runtime_state='paused' if not d['desired_running'] else 'blocked' if admission_reasons else 'running',
                    reason=manual_reason or (reasons[0] if reasons else None), funds=funds,
                    slots=dict(active=len(occupied)-len(pending)-len(canceling),pending=len(pending),
                               pending_review=len(pending_review),canceling=len(canceling),
                               occupied=len(occupied)),
                    **({'intents': deepcopy(intents)} if include_intents else {}))

    def state(self, *, include_intents=True):
        return self._projection(self._read(), include_intents=include_intents)

    def configure(self, payload, *, audit=None):
        if not isinstance(payload, dict) or set(payload)-{'budget_usd','target_buy_count','expected_config_version'}:
            raise ValueError('auto_config_invalid')
        budget = _decimal(payload.get('budget_usd'), 'budget_usd')
        target = payload.get('target_buy_count')
        if budget<0 or isinstance(target,bool) or not isinstance(target,int) or not 0<=target:
            raise ValueError('auto_config_invalid')
        def apply(d):
            state=self._projection(d)
            if not d['account_id'] or d['account_id']!=self.execution._lp_account_id():
                raise ValueError('account_identity_mismatch')
            if d['desired_running'] or state['slots']['occupied']:
                raise ValueError('pause_and_finish_automatic_buys_before_configuring')
            if payload.get('expected_config_version',d['config_version'])!=d['config_version']:
                raise ValueError('config_version_changed')
            if state['funds']['status']!='known':
                raise ValueError('financial_facts_unknown')
            d['config_version']+=1
            d['allocations'].append(dict(source_id=f"config:{d['config_version']}",
                amount_usd=str(budget-_decimal(state['funds']['total_usd'])), occurred_at=self._stamp(),audit=audit))
            d.update(budget_usd=str(budget),target_buy_count=target,updated_at=self._stamp())
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

    def candidates(self, *, releasing=()):
        """Consume the entire qualified pool before exclusion, never the UI top ten."""
        from .polymarket_lp import _candidate_pool_row_expired
        with self.lp._candidate_state_lock:
            facts={key:deepcopy(value) for key,value in self.lp._candidate_qualification_facts.items()
                   if key in self.lp._candidate_pool and not _candidate_pool_row_expired(self.lp._candidate_pool[key], self._now())}
        result=[]
        for condition_id,cached in facts.items():
            if self._excluded(condition_id):
                continue
            for direction in cached.get('directions',[]):
                evaluated=evaluate_lp_entry(direction,account=self._ranking_account(cached.get('account') or {}, releasing),
                    now=self._now(),reservations=self._ranking_reservations(releasing),candidate=True)
                if evaluated.get('state')!='eligible':
                    continue
                guidance=evaluated['guidance']
                estimate=minimum_order_estimate(direction,guidance,self._now())
                if estimate['state']!='known':
                    continue
                result.append({**guidance,'minimum_order_estimate':estimate})
        return sorted(result,key=lambda r:(-_decimal(r['minimum_order_estimate']['yield_pct_per_hour']),str(r['condition_id']),str(r['token_id'])))

    def _ranking_account(self, account, releasing):
        if not isinstance(account.get('open_orders'), (list, tuple)):
            return dict(account)
        ids = {i['order_id'] for i in releasing}
        return {**account, 'open_orders': [o for o in account.get('open_orders', [])
                if self.lp._order_id(o) not in ids]}

    def _ranking_reservations(self, releasing):
        ids = {i['order_id'] for i in releasing}
        return tuple(r for r in self.lp._candidate_reservations() if r['order_id'] not in ids)

    def _rotation_session(self, intent):
        session = self.store.lp_session(intent['session_id'])
        if (intent['state'] != 'active' or not session or session.get('state') != 'entry_open'
                or session.get('entry_cancel_requested') or session.get('stop_requested')
                or session.get('stop_loss_latched') or session.get('order_identity_conflict')
                or self.lp._has_unresolved_submission(session)
                or _decimal(session.get('buy_filled_quantity', 0))
                or any(str(session.get(k)) != str(intent[k]) for k in ('condition_id', 'token_id'))
                or session.get('entry_order_id') != intent['order_id']
                or self.lp._session_order_ids(session) != [intent['order_id']]
                or self.lp._order_history(session).get(intent['order_id'], {}).get('status') != 'LIVE'):
            raise ValueError('rotation_awaiting_reconciliation')
        if self._now() >= _timestamp(session.get('review_at')):
            raise ValueError('review_deadline')
        if any(b.get('state') not in ('registered', 'monitoring')
               for b in self.lp._queue_protection_levels(session).values()):
            raise ValueError('rotation_protection_active')
        return session

    def _ranked_buys(self, state):
        """One market per slot, including existing BUYs, on the same yield basis."""
        active = [i for i in state['intents'] if i['state'] not in ('terminal', 'rejected', 'aborted')]
        if len(active) < state['target_buy_count']:
            candidates = self.candidates()
            return candidates, [], candidates, []
        occupied_count = len(active)
        blocked = []
        rankable = []
        state_reasons = {'reserved': 'submission_pending', 'sending': 'submission_pending',
                         'unknown': 'submission_unknown', 'canceling': 'rotation_awaiting_reconciliation'}
        for intent in active:
            if intent['state'] != 'active':
                blocked.append({'condition_id': intent['condition_id'], 'token_id': intent['token_id'],
                                'reason': intent.get('reconcile_reason') or state_reasons[intent['state']]})
                continue
            if intent.get('financial_status') != 'known':
                blocked.append({'condition_id': intent['condition_id'], 'token_id': intent['token_id'],
                                'reason': 'financial_facts_unknown'})
                continue
            try:
                _freshness(intent.get('checked_at'), self._now(), 'financial_facts', max_age=Decimal(60))
            except ValueError as exc:
                blocked.append({'condition_id': intent['condition_id'], 'token_id': intent['token_id'],
                                'reason': str(exc)})
                continue
            rankable.append(intent)
        active = rankable
        rows = []
        for intent in active:
            try:
                self._rotation_session(intent)
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
                evaluated = evaluate_lp_entry(direction, account=self._ranking_account(account, active),
                    now=self._now(), reservations=self._ranking_reservations(active), candidate=True)
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
        active = [i for i in active if any(r['intent_id'] == i['intent_id'] for r in rows)]
        slots = state['target_buy_count'] - (occupied_count - len(active))
        if slots <= 0:
            return [], [], [], blocked
        candidates = self.candidates(releasing=active)
        rows.extend(candidates)
        refreshed = {(r['condition_id'], r['token_id']) for r in rows if r.get('intent_id')}
        while True:
            rows.sort(key=lambda r: (-_decimal(r['minimum_order_estimate']['yield_pct_per_hour']),
                not bool(r.get('intent_id')), str(r['condition_id']), str(r['token_id'])))
            unique = {}
            for row in rows:
                unique.setdefault(row['condition_id'], row)
            targets = list(unique.values())[:slots]
            pending = next((r for r in targets if (r['condition_id'], r['token_id']) not in refreshed), None)
            if pending is None:
                break
            try:
                facts = self.lp._read_candidate_facts(pending, wait_for_capacity=True)
                account, direction = facts['account'], facts['direction']
                evaluated = evaluate_lp_entry(direction, account=self._ranking_account(account, active),
                    now=self._now(), reservations=self._ranking_reservations(active), candidate=True)
                if evaluated.get('state') != 'eligible':
                    if evaluated.get('state') == 'rejected':
                        rows.remove(pending)
                        candidates.remove(pending)
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
        for row in targets:
            self._ranking_fresh(row)
        victims = [r for r in rows if r.get('intent_id') and r['condition_id'] not in {t['condition_id'] for t in targets}]
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
        return targets, victims, candidates, blocked

    def _ranking_fresh(self, row):
        wallet = str(row['ranking_account'].get('wallet_address') or '').strip().casefold()
        if not wallet or hashlib.sha256(wallet.encode()).hexdigest() != self.execution._lp_account_id():
            raise ValueError('account_identity_mismatch')
        for stamp in (row['ranking_account'].get('checked_at'), row['ranking_book_at'], row['ranking_reward_at']):
            _freshness(stamp, self._now(), 'ranking_freshness', max_age=60)

    def _rotate_out(self, victims, targets, version):
        """Validate and fence the whole batch before any exact-ID cancel."""
        from .polymarket_lp import expiration_for_review
        from .polymarket_lp_views import _next_review_at
        with self._send_barrier():
            lock = self.execution._acquire_global_lock()
            if lock is None:
                raise ValueError('execution_lock')
            try:
                with self.lp._mutex:
                    state = self.state()
                    if not state['desired_running'] or state['admission_block_reasons'] or state['config_version'] != version:
                        raise ValueError(state['reason'] or 'config_version_changed')
                    if self.store.active_execution() is not None:
                        raise ValueError('active_execution')
                    expiration_for_review(_next_review_at(self._now()), now=self._now())
                    for target in [*victims, *targets]:
                        self._ranking_fresh(target)
                        if target.get('intent_id'):
                            self._rotation_session(target)
                    capital = sum((_decimal(t['minimum_order_estimate']['capital_usd']) for t in targets), ZERO)
                    released_ids = {r['intent_id'] for r in [*victims, *targets] if r.get('intent_id')}
                    available = _decimal(state['funds']['spendable_usd']) + sum(
                        (_decimal(i['reserved_usd']) for i in state['intents'] if i['intent_id'] in released_ids), ZERO)
                    if capital > available:
                        raise ValueError('top_yield_funds_insufficient')
                    def record(d):
                        for row in victims:
                            intent = d['intents'][row['intent_id']]
                            intent.update(state='canceling', rotation_requested_at=self._stamp(),
                                          rotation_last_attempt_at=self._stamp(), rotation_cancel_acknowledged=False)
                            self._event(d, intent, 'rotation_requested', occurred_at=self._stamp(),
                                target_conditions=[t['condition_id'] for t in targets],
                                previous_yield=row['minimum_order_estimate']['yield_pct_per_hour'])
                    self._update(record)
                    attempts = self.lp.begin_order_cancel([r['order_id'] for r in victims])
                    for row in victims:
                        session = self.store.lp_update_session(row['session_id'], patch={'entry_cancel_requested': True})
                        self.lp._mark_group_buckets_canceling(session, 'yield_rotation')
            finally:
                self.execution._release_global_lock(lock)
            actions = []
            for row in victims:
                acknowledged = self._send_rotation_cancel(row, [a for a in attempts if a[2]['order_id'] == row['order_id']])
                actions.append(dict(condition_id=row['condition_id'], order_id=row['order_id'], state='canceling',
                                    reason='yield_rotation' if acknowledged else 'rotation_cancel_unknown'))
            return actions

    def _send_rotation_cancel(self, intent, attempts):
        # Caller holds the send barrier; no global lock, LP mutex or DB transaction.
        try:
            acknowledged = self.lp._cancel_order(intent['order_id'], attempts=attempts)
        except Exception:
            acknowledged = False
        self._update(lambda d: d['intents'][intent['intent_id']].update(rotation_cancel_acknowledged=acknowledged))
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
                        current = self._read()['intents'][intent['intent_id']]
                        session = self.store.lp_session(intent['session_id'])
                        if (current.get('rotation_cancel_acknowledged') or current.get('rotation_settled_at')
                                or current['state'] in ('terminal', 'aborted', 'rejected') or not session
                                or session.get('order_identity_conflict')
                                or any(session.get(k) != intent[k] for k in ('condition_id', 'token_id'))
                                or session.get('entry_order_id') != intent['order_id']
                                or self.lp._order_history(session).get(intent['order_id'], {}).get('status') != 'LIVE'
                                or checked_at <= _timestamp(current['rotation_last_attempt_at'])):
                            return
                        self._update(lambda d: d['intents'][intent['intent_id']].update(rotation_last_attempt_at=self._stamp()))
                        attempts = self.lp.begin_order_cancel((intent['order_id'],))
                        session = self.store.lp_update_session(intent['session_id'], patch={'entry_cancel_requested': True})
                        self.lp._mark_group_buckets_canceling(session, 'yield_rotation')
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
                if not intent.get('rotation_requested_at') or intent.get('rotation_settled_at'):
                    continue
                if intent['state'] != 'terminal' or intent.get('financial_status') != 'known':
                    reason = reason or 'rotation_awaiting_reconciliation'
                    if not intent.get('rotation_cancel_acknowledged') and intent['state'] not in ('terminal', 'aborted', 'rejected'):
                        retries.append(deepcopy(intent))
                    continue
                intent['rotation_settled_at'] = self._stamp()
                filled = _decimal(intent.get('filled_quantity', 0))
                self._event(d, intent, 'rotation_ended', occurred_at=self._stamp(), filled_quantity=str(filled))
                if filled:
                    reason = 'rotation_filled'
            return reason
        reason = self._update(apply)
        for intent in retries:
            self._retry_rotation_cancel(intent)
        return reason

    def _deliver_attention(self, intent_id: str, *, recovery: bool = False) -> None:
        """Send one persisted episode notice; delivery is explicit, not assumed."""
        # One process-wide delivery lock prevents duplicate sends. It never
        # guards fact reads, SQLite publication, or trading.
        with self._attention_delivery_lock:
            document=self._read()
            intent=document['intents'].get(intent_id)
            if intent is None:
                return
            now=self._now()
            retry_at=intent.get('attention_send_retry_at')
            if retry_at and now < _timestamp(retry_at,name='attention_send_retry_at'):
                return
            if recovery:
                if not intent.get('attention_recovery_due'):
                    return
                episode=intent.get('attention_episode') or intent.get('attention_since')
                title, message='LP 核对已恢复', 'LP 资金核对已恢复，自动调度保持原运行/暂停设置。'
            else:
                if not intent.get('attention_due'):
                    return
                episode=intent.get('attention_episode') or intent.get('attention_since')
                reason=intent.get('reconcile_error') or intent.get('reconcile_reason') or 'unknown'
                title='LP 核对持续失败'
                message=f'LP 核对已连续 5 分钟无有效进展：{reason}。系统继续只读核对并保留资金边界。'
            if not episode:
                return
            # Persist the attempt before network I/O so restart cannot reset
            # the episode and immediately repeat it.
            def claim(d):
                current=d['intents'].get(intent_id)
                if current is None or (current.get('attention_episode') or current.get('attention_since')) != str(episode):
                    return False
                if recovery:
                    if not current.get('attention_recovery_due'):
                        return False
                elif not current.get('attention_due'):
                    return False
                current.update(
                    attention_episode=str(episode),
                    attention_sending=True,
                    attention_send_retry_at=(now+timedelta(seconds=60)).isoformat(),
                )
                return True
            if not self._update(claim):
                return
            fault_key, recovery_key=('attention_attempted_channels','attention_recovery_attempted_channels')
            attempted={str(v) for v in (intent.get(recovery_key if recovery else fault_key) or ())}
            if recovery and not attempted:
                attempted.update(intent.get('attention_delivered_channels') or ())
            delivered_channels={str(v) for v in (intent.get(
                'attention_recovery_delivered_channels' if recovery
                else 'attention_delivered_channels') or ())}
            missing=(attempted-delivered_channels) if attempted else None
            delivery_unknown=False
            try:
                attempts=send_notification_with_results(
                    self.execution._notifier,title,message,channels=missing)
            except Exception:
                # The remote result is unknown, not failed or delivered. A
                # future retry may duplicate; that is safer than losing the
                # only notice while reservations remain held.
                attempts=()
                delivery_unknown=True
            attempted.update(str(a.channel) for a in attempts)
            delivered_channels.update(
                str(a.channel) for a in attempts if a.success)
            delivered=bool(attempted) and delivered_channels >= attempted
            attempted_key=('attention_recovery_attempted_channels' if recovery
                           else 'attention_attempted_channels')
            delivered_key=('attention_recovery_delivered_channels' if recovery
                           else 'attention_delivered_channels')
            def apply(d):
                intent=d['intents'].get(intent_id)
                if intent is None or intent.get('attention_episode') != str(episode):
                    return
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
                                    'attention_recovered_at'):
                            intent.pop(key,None)
                    else:
                        intent['attention_send_error']='notification_delivery_failed'
                else:
                    if intent.get('attention_recovered_at'):
                        intent['attention_due']=False
                        intent['attention_recovery_due']=bool(delivered_channels)
                        intent.pop('attention_send_retry_at',None)
                        return
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

    def flush_attention(self, session_id: str | None = None) -> None:
        """Deliver persisted notices only after their fact transaction commits."""
        document=self._read()
        intents=document['intents'].values()
        selected=[i for i in intents if session_id is None or i.get('session_id')==session_id]
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
                'attention_recovered_at',
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
            i['attention_due']=bool(due_now or i.get('attention_sending'))

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
                                 and 'cancel' in str(a.get('action_key')) for a in actions)
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
                role=str(action.get('role') or '')
                if role=='entry' or 'cancel' in role or 'cancel' in str(action.get('action_key') or ''):
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
                    and 'cancel' not in str(a.get('action_key') or '')), {})
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
                if oid in bindings and ('cancel' in str(action.get('role') or '') or 'cancel' in str(action.get('action_key') or '')):
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

    def _reconcile_unknown(self, *, reuse=False, bounded=False):
        d=self._read()
        if not d['account_id'] or d['account_id']!=self.execution._lp_account_id():
            return self._projection(d)
        intents = [i for i in d['intents'].values() if not i.get('settled')]
        if bounded:
            # Persistent per-intent jobs: a slow read never owns the auto-round
            # barrier, and later rounds cannot stack workers for that session.
            deadline = monotonic() + 1.0
            remaining = sorted(intents, key=lambda i: i.get('checked_at') or i['created_at'])
            lease = self._begin_account_round()
            try:
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
            finally:
                # The caller drops its reference now; jobs launched by this
                # batch each hold another reference until their result lands.
                if lease is not None:
                    lease.release()
        else:
            lease = self._begin_account_round()
            try:
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
            finally:
                if lease is not None:
                    lease.release()
        def finish(doc):
            doc['last_checked_at']=self._stamp()
            if all(i.get('financial_status')=='known' and i['state']!='unknown' for i in doc['intents'].values()):
                doc['last_reconciled_at']=self._stamp()
        self._update(finish)
        return self.state()

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

    def _reconcile_intent(self, i, *, reuse, account_round=None):
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
        if d['config_version']!=i['config_version']:
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
        _freshness(snapshot['account'].get('checked_at'),self._now(),'account_freshness',max_age=60)
        _freshness(snapshot['book'].get('received_at'),self._now(),'book_freshness',max_age=60)

    def _submit(self, row, round_id, index, version):
        # Network preparation never owns the existing protection/apply lane.
        snapshot=self.lp._read_candidate_snapshot(row,now=self._now())
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
            return self._submit_prepared(row,round_id,index,version,snapshot,release)
        finally:
            release()

    def _submit_prepared(self, row, round_id, index, version, snapshot, release):
        from .polymarket_lp import expiration_for_review
        from .polymarket_lp_views import _next_review_at
        if self.store.active_execution() is not None:
            raise ValueError('active_execution')
        fresh=self.lp._fresh_candidate_row(row,snapshot,now=self._now())
        request=self.lp._normalize_request({**fresh,'candidate_policy':'best_bid_minimum','review_at':_next_review_at(self._now())})
        facts=self.lp._validate_snapshot(request,snapshot,now=self._now(),reservations=self.lp._candidate_reservations())
        self.lp._require_lp_history(request,now=self._now())
        if self._excluded(str(row['condition_id'])):
            raise ValueError('market_already_participating')
        intent_id=f'{round_id}:{index}'
        session_id=uuid.uuid4().hex
        amount=request['price']*request['quantity']
        def reserve(d):
            state=self._projection(d)
            if intent_id in d['intents']:
                raise ValueError('intent_already_reserved')
            if not d['desired_running'] or state['admission_block_reasons'] or d['config_version']!=version:
                raise ValueError(state['reason'] or 'config_version_changed')
            if state['slots']['occupied']>=d['target_buy_count']:
                raise ValueError('target_filled')
            if amount>_decimal(state['funds']['spendable_usd']):
                raise ValueError('strategy_funds_insufficient')
            i=dict(intent_id=intent_id,session_id=session_id,order_id=None,config_version=version,
                   state='reserved',reserved_usd=str(amount),inventory_cost_usd='0',realized_pnl_usd='0',financial_status='known',
                   **{k:str(request[k]) for k in ('condition_id','market_id','token_id','outcome','price','quantity')},created_at=self._stamp())
            d['intents'][intent_id]=i
            self._event(d,i,'intent',occurred_at=self._stamp(),quantity=i['quantity'],price=i['price'])
        self._update(reserve)
        def post(signed, mark_post_started):
            from .polymarket_lp import AutoEntryNotSent
            try:
                latest = self.lp._read_candidate_snapshot(request, now=self._now(), ignore_session_id=session_id)
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
        return result

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
        with self._round_barrier() as locked:
            if not locked:
                return {**self.state(),'round_reason':'round_in_progress'}
            return self._run_once(round_id=round_id, reuse_facts=reuse_facts)

    def _run_once(self, *, round_id=None, reuse_facts=False):
        self._reconcile_unknown(reuse=reuse_facts, bounded=True)
        rotation_reason = self._settle_rotations()
        d=self._read()
        round_id=round_id or uuid.uuid4().hex
        if not isinstance(round_id,str) or not round_id or len(round_id)>128:
            raise ValueError('round_id_invalid')
        if round_id in d['rounds']:
            return self.state()
        state=self._projection(d)
        deficit=max(0,d['target_buy_count']-state['slots']['occupied'])
        actions=[]
        candidates=[]
        targets=[]
        blocked=[]
        reason=(state['admission_block_reasons'] or [None])[0] or (rotation_reason if rotation_reason == 'rotation_filled' else None)
        self._update(lambda doc:doc['rounds'].update({round_id:dict(started_at=self._stamp())}))
        if state['desired_running'] and not reason and not state['admission_block_reasons'] and self.execution.lp_mutation_allowed():
            try:
                targets, victims, candidates, blocked = self._ranked_buys(state)
                if not targets and blocked:
                    reason = blocked[0]['reason']
                if victims:
                    actions.extend(self._rotate_out(victims, targets, d['config_version']))
                    reason = 'rotation_awaiting_reconciliation'
            except ValueError as exc:
                reason = str(exc)
            submitted=0
            seen=set()
            for index,row in enumerate(targets if not reason else []):
                if submitted>=deficit:
                    break
                if row.get('intent_id'):
                    continue
                if row['condition_id'] in seen:
                    continue
                if not self.state()['desired_running'] or self.state()['admission_block_reasons']:
                    break
                try:
                    result=self._submit(row,round_id,index,d['config_version'])
                    actions.append({'condition_id':row['condition_id'],**result})
                    if result.get('session_id') and result.get('state')!='entry_rejected':
                        submitted+=1
                        seen.add(row['condition_id'])
                except ValueError as exc:
                    actions.append(dict(condition_id=row['condition_id'],state='rejected',reason=str(exc)))
        self._update(lambda doc:doc.update(last_round=dict(round_id=round_id,checked_at=self._stamp(),actions=actions,
            candidates=[{k: r[k] for k in ('condition_id','token_id','outcome','price','quantity','minimum_order_estimate')}
                        for r in candidates[:10]],candidate_count=len(candidates),
            targets=[{k: r[k] for k in ('condition_id','token_id','price','quantity','minimum_order_estimate')}
                     for r in targets[:d['target_buy_count']]],
            reason=reason or rotation_reason or ('candidates_or_funds_insufficient' if self._projection(doc)['slots']['occupied']<doc['target_buy_count'] else 'target_filled'),blocked=blocked)))
        return self.state()

    def report_facts(self, period_start=None, period_end=None):
        d=self._read()
        state=self._projection(d)
        sessions=[session for i in d['intents'].values()
                  if (session:=self.store.lp_session(i['session_id'])) is not None]
        events=list(d['events'].values())
        financial=dict(status='unknown',reason='historical_financial_boundary_unavailable',
                       realized_pnl_usd=None,inventory_cost_usd=None,inventory_quantity=None,
                       as_of=None,source='lp_session_verified_trades')
        if period_start is not None and period_end is not None:
            start,end=_timestamp(period_start),_timestamp(period_end)
            if start>end:
                raise ValueError('report_period_invalid')
            fills=[e for e in events if e['kind']=='fill']
            complete=all(e.get('occurred_at') and e.get('fee') is not None for e in fills)
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
                financial['reason']='trade_time_fee_or_reconciliation_unknown'
        return dict(account_id=d['account_id'],auto_run_id=d['run_id'],state=state,
                    events=events,sessions=sessions,funds=state['funds'],
                    as_of=d['last_reconciled_at'],financial_period=financial)
