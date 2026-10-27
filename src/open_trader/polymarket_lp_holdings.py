"""Holding exits owned by the existing live runtime and its mutation lane.

Persist intent before preparing or posting an order. Restored requests reserve
inventory only; they never confer permission to replay a submission.
"""
from collections.abc import Mapping
from decimal import Decimal
from datetime import datetime
import time
import json
from uuid import uuid4

from .lp_business_writer import RequestRecord, ReceiptRecord, ProgressRecord, HoldingInventoryRecord
from .lp_order_ownership import normalize_sell_quantity
from .polymarket_lp_current_state import CurrentOrderFact, CurrentPositionFact, Fact, ReceiptEvidence, SourceStamp, TerminalProof
from .polymarket_lp_risk import _freshness, _levels, _maybe_decimal, passive_exit_price, passive_cost_floor, TERMINAL_ORDER_STATES


class HoldingSells:
    def __init__(self, live):
        self.live = live
        self.requests = {r['request_id']: r for r in live.store.lp_holding_sell_orders(
            live.account_id, live.venue, include_inventory=True)}
        self._legacy_pending = None
        self.anchors = {}
        for row in sorted(self.requests.values(), key=lambda r: r.get('sequence', 0)):
            if row.get('kind') == 'inventory':
                self.anchors[self._identity(row)] = row
        self._refresh_legacy_pending()

    def session_inventory(self, snapshot, identity):
        """Exact episode fills determine owned shares, independent of API inventory."""
        result = []
        for scope, opening in snapshot.openings.items():
            if scope[:3] != identity or opening.phase == 'closed':
                continue
            orders = self.live._opening_orders(snapshot, opening)
            entries = [o for o in orders if o.side == 'BUY' and o.key[3] in opening.entry_order_ids]
            if not entries:
                return (), False
            quantities, costs = {'BUY': Decimal(0), 'SELL': Decimal(0)}, []
            fragmented = False
            for order in orders:
                if order.side == 'BUY' and order not in entries:
                    continue
                if order.size_matched.state != 'known':
                    return (), False
                if not order.size_matched.value:
                    continue
                fill = snapshot.own_fills.get(order.key)
                if (fill is None or not fill.complete_for_target or fill.quantity.state != 'known'
                        or fill.quantity.value != order.size_matched.value):
                    return (), False
                quantities[order.side] += fill.quantity.value
                if order.side == 'BUY':
                    costs.append(fill.notional_usd.value if fill.notional_usd.state == 'known' else None)
                    fragmented |= len(fill.fill_ids) > 1
            bought, sold = quantities['BUY'], quantities['SELL']
            if sold > bought:
                return (), False
            average = sum(costs, Decimal(0)) / bought if bought and costs and all(c is not None for c in costs) else None
            if sold and (fragmented or len(entries) > 1):
                # Aggregate purchase notional cannot prove the remaining lot
                # average after interleaved sales/additions. Keep quantity
                # proof and use the current account average when available.
                average = None
            result.append(dict(scope=scope, bought=bought, sold=sold, residual=bought-sold, average=average))
        return result, True

    def observe_account(self, snapshot, position):
        if not self._account_known(snapshot, self.live._utc_now()):
            return False
        identity = (position.account_id.casefold(), position.condition_id, position.token_id)
        sales = {o.key[3]: str(o.size_matched.value) for o in snapshot.orders if o.key[:3] == (
            position.account_id, position.condition_id, position.token_id) and o.side == 'SELL'
            and o.size_matched.state == 'known'}
        buys = {o.key[3]: str(o.size_matched.value) for o in snapshot.orders if o.key[:3] == (
            position.account_id, position.condition_id, position.token_id) and o.side == 'BUY'
            and o.size_matched.state == 'known'}
        old = self.anchors.get(identity)
        active_session = any(scope[:3] == (position.account_id, position.condition_id, position.token_id)
            and opening.phase != 'closed' for scope, opening in snapshot.openings.items())
        if active_session:
            for order in snapshot.orders:
                if (order.key[:3] != (position.account_id, position.condition_id, position.token_id)
                        or order.side != 'SELL' or not self.live._is_local_order(order)
                        or self.is_holding_order(order.key) and order.key not in self.live._opening_order_associations
                        or order.size_matched.state != 'known'
                        or not order.size_matched.value):
                    continue
                own = snapshot.own_fills.get(order.key)
                if (own is None or not own.complete_for_target or own.quantity.state != 'known'
                        or own.quantity.value != order.size_matched.value):
                    # A changing API balance cannot acknowledge an episode sale
                    # before its exact own-fill proof. Retain the earlier anchor.
                    return False
        sell_credit = buy_credit = Decimal(0)
        if old is not None:
            # Missing order IDs never erase historical matched quantities.
            sales = {**old['sales'], **sales}
            buys = {**old['buys'], **buys}
            sold_delta = sum((Decimal(q)-Decimal(old['sales'].get(k, '0')) for k,q in sales.items()), Decimal(0))
            buy_delta = sum((Decimal(q)-Decimal(old['buys'].get(k, '0')) for k,q in buys.items()), Decimal(0))
            if sold_delta < 0 or buy_delta < 0:
                return False
            sell_credit = Decimal(old.get('sell_credit', '0'))
            buy_credit = Decimal(old.get('buy_credit', '0'))
            used_sell, used_buy = min(sold_delta, sell_credit), min(buy_delta, buy_credit)
            expected = Decimal(old['quantity']) - (sold_delta-used_sell) + (buy_delta-used_buy)
            if sold_delta and position.quantity.value != expected:
                return False
            if buy_delta and position.quantity.value < expected:
                return False
            if (position.quantity.value == Decimal(old['quantity']) and sales == old['sales'] and buys == old['buys']):
                return True
            sell_credit -= used_sell
            buy_credit -= used_buy
            # Reconcile the unexplained movement AFTER the proven deltas,
            # including observations that contain a real new BUY or SELL.
            # A reversal consumes the opposing old credit before any new one.
            decrease = max(Decimal(0), expected-position.quantity.value)
            increase = max(Decimal(0), position.quantity.value-expected)
            reversed_buy, reversed_sell = min(decrease, buy_credit), min(increase, sell_credit)
            buy_credit -= reversed_buy
            sell_credit -= reversed_sell
            sell_credit += decrease-reversed_buy
            buy_credit += increase-reversed_sell
        row = dict(account_id=self.live.account_id, venue=self.live.venue, condition_id=position.condition_id,
            token_id=position.token_id, request_id=uuid4().hex, kind='inventory', state='terminal', order_id=None,
            quantity=str(position.quantity.value), price='0', observation_id=snapshot.observation.observation_id,
            generation=snapshot.current.cycle_generation, requested_at=self.live._utc_now().isoformat(),
            sales=sales, buys=buys, sell_credit=str(sell_credit), buy_credit=str(buy_credit),
            sequence=(old.get('sequence', 0)+1 if old else 1))
        self.anchors[identity] = row
        self.live.writer.offer(HoldingInventoryRecord(json.dumps(row, sort_keys=True)))
        return True

    def _position(self, snapshot, identity):
        positions = [p for p in snapshot.positions if (p.account_id.casefold(), p.condition_id, p.token_id)
            == (identity[0].casefold(), *identity[1:])]
        if len(positions) == 1:
            return positions[0]
        if not positions and self.live._positions_full_coverage and self._account_known(snapshot, self.live._utc_now()):
            stamp = snapshot.observation.components['positions']
            # Local proof projection only; never add a fabricated canonical row.
            return CurrentPositionFact(self.live.account_id, *identity[1:], Fact('known', Decimal(0), (stamp,)),
                Fact('unknown'), Fact('unknown'), Fact('unknown'), stamp)
        return None

    def observe_frame(self, snapshot):
        identities = {(p.account_id.casefold(), p.condition_id, p.token_id) for p in snapshot.positions} | set(self.anchors)
        for identity in identities:
            position = self._position(snapshot, identity)
            if position is not None and position.quantity.state == 'known':
                self.observe_account(snapshot, position)

    def account_converged(self, snapshot, position):
        anchor = self.anchors.get((position.account_id.casefold(), position.condition_id, position.token_id))
        if anchor is None or Decimal(anchor['quantity']) != position.quantity.value:
            return False
        return all(o.size_matched.state == 'known' and o.size_matched.value <= Decimal(
            anchor['sales' if o.side == 'SELL' else 'buys'].get(o.key[3], '0'))
            for o in snapshot.orders if o.key[:3] == (position.account_id, position.condition_id, position.token_id))

    def session_intent(self, scope, attempt, record, origin):
        row = dict(account_id=self.live.account_id, venue=self.live.venue, condition_id=scope[1], token_id=scope[2],
            request_id=attempt.request_id, kind='session', state='pending', order_id=None,
            quantity=str(attempt.submit_quantity.value), price=str(attempt.price.value),
            observation_id=record.observation_id, generation=record.cycle_generation,
            requested_at=record.requested_at.isoformat(), session_scope=list(scope),
            account_quantity=str(origin.account_quantity if origin.account_quantity is not None else origin.residual))
        self.requests[attempt.request_id] = row
        return row

    def finish_session(self, row, outcome, order_id, *, denied=False):
        state = ('rejected' if denied else 'unknown') if outcome == 'not_sent' else outcome
        self._save(dict(row, state=state, order_id=order_id))

    def sync_session_terminals(self, snapshot):
        attempts = [a for sell in snapshot.sells.values() for a in (sell.passive, sell.protected) if a is not None]
        attempts += list(snapshot.resolved_exit_attempts.values())
        for attempt in attempts:
            row = self.requests.get(attempt.request_id)
            if row is not None and attempt.native_state == 'ended' and attempt.state == 'terminal' and row['state'] != 'terminal':
                self._save(dict(row, state='terminal'))
        for row in tuple(self.requests.values()):
            if row.get('kind') in ('inventory', 'cancel') or row['state'] in ('terminal', 'rejected'):
                continue
            if any(h.identity.request_id == row['request_id'] and h._thread.is_alive()
                    for h in self.live._native_jobs.values()):
                continue
            scope = tuple(row.get('session_scope', ()))
            closed = scope in snapshot.openings and snapshot.openings[scope].phase == 'closed'
            order = next((o for o in snapshot.orders if o.key == (
                self.live.account_id, row['condition_id'], row['token_id'], row.get('order_id'))), None)
            if closed:
                # Retirement already required exact fills, current zero and
                # ended native owners before removing the episode's attempts.
                self._save(dict(row, state='terminal'))
                continue
            if row.get('kind') == 'session' or order is None or order.terminal_proof is None:
                continue
            proof = order.terminal_proof
            position = self._position(snapshot, order.key[:3])
            observation = snapshot.observation
            if (proof.identity_verified and proof.matched.state == 'known'
                    and proof.original_size.value == Decimal(row['quantity'])
                    and position is not None and position.quantity.state == 'known'
                    and self.account_converged(snapshot, position)
                    and observation.components['positions'].checked_at >= proof.source.checked_at
                    and observation.order_view.orders_source.checked_at >= proof.source.checked_at):
                self._save(dict(row, state='terminal'))

    def attribution_uncertain(self, identity):
        return any(r.get('kind') not in ('inventory', 'cancel', 'session')
            and r['state'] in ('pending', 'unknown') and not r.get('order_id')
            and self._identity(r) == (identity[0].casefold(), *identity[1:3]) for r in self.requests.values())

    def is_holding_order(self, key):
        # Before a POST returns its canonical ID, a newly visible account SELL
        # cannot safely be enrolled as a session sale. Exact existing episode
        # associations remain intact; an uncertain holding never widens them.
        return self.attribution_uncertain(key[:3]) or any(
            r.get('kind') not in ('inventory', 'cancel', 'session') and r.get('order_id') == key[3]
            and self._identity(r) == (key[0].casefold(), *key[1:3]) for r in self.requests.values())

    def _refresh_legacy_pending(self):
        """Legacy requests reserve exposure; audit-only rows confer no authority."""
        try:
            self._legacy_pending = set(self.live.store.lp_unresolved_sell_obligations(self.live.account_id))
        except Exception:
            self._legacy_pending = None

    def _cost(self, snapshot, position):
        identity = (position.account_id, position.condition_id, position.token_id)
        sessions, complete = self.session_inventory(snapshot, identity)
        account = position.average_price.value if position.average_price.state == 'known' else None
        account = account if account is not None and account.is_finite() and account > 0 else None
        local = [s['average'] for s in sessions if s['average'] is not None and s['average'].is_finite() and s['average'] > 0]
        if sessions and complete and sum((s['residual'] for s in sessions), Decimal(0)) < position.quantity.value:
            return (max([account, *local]), 'mixed_max') if account is not None else (None, 'unknown')
        if sessions and complete and len(local) == len([s for s in sessions if s['residual'] > 0]) and local:
            return max(local), 'session_ledger'
        return (account, 'account_average') if account is not None else (None, 'unknown')

    def _account_known(self, snapshot, now):
        observation = snapshot.observation
        if (observation is None or not observation.orders_complete or not observation.positions_complete
                or observation.source_frame.account.casefold() != self.live.account_id.casefold()
                or observation.cycle_generation != snapshot.current.cycle_generation
                or len({(p.condition_id, p.token_id) for p in snapshot.positions}) != len(snapshot.positions)):
            return False
        try:
            for stamp in (observation.order_view.orders_source, observation.components['positions']):
                if stamp.state != 'known' or not stamp.complete:
                    return False
                _freshness(stamp.checked_at, now, 'holding_account_stale')
        except ValueError:
            return False
        return True

    @staticmethod
    def _identity(row):
        return row['account_id'].casefold(), row['condition_id'], row['token_id']

    def _market(self, snapshot, position):
        source = snapshot.observation.source_frame.sources.get('market:' + position.condition_id + ':' + position.token_id)
        if source is None or not source.complete or source.error or not isinstance(source.value, Mapping):
            return None
        metadata, book = source.value.get('metadata'), source.value.get('book')
        if (not isinstance(metadata, Mapping) or not isinstance(book, Mapping)
                or metadata.get('condition_id') != position.condition_id
                or metadata.get('closed') is not False or metadata.get('accepting_orders') is not True
                or metadata.get('resolved') is True
                or (book.get('condition_id'), book.get('token_id')) != (position.condition_id, position.token_id)):
            return None
        try:
            _freshness(source.checked_at, self.live._utc_now(), 'holding_market_stale')
            _freshness(book.get('received_at'), self.live._utc_now(), 'book_freshness')
            _freshness(book.get('timestamp'), self.live._utc_now(), 'book_timestamp')
        except ValueError:
            return None
        return book

    def project(self, now):
        """Read one immutable account frame; projection never acquires trading IO."""
        live = self.live
        with live.state._lock:
            snapshot = live.state.snapshot()
            items = []
            for position in snapshot.positions:
                if position.quantity.state != 'known' or position.quantity.value <= 0:
                    continue
                cost, source_name = self._cost(snapshot, position)
                observation = snapshot.observation
                source = observation.source_frame.sources.get('market:' + position.condition_id + ':' + position.token_id) if observation else None
                data = source.value if source and source.complete and not source.error and isinstance(source.value, Mapping) else {}
                book = data.get('book') if isinstance(data.get('book'), Mapping) else {}
                try:
                    floor = passive_exit_price(0, cost, book.get('tick_size'))
                except ValueError:
                    floor = None
                sells = [o for o in snapshot.active_orders if o.side == 'SELL'
                    and o.key[:3] == (position.account_id, position.condition_id, position.token_id)]
                covered = min(position.quantity.value, sum((o.remaining.value for o in sells), Decimal(0)))
                uncovered = position.quantity.value - covered
                state = 'cost_unknown' if cost is None else 'covered' if not uncovered else 'pending'
                if uncovered > 0 and cost is not None and floor is None and _maybe_decimal(book.get('tick_size')) is not None:
                    state = 'cost_above_maximum'
                minimum = _maybe_decimal(book.get('min_order_size'))
                if uncovered > 0 and cost is not None:
                    if uncovered < Decimal('.01'):
                        state = 'dust'
                    elif minimum is not None and uncovered.quantize(Decimal('.01'), rounding='ROUND_DOWN') < minimum:
                        state = 'below_minimum'
                metadata = data.get('metadata') if isinstance(data.get('metadata'), Mapping) else {}
                if uncovered > 0:
                    if metadata.get('closed') is True or metadata.get('resolved') is True:
                        state = 'market_ended'
                    elif metadata.get('closed') is not False or metadata.get('accepting_orders') is not True:
                        state = 'market_unknown'
                identity = (position.account_id, position.condition_id, position.token_id)
                if not self._account_known(snapshot, now):
                    state = 'account_unknown'
                elif not self.account_converged(snapshot, position):
                    state = 'position_unreconciled'
                elif any(self._identity(r) == (identity[0].casefold(), *identity[1:]) and
                        r['state'] in ('pending', 'unknown', 'cancel_unknown', 'finality_wait') for r in self.requests.values()):
                    state = 'order_unknown'
                elif state == 'pending' and any(r.get('reason') == 'admission_changed'
                        and r.get('observation_id') == observation.observation_id
                        and (r['account_id'], r['condition_id'], r['token_id']) == identity for r in self.requests.values()):
                    state = 'admission_changed'
                if state == 'pending' and self._legacy_pending is None:
                    state = 'legacy_obligation_unknown'
                try:
                    minimum_cost = passive_cost_floor(cost, book.get('tick_size'))
                except ValueError:
                    minimum_cost = None
                if (state not in ('account_unknown', 'position_unreconciled') and minimum_cost is not None
                        and any(o.price.value < minimum_cost for o in sells)):
                    state = 'below_cost_sell'
                below_cost_cancel = any(r.get('kind') == 'cancel'
                    and self._identity(r) == (identity[0].casefold(), *identity[1:])
                    and r['state'] in ('pending', 'cancel_unknown', 'finality_wait') for r in self.requests.values())
                reasons = dict(cost_unknown='成本未知', covered='已挂全', pending='等待当前挂卖准入',
                    dust='小于精度零头', below_minimum='低于最小下单量',
                    market_ended='市场已结束，不可挂单', market_unknown='市场状态未知',
                    account_unknown='账户观察不完整、身份未确认或已过期', admission_changed='准入时账户观察已改变，等待新一轮核对',
                    cost_above_maximum='成本过高，暂不挂卖',
                    legacy_obligation_unknown='历史卖单义务未确认，暂不新增挂卖')
                reasons.update(position_unreconciled='成交已确认，等待账户持仓收敛', order_unknown='卖单请求或撤单最终结果未知，保留数量')
                reasons['below_cost_sell'] = ('仍有低于成本卖单，撤单最终结果未确认' if below_cost_cancel
                    else '仍有低于成本卖单，等待安全纠正')
                raw = next((r for r in observation.source_frame.positions if
                    (r['condition_id'], r['token_id']) == (position.condition_id, position.token_id)), {}) if observation else {}
                current_value = _maybe_decimal(raw.get('current_value'))
                items.append(dict(condition_id=position.condition_id, token_id=position.token_id,
                    quantity=str(position.quantity.value), cost_source=source_name,
                    cost_floor=str(floor) if floor is not None else None,
                    current_value=str(current_value) if current_value is not None else None,
                    covered_quantity=str(covered), uncovered_quantity=str(uncovered), state=state,
                    reason=reasons[state], minimum_quantity=str(minimum) if minimum is not None else None,
                    sells=[dict(order_id=o.key[3], quantity=str(o.remaining.value), price=str(o.price.value)) for o in sells]))
            known = self._account_known(snapshot, now)
            total = sum((Decimal(r['quantity']) for r in items), Decimal(0))
            covered = sum((Decimal(r['covered_quantity']) for r in items), Decimal(0))
            uncovered = total - covered
            unsafe = any(r['state'] == 'below_cost_sell' for r in items)
            status = ('unknown' if not known else 'abnormal' if unsafe or any(r['state'] == 'cost_unknown' for r in items)
                else 'attention' if uncovered or any(r['state'] != 'covered' for r in items) else 'normal')
            reason = ('账户观察未确认，覆盖汇总未知' if not known else '仍有低于成本卖单，请核对撤单最终结果'
                if unsafe else '账户持仓的成本与挂卖覆盖')
            return dict(status=status, reason=reason,
                account_id=live.account_id, observed_at=snapshot.observation.accepted_at.isoformat() if snapshot.observation else None,
                items=items, position_quantity=str(total) if known else None,
                covered_quantity=str(covered) if known else None, uncovered_quantity=str(uncovered) if known else None,
                reasoned_quantity=str(uncovered) if known else None)

    def reserved(self, identity):
        return sum((Decimal(r['quantity']) for r in self.requests.values()
            if self._identity(r) == (identity[0].casefold(), *identity[1:])
            and r['state'] not in ('rejected', 'terminal')), Decimal(0))

    def _available(self, snapshot, position, *, ignore_request=None, exclude_order=None):
        identity = (position.account_id, position.condition_id, position.token_id)
        sells = [o for o in snapshot.active_orders if o.key[:3] == identity and o.side == 'SELL']
        ids = {o.key[3] for o in sells}
        reserved = sum((Decimal(r['quantity']) for r in self.requests.values()
            if self._identity(r) == (identity[0].casefold(), *identity[1:])
            and r['request_id'] != ignore_request and r['state'] not in ('rejected', 'terminal')
            and r.get('order_id') not in ids), Decimal(0))
        recorded = {r['request_id'] for r in self.requests.values() if r['state'] not in ('terminal', 'rejected')}
        for scope, sell in snapshot.sells.items():
            if scope[:3] != identity:
                continue
            for attempt in (sell.passive, sell.protected):
                if (attempt is not None and attempt.request_id != ignore_request and attempt.request_id not in recorded
                        and (attempt.native_state != 'ended' or attempt.state not in ('terminal', 'rejected'))
                        and (attempt.order_key is None or attempt.order_key[3] not in ids)):
                    reserved += attempt.submit_quantity.value
        return position.quantity.value - sum((o.remaining.value for o in sells if o.key[3] != exclude_order), Decimal(0)) - reserved

    def _eligible(self, snapshot, identity, *, allow_session=False):
        live = self.live
        observation = snapshot.observation
        if (not self._account_known(snapshot, live._utc_now())
                or not live._protection_frame_current(snapshot)
                or not live._dispatch_current(snapshot.current.cycle_generation, running=True)):
            return False
        if self._legacy_pending is None or any(token == identity[2] and condition in ('', identity[1])
                for condition, token in self._legacy_pending):
            return False
        # Session exits retain priority and all historical unresolved obligations.
        position = next((p for p in snapshot.positions if (p.account_id,p.condition_id,p.token_id) == identity), None)
        sessions, complete = self.session_inventory(snapshot, identity)
        if position is None or not complete:
            return False
        if not self.account_converged(snapshot, position):
            return False
        if any(self._identity(r) == (identity[0].casefold(), *identity[1:])
                and r.get('kind') == 'session' and r['state'] in ('pending', 'unknown') for r in self.requests.values()):
            return False
        if any(scope[:3] == identity and any(a is not None and
                (a.native_state != 'ended' or a.state not in ('terminal', 'rejected'))
                and (a.kind == 'protected_exit' or a.state != 'accepted' or a.native_state != 'ended'
                    or not any(o.key == a.order_key for o in snapshot.active_orders))
                for a in (sell.passive, sell.protected)) for scope, sell in snapshot.sells.items()):
            return False
        return True

    def _save(self, row):
        # Current observations are memory-owned and audited asynchronously.
        # A mutation/release still requires its latest restart guard and intent
        # durably committed outside CurrentLPState before any external action.
        anchor = self.anchors.get(self._identity(row))
        if anchor is not None:
            self.live.store.lp_save_holding_sell(anchor)
        self.live.store.lp_save_holding_sell(row)
        with self.live.state._lock:
            self.requests[row['request_id']] = row

    def _refresh_orders(self):
        """Read exact registered holding targets through the existing read lane."""
        live = self.live
        for row in tuple(self.requests.values()):
            if (row.get('kind') in ('inventory', 'cancel', 'session') or row['state'] in ('terminal', 'rejected')
                    or not row.get('order_id') or any(h.identity.request_id == row['request_id']
                        and h._thread.is_alive() for h in live._native_jobs.values())):
                continue
            snapshot = live.state.snapshot()
            key = (live.account_id, row['condition_id'], row['token_id'], row['order_id'])
            order = next((o for o in snapshot.orders if o.key == key), None)
            if order is not None:
                live._refresh_protection_order(order, time.monotonic()+2)
                continue
            source = live._read_protection('order', key[1:], time.monotonic()+2)
            value = source.value
            if (not source.complete or source.error or not isinstance(value, Mapping)
                    or (value.get('side'), value.get('price'), value.get('original_size')) !=
                        ('SELL', Decimal(row['price']), Decimal(row['quantity']))):
                continue
            stamp = live._source_stamp(source)
            fact = lambda amount: Fact('known', amount, (stamp,))
            proof = TerminalProof(key, value['status'], fact(value['original_size']), fact(value['size_matched']), stamp, True) if value['status'] in TERMINAL_ORDER_STATES else None
            incoming = CurrentOrderFact(key, 'SELL', value['status'], fact(value['price']), fact(value['original_size']),
                fact(value['size_matched']), fact(value['remaining_size']), 'present', None,
                fact(value['order_type']), fact(value['expiration']), 1, stamp)
            current = live.state.snapshot()
            live.state.accept_receipt_evidence(ReceiptEvidence(uuid4().hex, row['request_id'],
                live.account_id, live.process_epoch, current.current.cycle_generation, key, 'SELL', 0,
                order_fact=incoming, terminal_proof=proof))

    def _refresh_cancel_orders(self, row):
        """Resolve readonly BUY/SELL quantities retained by this cancel baseline."""
        live = self.live
        targets = [(side, order_id, terms, row.get(baseline, {}).get(order_id, '0'))
            for side, field, baseline in (('BUY', 'buy_terms', 'buy_matched_baseline'), ('SELL', 'sell_terms', 'matched_baseline'))
            for order_id, terms in row.get(field, {}).items()]
        for side, order_id, terms, baseline in targets:
            key = (live.account_id, row['condition_id'], row['token_id'], order_id)
            current = live.state.snapshot()
            order = next((o for o in current.orders if o.key == key), None)
            if order is not None:
                live._refresh_protection_order(order, time.monotonic()+2)
                continue
            source = live._read_protection('order', key[1:], time.monotonic()+2)
            value = source.value
            if (not source.complete or source.error or not isinstance(value, Mapping)
                    or (value.get('side'), value.get('price'), value.get('original_size')) !=
                        (side, Decimal(terms['price']), Decimal(terms['original_size']))
                    or value['size_matched'] < Decimal(baseline)):
                continue
            stamp = live._source_stamp(source)
            fact = lambda amount: Fact('known', amount, (stamp,))
            proof = TerminalProof(key, value['status'], fact(value['original_size']), fact(value['size_matched']), stamp, True) if value['status'] in TERMINAL_ORDER_STATES else None
            incoming = CurrentOrderFact(key, side, value['status'], fact(value['price']), fact(value['original_size']),
                fact(value['size_matched']), fact(value['remaining_size']), 'present', None,
                fact(value['order_type']), fact(value['expiration']), 1, stamp)
            current = live.state.snapshot()
            live.state.accept_receipt_evidence(ReceiptEvidence(uuid4().hex, row['request_id'],
                live.account_id, live.process_epoch, current.current.cycle_generation, key, side, 0,
                order_fact=incoming, terminal_proof=proof))

    def _reconcile_cancels(self):
        live = self.live
        for row in tuple(self.requests.values()):
            if row.get('kind') != 'cancel' or row['state'] in ('terminal', 'rejected'):
                continue
            with live.state._lock:
                snapshot = live.state.snapshot()
                identity = (live.account_id, row['condition_id'], row['token_id'])
                if not self._account_known(snapshot, live._utc_now()):
                    continue
                if any(h.identity.request_id == row['request_id'] and not h.done() for h in live._native_jobs.values()):
                    continue
            if row['state'] != 'finality_wait':
                source = live._read_protection('order', (row['condition_id'], row['token_id'], row['order_id']), time.monotonic() + 2)
                value = source.value
                if (not source.complete or source.error or not isinstance(value, Mapping)
                        or value.get('status') not in TERMINAL_ORDER_STATES
                        or value.get('side') != 'SELL' or value.get('price') != Decimal(row['price'])
                        or value.get('original_size') != Decimal(row['original_size'])
                        or value.get('size_matched') is None
                        or not Decimal(row['matched_before']) <= value['size_matched'] <= Decimal(row['original_size'])):
                    continue
                stamp = live._source_stamp(source)
                try:
                    _freshness(stamp.checked_at, live._utc_now(), 'holding_finality_stale')
                except ValueError:
                    continue
                key = (*identity, row['order_id'])
                fact = lambda amount: Fact('known', amount, (stamp,))
                proof = TerminalProof(key, value['status'], fact(value['original_size']), fact(value['size_matched']), stamp, True)
                with live.state._lock:
                    current = live.state.snapshot()
                    order = next((o for o in current.orders if o.key == key), None)
                    incoming = None
                    if order is None:
                        incoming = CurrentOrderFact(key, 'SELL', value['status'], fact(value['price']),
                            fact(value['original_size']), fact(value['size_matched']),
                            fact(value['original_size'] - value['size_matched']), 'present', None,
                            fact(value['order_type']), fact(value['expiration']), 1, stamp)
                    result = live.state.accept_receipt_evidence(ReceiptEvidence(uuid4().hex, row['request_id'],
                        live.account_id, live.process_epoch, current.current.cycle_generation, key, 'SELL',
                        order.terms_version if order else 0, terminal_proof=proof, order_fact=incoming))
                    if result != 'applied':
                        continue
                self._save(dict(row, state='finality_wait', final_matched=str(value['size_matched']),
                    final_checked_at=stamp.checked_at.isoformat(), final_observation_id=current.observation.observation_id))
                live.writer.offer(ProgressRecord(live.account_id, live.process_epoch, row['request_id'],
                    'holding:' + row['request_id'], row['condition_id'], row['token_id'], row['order_id'],
                    value['original_size'], value['size_matched'], value['price'], value['status'], stamp.checked_at))
                continue
            self._refresh_cancel_orders(row)
            with live.state._lock:
                snapshot = live.state.snapshot()
                observation = snapshot.observation
                position = self._position(snapshot, identity)
                baseline = row.get('matched_baseline')
                sales = {o.key[3]: str(o.size_matched.value) for o in snapshot.orders
                    if o.key[:3] == identity and o.side == 'SELL' and o.size_matched.state == 'known'}
                anchor = self.anchors.get((identity[0].casefold(), *identity[1:]))
                sales = {**(anchor.get('sales', {}) if anchor else {}), **sales}
                delta = (sum((Decimal(q)-Decimal(baseline.get(k, '0')) for k,q in sales.items()), Decimal(0))
                    if baseline is not None else Decimal(row['final_matched']) - Decimal(row['matched_before']))
                buy_baseline = row.get('buy_matched_baseline', {})
                buys = {o.key[3]: str(o.size_matched.value) for o in snapshot.orders
                    if o.key[:3] == identity and o.side == 'BUY' and o.size_matched.state == 'known'}
                buys = {**(anchor.get('buys', {}) if anchor else {}), **buys}
                bought_delta = sum((Decimal(q)-Decimal(buy_baseline.get(k, '0')) for k,q in buys.items()), Decimal(0)) if 'buy_matched_baseline' in row else Decimal(0)
                quantities_complete = all(any(o.key == (*identity, order_id) and o.side == side
                    and o.price.value == Decimal(terms['price']) and o.original_size.value == Decimal(terms['original_size'])
                    and o.size_matched.state == 'known' and o.size_matched.value >= Decimal(before.get(order_id, '0'))
                    and (o.presence == 'present' or o.terminal_proof is not None and o.terminal_proof.identity_verified)
                    for o in snapshot.orders)
                    for side, field, before in (('BUY', 'buy_terms', buy_baseline), ('SELL', 'sell_terms', baseline or {}))
                    for order_id, terms in row.get(field, {}).items())
                expected = Decimal(row['position_quantity']) - delta + bought_delta
                siblings_final = all(r['state'] in ('finality_wait', 'terminal', 'rejected')
                    for r in self.requests.values() if r.get('kind') == 'cancel' and self._identity(r) == self._identity(row))
                fresh = (siblings_final and quantities_complete and delta >= 0 and bought_delta >= 0 and observation.observation_id != row['final_observation_id']
                    and observation.components['positions'].checked_at >= datetime.fromisoformat(row['final_checked_at'])
                    and observation.order_view.orders_source.checked_at >= datetime.fromisoformat(row['final_checked_at'])
                    and position is not None and position.quantity.state == 'known' and position.quantity.value == expected
                    and self.account_converged(snapshot, position))
            if fresh:
                for parent in tuple(self.requests.values()):
                    if parent.get('kind') != 'cancel' and parent.get('order_id') == row['order_id']:
                        self._save(dict(parent, state='terminal'))
                self._save(dict(row, state='terminal'))

    def _cancel_current(self, snapshot, position, order, origin, row, lease):
        live = self.live
        identity = (position.account_id, position.condition_id, position.token_id)
        if (not self._eligible(snapshot, identity, allow_session=True)
                or snapshot.observation is not origin.observation or snapshot.trade_version != origin.trade_version
                or position not in snapshot.positions or order not in snapshot.active_orders
                or order.side != 'SELL' or order.key[:3] != identity
                or order.key in live._opening_order_associations):
            return False
        book = self._market(snapshot, position)
        if book is None:
            return False
        try:
            cost, _ = self._cost(snapshot, position)
            floor = passive_cost_floor(cost, book.get('tick_size'))
            if row['trigger'] == 'below_floor':
                # Ray t531u permits verified same-wallet price correction even
                # without local provenance; this grants no generic ownership.
                return order.price.value < floor
            registered = any(r.get('kind') not in ('cancel', 'inventory', 'session') and r['order_id'] == order.key[3]
                and r['state'] == 'accepted' for r in self.requests.values())
            if row['trigger'] != 'reprice' or not registered:
                return False
            _freshness(book.get('received_at'), live._utc_now(), 'book_freshness')
            _freshness(book.get('timestamp'), live._utc_now(), 'book_timestamp')
            own_at_price = {}
            for other in snapshot.active_orders:
                if other.key[:3] == identity and other.side == 'SELL':
                    own_at_price[other.price.value] = own_at_price.get(other.price.value, Decimal(0)) + other.remaining.value
            ask = min((p for p, size in _levels(book.get('asks'), 'asks')
                if size > own_at_price.get(p, 0)), default=floor)
            return order.price.value != passive_exit_price(ask, cost, book.get('tick_size'))
        except (ValueError, AttributeError):
            return False

    def _start_cancel(self, position, order, origin, *, trigger='below_floor'):
        live = self.live
        lease = live._acquire_mutation('SELL_CANCEL')
        if lease is None:
            return None
        transferred = False
        try:
            row = dict(account_id=live.account_id, venue=live.venue, request_id=uuid4().hex,
                condition_id=position.condition_id, token_id=position.token_id, quantity=str(order.remaining.value),
                price=str(order.price.value), state='pending', order_id=order.key[3], kind='cancel', trigger=trigger,
                original_size=str(order.original_size.value), matched_before=str(order.size_matched.value),
                position_quantity=str(position.quantity.value), matched_baseline={
                    **self.anchors.get((position.account_id.casefold(), position.condition_id, position.token_id), {}).get('sales', {}),
                    **{o.key[3]: str(o.size_matched.value) for o in origin.orders if o.key[:3] == order.key[:3]
                        and o.side == 'SELL' and o.size_matched.state == 'known'}},
                buy_matched_baseline={
                    **self.anchors.get((position.account_id.casefold(), position.condition_id, position.token_id), {}).get('buys', {}),
                    **{o.key[3]: str(o.size_matched.value) for o in origin.orders if o.key[:3] == order.key[:3]
                        and o.side == 'BUY' and o.size_matched.state == 'known'}},
                buy_terms={o.key[3]: dict(price=str(o.price.value), original_size=str(o.original_size.value))
                    for o in origin.orders if o.key[:3] == order.key[:3] and o.side == 'BUY'},
                sell_terms={o.key[3]: dict(price=str(o.price.value), original_size=str(o.original_size.value))
                    for o in origin.orders if o.key[:3] == order.key[:3] and o.side == 'SELL'
                    and all(f.state == 'known' for f in (o.price, o.original_size, o.size_matched))},
                observation_id=origin.observation.observation_id,
                generation=origin.current.cycle_generation, requested_at=live._utc_now().isoformat())
            if not lease.allowed('SELL_CANCEL'):
                return None
            with live.state._lock:
                if (not self._cancel_current(live.state.snapshot(), position, order, origin, row, lease)
                        or any(r.get('kind') == 'cancel' and r['order_id'] == row['order_id']
                            and r['state'] not in ('terminal', 'rejected') for r in self.requests.values())):
                    return None
                self.requests[row['request_id']] = row
            self._save(row)
            record = RequestRecord(live.account_id, live.process_epoch, row['request_id'], 'holding:' + row['request_id'],
                position.condition_id, position.token_id, 'CANCEL', order.price.value, order.remaining.value,
                live._utc_now(), order.key[3], live.venue, row['generation'], 'holding:' + row['request_id'],
                live.rounds.snapshot().round_number, row['observation_id'], 'GTC', None, None, ())
            live.writer.offer(record)
            def cancel():
                from .polymarket_lp_live_execution import _ProtectionOutcome
                outcome = 'unknown'
                try:
                    permitted = lease.allowed('SELL_CANCEL')
                    with live.state._lock:
                        permitted = permitted and self._cancel_current(live.state.snapshot(), position, order, origin, row, lease)
                    if permitted:
                        # Ray t531u price-correction permission includes unknown/peer
                        # same-wallet SELLs, without generic ownership enrollment.
                        response = live.trading.cancel_orders_detailed((order.key[3],))
                        outcome = 'accepted' if order.key[3] in response['canceled'] else 'unknown'
                    else:
                        outcome = 'not_sent'
                except Exception:
                    pass
                self._save(dict(row, state='rejected' if outcome == 'not_sent' else 'cancel_unknown'))
                live.writer.offer(ReceiptRecord(live.account_id, live.process_epoch, row['request_id'],
                    record.session_id, row['condition_id'], row['token_id'], row['order_id'], outcome, live._utc_now()))
                return _ProtectionOutcome(row['request_id'], outcome, row['order_id'])
            from .polymarket_lp_live_execution import _ProtectionAction
            action = _ProtectionAction(row['request_id'], live.account_id, (*order.key[:3], 'holding:' + row['request_id']), 'cancel_exit')
            handle = live._start_protection_native(action, cancel, lease=lease, diagnostic_record=record)
            transferred = True
            return handle
        finally:
            if not transferred:
                lease.release()

    def evaluate(self):
        live = self.live
        handles = []
        self._reconcile_cancels()
        self._refresh_orders()
        self.observe_frame(live.state.snapshot())
        self.sync_session_terminals(live.state.snapshot())
        for position in live.state.snapshot().positions:
            identity = (position.account_id, position.condition_id, position.token_id)
            snapshot = live.state.snapshot()
            for order in snapshot.active_orders:
                if order.key[:3] == identity and order.side == 'SELL':
                    handle = self._start_cancel(position, order, snapshot)
                    if handle is None:
                        handle = self._start_cancel(position, order, snapshot, trigger='reprice')
                    if handle is not None:
                        handles.append(handle)
            lease = live._acquire_mutation('SELL')
            if lease is None:
                continue
            transferred = False
            try:
                with live.state._lock:
                    snapshot = live.state.snapshot()
                    if (not self._eligible(snapshot, identity)
                            or position.quantity.state != 'known' or position.quantity.value <= 0
                            or len(live._placement_owners()) >= 2):
                        continue
                    source = snapshot.observation.source_frame.sources.get(
                        'market:' + position.condition_id + ':' + position.token_id)
                    if (source is None or not source.complete or source.error
                            or not isinstance(source.value, Mapping)):
                        continue
                    metadata, book = source.value.get('metadata'), source.value.get('book')
                    if (not isinstance(metadata, Mapping) or not isinstance(book, Mapping)
                            or metadata.get('condition_id') != position.condition_id
                            or metadata.get('closed') is not False or metadata.get('accepting_orders') is not True
                            or (book.get('condition_id'), book.get('token_id')) != identity[1:]):
                        continue
                    try:
                        _freshness(source.checked_at, live._utc_now(), 'holding_market_stale')
                        _freshness(book.get('received_at'), live._utc_now(), 'book_freshness')
                        _freshness(book.get('timestamp'), live._utc_now(), 'book_timestamp')
                        asks = _levels(book.get('asks'), 'asks')
                        available = self._available(snapshot, position)
                        sessions, complete = self.session_inventory(snapshot, identity)
                        if sessions:
                            available = min(available, max(Decimal(0), position.quantity.value - sum((s['residual'] for s in sessions), Decimal(0))))
                        quantity = normalize_sell_quantity(available)
                        minimum = _maybe_decimal(book.get('min_order_size'))
                        if minimum is None or quantity < minimum:
                            continue
                        own_at_price = {}
                        for order in snapshot.active_orders:
                            if order.key[:3] == identity and order.side == 'SELL':
                                own_at_price[order.price.value] = own_at_price.get(order.price.value, Decimal(0)) + order.remaining.value
                        ask = min((p for p, size in asks if size > own_at_price.get(p, 0)), default=Decimal(0))
                        average, _ = self._cost(snapshot, position)
                        price = passive_exit_price(ask, average, book.get('tick_size'))
                    except ValueError:
                        continue
                    request_id = uuid4().hex
                    row = dict(account_id=live.account_id, venue=live.venue, request_id=request_id,
                        condition_id=position.condition_id, token_id=position.token_id,
                        quantity=str(quantity), price=str(price), state='pending', order_id=None,
                        observation_id=snapshot.observation.observation_id,
                        generation=snapshot.current.cycle_generation, requested_at=live._utc_now().isoformat())
                    self.requests[request_id] = row
                # SQL admission occurs outside the state owner lock.
                self._save(row)
                from .polymarket_lp_live_execution import _ProtectionAction
                scope = (*identity, 'holding:' + request_id)
                action = _ProtectionAction(request_id, live.account_id, scope, 'holding_sell')
                record = RequestRecord(live.account_id, live.process_epoch, request_id, 'holding:' + request_id,
                    position.condition_id, position.token_id, 'SELL', price, quantity, live._utc_now(),
                    None, live.venue, row['generation'], 'holding:' + request_id,
                    live.rounds.snapshot().round_number, row['observation_id'], 'GTC', None, True, ())
                live.writer.offer(record)
                handle = live._start_protection_native(action,
                    lambda row=row, snapshot=snapshot, position=position, record=record, lease=lease:
                        self._execute(row, snapshot, position, record, lease), lease=lease, diagnostic_record=record)
                transferred = True
                handles.append(handle)
            finally:
                if not transferred:
                    lease.release()
        return tuple(handles)

    def _current(self, origin, position, row, lease):
        live = self.live
        permitted = lease.allowed('SELL')
        with live.state._lock:
            current = live.state.snapshot()
            identity = (position.account_id, position.condition_id, position.token_id)
            return (permitted and self._eligible(current, identity)
                and self._market(current, position) is not None
                and current.observation is origin.observation and current.trade_version == origin.trade_version
                and position in current.positions and self.requests.get(row['request_id']) == row
                and self._available(current, position, ignore_request=row['request_id']) >= Decimal(row['quantity']))

    def _execute(self, row, origin, position, record, lease):
        live = self.live
        outcome, order_id = 'not_sent', None
        try:
            if not self._current(origin, position, row, lease):
                return
            arguments = dict(token_id=row['token_id'], side='SELL', price=Decimal(row['price']),
                quantity=Decimal(row['quantity']), expiration=None, post_only=True)
            if live.lp_instance_id is not None:
                prepared = live.trading.lp_prepare_limit_order(**arguments)
                provenance = live.store.lp_register_order_provenance(prepared.provenance(
                    instance_id=live.lp_instance_id, request_id=row['request_id'],
                    condition_id=row['condition_id'], venue=live.venue))
                with live.state._lock:
                    live._order_provenance[prepared.order_id] = provenance
                signed = prepared.signed_order
                order_id = prepared.order_id
            else:
                signed = live.trading.lp_create_limit_order(**arguments)
            if not self._current(origin, position, row, lease):
                return
            # Persist a canonical ID before the external call when one is available.
            row = dict(row, state='unknown', order_id=order_id)
            live.store.lp_save_holding_sell(row)
            with live.state._lock:
                self.requests[row['request_id']] = row
            if not self._current(origin, position, row, lease):
                return
            outcome = 'unknown'
            response = live.trading.lp_post_order(signed)
            value = lambda name, default=None: response.get(name, default) if isinstance(response, Mapping) else getattr(response, name, default)
            returned_id = value('order_id', value('id'))
            if live.lp_instance_id is not None and returned_id != order_id:
                raise ValueError('lp_receipt_order_identity_mismatch')
            if value('accepted') is False or value('ok') is False:
                outcome = 'rejected'
            elif isinstance(returned_id, str) and returned_id:
                order_id, outcome = returned_id, 'accepted'
                stamp = SourceStamp(row['request_id'], checked_at=live._utc_now(), complete=True, state='known')
                fact = lambda value: Fact('known', value, (stamp,))
                matched = _maybe_decimal(value('making_amount'))
                if matched is not None and 0 <= matched <= Decimal(row['quantity']):
                    key = (live.account_id, row['condition_id'], row['token_id'], order_id)
                    order = CurrentOrderFact(key, 'SELL', str(value('status', 'ACCEPTED')).upper(),
                        fact(Decimal(row['price'])), fact(Decimal(row['quantity'])), fact(matched),
                        fact(Decimal(row['quantity']) - matched), 'present', None,
                        fact('GTC'), fact(None), 1, stamp)
                    live.state.accept_receipt_evidence(ReceiptEvidence(uuid4().hex, row['request_id'],
                        live.account_id, live.process_epoch, row['generation'], key, 'SELL', 0, order_fact=order))
        except Exception:
            # Any preparation/commit/transport uncertainty retains its reservation.
            if outcome == 'not_sent':
                outcome = 'unknown'
        finally:
            state = 'rejected' if outcome == 'not_sent' else outcome
            updated = dict(row, state=state, order_id=order_id,
                reason='admission_changed' if outcome == 'not_sent' else None)
            live.store.lp_save_holding_sell(updated)
            with live.state._lock:
                self.requests[row['request_id']] = updated
            live.writer.offer(ReceiptRecord(live.account_id, live.process_epoch, row['request_id'],
                record.session_id, row['condition_id'], row['token_id'], order_id, outcome, live._utc_now()))
