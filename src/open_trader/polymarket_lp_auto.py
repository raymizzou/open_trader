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
from contextlib import contextmanager
from copy import deepcopy
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from .polymarket_lp_risk import (
    TERMINAL_ORDER_STATES, _decimal as _money, _freshness,
    _maybe_decimal, _timestamp, evaluate_lp_entry, estimate_lp_target_share_yield,
)

ZERO = Decimal('0')


def _decimal(value, name='money'):
    return _money(value, name)


def _json(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    raise TypeError(type(value).__name__)


def minimum_order_estimate(direction, guidance, now):
    """Reuse reward weights, recompute the share at the actual legal quantity."""
    market = direction['market']
    estimate = estimate_lp_target_share_yield(
        direction['book'], price=_decimal(guidance['price']),
        reward_min_size=_decimal(market['reward_min_size']),
        reward_max_spread=_decimal(market['reward_max_spread']),
        daily_pool_usd=_decimal(direction.get('daily_pool_usd')), now=now,
    )
    if estimate['state'] != 'known':
        return estimate
    price, quantity = _decimal(guidance['price']), _decimal(guidance['quantity'])
    weight = (1 - abs(price - estimate['midpoint']) / _decimal(market['reward_max_spread'])) ** 2
    own_score = quantity * weight / 3
    share = own_score / (estimate['competition_upper_bound'] + own_score)
    reward = _decimal(direction['daily_pool_usd']) * share / 24
    return dict(state='known', basis='minimum_scoring_order', quantity=quantity,
                price=price, capital_usd=price*quantity, hourly_reward_usd=reward,
                yield_pct_per_hour=reward/(price*quantity)*100, checked_at=now)


class LPAutoPool:
    def __init__(self, execution):
        self.execution = execution
        self.lp = execution._lp
        self.store = execution._store
        self.send_path = Path(str(self.store.path) + '.lp-auto-send.lock')

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

    def _update(self, fn):
        # ponytail: one SQLite document serializes this single-account MVP;
        # split event rows if retained history makes document rewrites material.
        with self.store._transaction() as c:
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

    def _projection(self, d):
        intents = list(d['intents'].values())
        occupied = [i for i in intents if i['state'] not in ('terminal','rejected','aborted')]
        pending = [i for i in occupied if i['state'] in ('reserved','sending','unknown')]
        canceling = [i for i in occupied if i['state']=='canceling']
        pnl = sum((_decimal(i.get('realized_pnl_usd',0)) for i in intents), ZERO)
        inventory = sum((_decimal(i.get('inventory_cost_usd',0)) for i in intents), ZERO)
        reserved = sum((_decimal(i['reserved_usd']) for i in occupied), ZERO)
        allocated = sum((_decimal(a['amount_usd']) for a in d['allocations']), ZERO)
        total = allocated + pnl
        financial_unknown = any(i.get('financial_status')=='unknown' for i in intents)
        reasons = []
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
        funds = dict(total_usd=str(total), available_usd=None if financial_unknown else str(max(ZERO,total-inventory-reserved)),
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
                    runtime_state='paused' if not d['desired_running'] else 'blocked' if reasons else 'running',
                    reason=manual_reason or (reasons[0] if reasons else None), funds=funds,
                    slots=dict(active=len(occupied)-len(pending)-len(canceling),pending=len(pending),
                               canceling=len(canceling),occupied=len(occupied)), intents=deepcopy(intents))

    def state(self):
        return self._projection(self._read())

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

    def candidates(self):
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
                evaluated=evaluate_lp_entry(direction,account=cached.get('account') or {},
                    now=self._now(),reservations=self.lp._candidate_reservations(),candidate=True)
                if evaluated.get('state')!='eligible':
                    continue
                guidance=evaluated['guidance']
                estimate=minimum_order_estimate(direction,guidance,self._now())
                if estimate['state']!='known':
                    continue
                result.append({**guidance,'minimum_order_estimate':estimate})
        return sorted(result,key=lambda r:(-_decimal(r['minimum_order_estimate']['yield_pct_per_hour']),str(r['condition_id']),str(r['token_id'])))

    def _record_session(self, intent_id, session, *, error=None):
        def apply(d):
            i=d['intents'][intent_id]
            if error:
                if i['state']=='terminal' and i.get('financial_status')=='known' and _decimal(i.get('inventory_cost_usd',0))==0:
                    return
                i['financial_status']='unknown'
                i['reconcile_reason']=error
                return
            if session.get('order_identity_conflict'):
                i.update(state='unknown',financial_status='unknown',reconcile_reason='order_identity_conflict',
                    order_identity_conflict=session['order_identity_conflict'])
                self._event(d,i,'unknown',**session['order_identity_conflict'])
                return
            order_id=session.get('entry_order_id')
            if order_id:
                if any(other.get('order_id')==order_id and other['intent_id']!=intent_id for other in d['intents'].values()):
                    i.update(state='unknown',financial_status='unknown',reconcile_reason='order_identity_conflict')
                    return
                i['order_id']=order_id
            status=session.get('submit_status')
            if session.get('state')=='entry_rejected':
                i.update(state='rejected',reserved_usd='0',financial_status='known')
                self._event(d,i,'rejected',occurred_at=session.get('submit_receipt_at'))
                return
            if not order_id:
                i.update(state='unknown',financial_status='unknown',reconcile_reason='missing_reliable_order_id')
                self._event(d,i,'unknown')
                return
            history=self.lp._order_history(session)
            entry=history.get(order_id,{})
            order_state=str(entry.get('status') or '').upper()
            if order_state in ('', 'UNKNOWN'):
                i.update(state='unknown', financial_status='unknown', reconcile_reason='order_receipt_unknown')
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
            i['submission_unknown']=self.lp._has_unresolved_submission(session) or any(
                str(owned.get('status') or 'UNKNOWN').upper()=='UNKNOWN' for owned in history.values())
            known=known and not i['submission_unknown']
            if known:
                acquired=cost+buy_fees
                released=acquired*sold/quantity if quantity else ZERO
                i.update(realized_pnl_usd=str(revenue-sell_fees-released),inventory_cost_usd=str(acquired-released),financial_status='known')
            else:
                i['financial_status']='unknown'
            i['reserved_usd']=str(ZERO if terminal else max(ZERO,_decimal(i['quantity'])-quantity)*_decimal(i['price']))
            i['state']='terminal' if terminal else 'canceling' if session.get('entry_cancel_requested') else 'active'
            i['filled_quantity']=str(quantity)
            i['reconcile_reason']=None if known else 'position_or_fee_unknown'
            actions=self.store.lp_actions(str(session['session_id']))
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
            i['checked_at']=self._stamp()
        self._update(apply)

    def _reconcile_unknown(self):
        d=self._read()
        if not d['account_id'] or d['account_id']!=self.execution._lp_account_id():
            return self._projection(d)
        for i in d['intents'].values():
            if i['state'] in ('aborted','rejected'):
                continue
            session=self.store.lp_session(i['session_id'])
            if session is None:
                # The session is persisted before signing/posting. No session
                # proves this reserved intent never reached the exchange lane.
                self._update(lambda doc:doc['intents'][i['intent_id']].update(state='aborted',reserved_usd='0'))
                continue
            if session.get('order_identity_conflict'):
                self._record_session(i['intent_id'],session)
                continue
            if not session.get('entry_order_id'):
                lock=self.execution._acquire_global_lock()
                if lock is None:
                    self._record_session(i['intent_id'],session,error='execution_lock')
                    continue
                try:
                    with self.lp._mutex:
                        session=self.store.lp_session(i['session_id'])
                        actions=self.store.lp_actions(i['session_id'])
                        ids={a.get('order_id') for a in actions if a.get('role')=='entry' and a.get('state')=='accepted' and a.get('order_id')}
                        if len(ids)==1 and not session.get('entry_order_id'):
                            oid=ids.pop()
                            session=self.store.lp_update_session(i['session_id'],patch=dict(entry_order_id=oid,owned_order_ids=[oid],submit_status='unknown'))
                finally:
                    self.execution._release_global_lock(lock)
                if not session.get('entry_order_id'):
                    self._record_session(i['intent_id'],session)
                    continue
            try:
                session,revision=self.store.lp_session_with_revision(i['session_id'])
                snapshot=self.lp._read_snapshot(session)
                wallet=str((snapshot.get('account') or {}).get('wallet_address') or '').strip().casefold()
                if not wallet or hashlib.sha256(wallet.encode()).hexdigest()!=d['account_id']:
                    raise ValueError('account_identity_mismatch')
                lock=self.execution._acquire_global_lock()
                if lock is None:
                    self._record_session(i['intent_id'],session,error='execution_lock')
                    continue
                try:
                    with self.lp._mutex:
                        if self.store.lp_session_revision(i['session_id'])!=revision:
                            self._record_session(i['intent_id'],session,error='session_changed')
                            continue
                        session=self.lp._sync_order_history(session,snapshot)
                        receipt=self.lp._order_history(session).get(str(session.get('entry_order_id')), {})
                        if receipt.get('status') not in (None, '', 'UNKNOWN') and session.get('submit_status') in ('unknown','accepted_without_order_id',None):
                            session=self.store.lp_update_session(i['session_id'],state='review' if session.get('stop_requested') else 'entry_open',
                                patch=dict(submit_status='accepted',resume_state=None))
                        patch=self.lp._fill_patch(session,snapshot)
                        session=self.store.lp_update_session(i['session_id'],patch=patch)
                        session=self.lp._complete_if_flat(session,snapshot)
                        self._record_session(i['intent_id'],session)
                finally:
                    self.execution._release_global_lock(lock)
            except (ValueError,RuntimeError,OSError) as exc:
                self._record_session(i['intent_id'],session,error=str(exc))
        def finish(doc):
            doc['last_checked_at']=self._stamp()
            if all(i.get('financial_status')=='known' and i['state']!='unknown' for i in doc['intents'].values()):
                doc['last_reconciled_at']=self._stamp()
        self._update(finish)
        return self.state()

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
        reasons=[r for r in state['block_reasons'] if r not in ('submission_unknown',)]
        if reasons or any(o['state']=='unknown' for o in d['intents'].values() if o['intent_id']!=intent_id):
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
            if not d['desired_running'] or state['block_reasons'] or d['config_version']!=version:
                raise ValueError(state['reason'] or 'config_version_changed')
            if state['slots']['occupied']>=d['target_buy_count']:
                raise ValueError('target_filled')
            if amount>_decimal(state['funds']['available_usd']):
                raise ValueError('strategy_funds_insufficient')
            i=dict(intent_id=intent_id,session_id=session_id,order_id=None,config_version=version,
                   state='reserved',reserved_usd=str(amount),inventory_cost_usd='0',realized_pnl_usd='0',financial_status='known',
                   **{k:str(request[k]) for k in ('condition_id','market_id','token_id','outcome','price','quantity')},created_at=self._stamp())
            d['intents'][intent_id]=i
            self._event(d,i,'intent',occurred_at=self._stamp(),quantity=i['quantity'],price=i['price'])
        self._update(reserve)
        def post(signed):
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
                        self._update(lambda d:d['intents'][intent_id].update(state='sending'))
                finally:
                    self.execution._release_global_lock(lock)
                return self.lp._post_limit(signed)
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

    def run_once(self, *, round_id=None):
        with self._round_barrier() as locked:
            if not locked:
                return {**self.state(),'round_reason':'round_in_progress'}
            return self._run_once(round_id=round_id)

    def _run_once(self, *, round_id=None):
        self._reconcile_unknown()
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
        self._update(lambda doc:doc['rounds'].update({round_id:dict(started_at=self._stamp())}))
        if state['desired_running'] and not state['block_reasons'] and deficit and self.execution.lp_mutation_allowed():
            submitted=0
            seen=set()
            candidates=self.candidates()
            for index,row in enumerate(candidates):
                if submitted>=deficit:
                    break
                if row['condition_id'] in seen:
                    continue
                if not self.state()['desired_running'] or 'submission_unknown' in self.state()['block_reasons']:
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
            candidates=candidates[:10],candidate_count=len(candidates),
            reason=state['reason'] or ('candidates_or_funds_insufficient' if self._projection(doc)['slots']['occupied']<doc['target_buy_count'] else 'target_filled'))))
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
