"""Account facts and temporary submission coverage, independent of audit outcome.

A complete account observation can replace an ended request's temporary hold.
It cannot establish that request's exchange identity or change its audit result.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Mapping
import uuid

from .polymarket_lp_risk import TERMINAL_ORDER_STATES, _freshness, _items, _timestamp
from .polymarket_trading import _lp_maker_order_is_self, _lp_trade, _model_dict

ZERO = Decimal('0')


def default_account_pool_document(path, pool_account_id: str | None, now: datetime) -> dict[str, object]:
    return dict(run_id=uuid.uuid5(uuid.NAMESPACE_URL, str(Path(path).resolve()) + str(pool_account_id)).hex,
        account_id=pool_account_id, config_version=0, desired_running=False, ever_enabled=False,
        enabled_at=None, target_buy_count=0, budget_usd=None, allocations=[], intents={}, events={},
        rounds={}, last_round={}, last_reconciled_at=None, updated_at=now.isoformat())


def _number(value, name, *, positive=False):
    try:
        if isinstance(value, bool) or value in (None, ''):
            raise ValueError(name)
        number = Decimal(str(value))
        if not number.is_finite() or number < 0 or (positive and number == 0):
            raise ValueError(name)
        return number
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(name) from exc


def reservation_is_covered(row: Mapping[str, object], account_id: str | None = None) -> bool:
    marker = row.get('reservation_coverage')
    if not isinstance(marker, Mapping) or marker.get('state') != 'covered' or marker.get('version') != 1:
        return False
    if str(marker.get('session_id') or '') != str(row.get('session_id') or ''):
        return False
    wallet = str(marker.get('account_id') or '').strip().casefold()
    if not wallet or marker.get('pool_account_id') != hashlib.sha256(wallet.encode()).hexdigest():
        return False
    row_account = str(row.get('account_id') or '').strip().casefold()
    if row_account and row_account not in {wallet, marker['pool_account_id']}:
        return False
    if account_id is not None and str(account_id).strip().casefold() not in {wallet, marker['pool_account_id']}:
        return False
    return bool(marker.get('snapshot_id') and marker.get('read_started_at'))


def _account_trade_report(snapshot, orders, positions, *, wallet, now):
    """Historical reporting can be incomplete without changing current exposure."""
    reasons: set[str] = set()
    started = _timestamp(snapshot['read_started_at'], name='read_started_at')
    fills: dict[tuple[str, str], dict[str, object]] = {}
    fill_identities = {}
    for raw in snapshot['raw_trades']:
        trade = _lp_trade(raw)
        if trade is None:
            raise ValueError('trade_fact_invalid')
        raw_mapping = _model_dict(raw) or {}
        candidates = []
        if trade['trader_side'] == 'TAKER':
            candidates.append((trade, raw_mapping, str(trade['taker_order_id']), 'size', 'taker'))
        raw_makers = raw_mapping.get('maker_orders') or ()
        raw_makers_by_id = {}
        for item in raw_makers:
            data = _model_dict(item)
            if data is not None:
                raw_makers_by_id[str(data.get('order_id') or data.get('id') or '')] = data
        for maker in trade['maker_orders']:
            if (trade['status'] != 'FAILED'
                    and not str(maker.get('maker_address') or '').strip()
                    and not str(maker.get('owner') or '').strip()):
                # Unattributable maker volume is not proof of foreign volume,
                # including when this row also contains our taker execution.
                reasons.add('trade_ownership_unknown')
            if _lp_maker_order_is_self(maker, wallet):
                candidates.append((maker, raw_makers_by_id.get(maker['order_id'], {}), maker['order_id'], 'matched_amount', 'maker'))
        if not candidates and not trade['maker_orders'] and trade['status'] != 'FAILED':
            reasons.add('trade_ownership_unknown')
        if not candidates or trade['status'] == 'FAILED':
            continue
        if trade['status'] != 'CONFIRMED':
            reasons.add('trade_confirmation_unknown')
        for fill, source, oid, quantity_key, role in candidates:
            token = str(fill.get('token_id') or '')
            side = str(fill.get('side') or '')
            if not oid or not token or side not in {'BUY', 'SELL'}:
                raise ValueError('trade_identity_unknown')
            identity = (token, side)
            if oid in fill_identities and fill_identities[oid] != identity:
                raise ValueError('order_identity_conflict')
            fill_identities[oid] = identity
            amount = _number(fill.get(quantity_key), 'trade_fill_unknown', positive=True)
            price = _number(fill.get('price'), 'trade_price_unknown', positive=True)
            if price > 1:
                raise ValueError('trade_price_unknown')
            fee = source.get('fee', source.get('fees'))
            if fee is not None:
                fee = _number(fee, 'trade_fee_unknown')
            elif fill.get('fee_rate_bps') == ZERO:
                fee = ZERO
            else:
                # Same historical fee rules as session reconciliation, only
                # when callers supplied fresh market facts before this pure
                # calculation. No metadata read occurs inside publication.
                market = (snapshot.get('fee_metadata_by_token') or {}).get(token)
                if isinstance(market, Mapping):
                    try:
                        _freshness(market.get('fees_checked_at'), now, 'fees_freshness')
                        if market.get('fees_enabled') is False:
                            fee = ZERO
                        elif role == 'maker' and _number(market.get('fee'), 'trade_fee_unknown') == ZERO:
                            fee = ZERO
                        elif role == 'taker':
                            rate = _number(market.get('taker_fee_rate', market.get('fee_rate')), 'trade_fee_unknown')
                            exponent = _number(market.get('fee_exponent', 1), 'trade_fee_unknown')
                            fee = (amount * rate * (price * (Decimal(1) - price)) ** exponent).quantize(Decimal('0.00001'))
                    except (ValueError, InvalidOperation):
                        fee = None
                if fee is None:
                    reasons.add('trade_fee_unknown')
            timestamp = trade.get('matched_at')
            row = dict(order_id=oid, token_id=token, side=side, quantity=amount,
                price=price, fee=fee, matched_at=timestamp, status=trade['status'])
            key = (trade['id'], oid)
            if key in fills and fills[key] != row:
                raise ValueError('trade_fill_conflict')
            fills[key] = row
    # Every authenticated order's cumulative fills must be represented in the
    # raw account history. A matched receipt alone never supplies its fees.
    for oid, row in orders.items():
        covered = sum((fill['quantity'] for fill in fills.values() if fill['order_id'] == oid), ZERO)
        if covered != Decimal(row['filled_quantity']):
            reasons.add('order_fill_coverage_unknown')
        for fill in fills.values():
            if fill['order_id'] == oid and (fill['token_id'] != row['token_id'] or fill['side'] != row['side']):
                raise ValueError('order_identity_conflict')
    realized = ZERO
    for token in set(positions) | {fill['token_id'] for fill in fills.values()}:
        token_fills = [fill for fill in fills.values() if fill['token_id'] == token]
        has_sells = any(fill['side'] == 'SELL' for fill in token_fills)
        if has_sells and any(fill['matched_at'] is None for fill in token_fills):
            reasons.add('trade_time_unknown')
        # Ambiguous same-time buy/sell order cannot prove cost allocation.
        if has_sells:
            sides_at = {}
            for fill in token_fills:
                sides_at.setdefault(fill['matched_at'], set()).add(fill['side'])
            if any(len(sides) > 1 for sides in sides_at.values()):
                reasons.add('trade_time_ambiguous')
        token_fills.sort(key=lambda fill: (fill['matched_at'] or started, fill['order_id']))
        quantity = cost = pnl = ZERO
        for fill in token_fills:
            amount, price, fee = fill['quantity'], fill['price'], fill['fee']
            # Unknown cost components block the whole result below. This
            # subtotal is never published as known or as zero exposure.
            value = amount * price
            if fill['side'] == 'BUY':
                quantity += amount
                cost += value + (fee if fee is not None else ZERO)
            elif quantity < amount:
                reasons.add('position_cost_unknown')
                quantity -= amount
            else:
                released = cost * amount / quantity
                quantity -= amount
                cost -= released
                pnl += value - (fee if fee is not None else ZERO) - released
        position = positions.get(token, ZERO)
        if position != quantity:
            reasons.add('position_cost_unknown' if position > 0 and not token_fills else 'position_mismatch')
        realized += pnl
    confirmed = []
    for (trade_id, order_id), fill in sorted(fills.items()):
        if fill['status'] != 'CONFIRMED' or fill['fee'] is None:
            continue
        economics = {key: ('0' if fill[key] == ZERO else str(fill[key].normalize()))
                     for key in ('quantity', 'price', 'fee')}
        economics.update(token_id=fill['token_id'], side=fill['side'])
        confirmed.append(dict(trade_id=trade_id, order_id=order_id,
            fingerprint=hashlib.sha256(json.dumps(economics, sort_keys=True, separators=(',', ':')).encode()).hexdigest()))
    return dict(
        realized_pnl_usd=str(realized) if not reasons else None,
        report_status='unknown' if reasons else 'known', report_reason_codes=sorted(reasons),
        confirmed_fill_facts=confirmed,
        order_fills={oid: str(sum((fill['quantity'] for fill in fills.values()
            if fill['order_id'] == oid), ZERO)) for oid in set(orders) | {fill['order_id'] for fill in fills.values()}},
    )


def build_account_financial_facts(
    snapshot: Mapping[str, object], *, now: datetime, expected_account_id: str | None = None,
) -> dict[str, object]:
    """Validate an authenticated round and price actual account exposure once.

    Current positions and open orders are authoritative for exposure. Trade
    history supplies reports only; gaps never veto a complete current snapshot.
    """
    if snapshot.get('authenticated') is not True:
        raise ValueError('account_snapshot_unknown')
    required = ('balance_complete', 'open_orders_complete', 'positions_complete',
                'trades_complete', 'pagination_complete')
    if any(snapshot.get(key) is not True for key in required):
        raise ValueError('account_snapshot_incomplete')
    wallet = str(snapshot.get('wallet_address') or '').strip().casefold()
    if (not wallet or str(snapshot.get('account_id') or '').strip().casefold() != wallet
            or expected_account_id is not None and str(expected_account_id).strip().casefold() != wallet):
        raise ValueError('account_identity_mismatch')
    generation = snapshot.get('trade_generation')
    if type(generation) is not int or generation < 0:
        raise ValueError('account_round_invalid')
    started = _timestamp(snapshot.get('read_started_at'), name='read_started_at')
    checked = _timestamp(snapshot.get('checked_at'), name='checked_at')
    ended = _timestamp(snapshot.get('read_ended_at'), name='read_ended_at')
    if not started <= checked <= ended:
        raise ValueError('account_read_order_invalid')
    _freshness(checked, now, 'account_freshness')
    _freshness(ended, now, 'account_freshness')
    for name in ('open_orders', 'positions', 'raw_trades'):
        if not isinstance(snapshot.get(name), (tuple, list)):
            raise ValueError('account_' + name + '_unknown')
    balance = _number(snapshot.get('balance'), 'account_balance_unknown')
    allowance = _number(snapshot.get('allowance'), 'account_allowance_unknown')
    reasons: set[str] = set()
    orders: dict[str, dict[str, object]] = {}
    for raw in snapshot['open_orders']:
        if not isinstance(raw, Mapping):
            raise ValueError('account_order_identity_unknown')
        oid = str(raw.get('order_id') or raw.get('id') or '').strip()
        token = str(raw.get('token_id') or raw.get('asset_id') or '').strip()
        side = str(raw.get('side') or '').upper()
        status = str(raw.get('status') or '').upper()
        if not oid or not token or side not in {'BUY', 'SELL'} or not status:
            raise ValueError('account_order_identity_unknown')
        price = _number(raw.get('price'), 'order_price_unknown', positive=True)
        if price > 1:
            raise ValueError('order_price_unknown')
        quantity = _number(raw.get('original_size', raw.get('quantity')), 'order_quantity_unknown')
        filled = _number(raw.get('size_matched'), 'order_fill_unknown')
        if filled > quantity:
            raise ValueError('order_fill_exceeds_quantity')
        remaining = quantity - filled
        if status in {'FILLED', 'MATCHED'} and (quantity == ZERO or filled != quantity):
            reasons.add('order_fill_coverage_unknown')
        if raw.get('remaining_size') is not None and _number(raw['remaining_size'], 'order_remaining_unknown') != remaining:
            raise ValueError('order_remaining_conflict')
        record = dict(order_id=oid, token_id=token, condition_id=str(raw.get('condition_id') or raw.get('market') or ''),
            side=side, status=status, price=str(price), quantity=str(remaining),
            original_quantity=str(quantity), filled_quantity=str(filled))
        if oid in orders and orders[oid] != record:
            raise ValueError('order_identity_conflict')
        orders[oid] = record
    positions: dict[str, Decimal] = {}
    position_costs: dict[str, Decimal | None] = {}
    for raw in snapshot['positions']:
        if not isinstance(raw, Mapping):
            raise ValueError('account_position_unknown')
        token = str(raw.get('token_id') or raw.get('asset_id') or raw.get('asset') or '').strip()
        if not token:
            raise ValueError('account_position_unknown')
        quantity = _number(raw.get('size', raw.get('quantity')), 'account_position_unknown')
        if token in positions and positions[token] != quantity:
            raise ValueError('account_position_conflict')
        cost = None
        try:
            if quantity == ZERO:
                cost = ZERO
            elif (raw.get('redeemable') is True
                  and raw.get('current_price') is not None and raw.get('current_value') is not None
                  and _number(raw['current_price'], 'account_position_cost_unknown') == ZERO
                  and _number(raw['current_value'], 'account_position_cost_unknown') == ZERO):
                # Redeemable zero-payout tokens can remain in the API position
                # list after resolution. They no longer tie up strategy capital.
                cost = ZERO
            elif raw.get('initial_value') is not None:
                cost = _number(raw['initial_value'], 'account_position_cost_unknown')
            else:
                average = _number(raw.get('average_price'), 'account_position_cost_unknown')
                if average > 1:
                    raise ValueError('account_position_cost_unknown')
                cost = quantity * average
        except ValueError:
            reasons.add('account_position_cost_unknown')
        if token in position_costs and position_costs[token] != cost:
            raise ValueError('account_position_conflict')
        positions[token] = quantity
        position_costs[token] = cost
    try:
        report = _account_trade_report(snapshot, orders, positions, wallet=wallet, now=now)
    except ValueError as exc:
        report = dict(realized_pnl_usd=None, report_status='unknown',
                      report_reason_codes=[str(exc)], confirmed_fill_facts=[], order_fills={})
    buys = []
    for row in orders.values():
        if row['status'] in TERMINAL_ORDER_STATES or row['side'] != 'BUY' or Decimal(row['quantity']) == ZERO:
            continue
        state = ('canceling' if row['status'] in {'CANCELING', 'CANCELLING', 'PENDING_CANCEL'} else
            'active' if row['status'] in {'LIVE', 'OPEN', 'ACCEPTED', 'PARTIALLY_FILLED'} else 'unknown')
        if state == 'unknown':
            reasons.add('order_status_unknown')
        buys.append({**row, 'session_id': None, 'state': state, 'checked_at': checked.isoformat(),
            'reserved_usd': str(Decimal(row['quantity']) * Decimal(row['price'])),
            'financial_status': 'known' if state != 'unknown' else 'unknown'})
    known = not reasons
    result = dict(version=2, inventory_basis="api_positions", account_id=wallet, pool_account_id=hashlib.sha256(wallet.encode()).hexdigest(),
        checked_at=checked.isoformat(), read_started_at=started.isoformat(), read_ended_at=ended.isoformat(),
        trade_generation=generation, balance_usd=str(balance), allowance_usd=str(allowance),
        financial_status='known' if known else 'unknown', reason_codes=sorted(reasons),
        inventory_cost_usd=str(sum(position_costs.values(), ZERO)) if known else None,
        **report,
        buys=sorted(buys, key=lambda row: row['order_id']),
        open_order_tokens=sorted({row['token_id'] for row in orders.values()
            if row['status'] not in TERMINAL_ORDER_STATES and Decimal(row['quantity']) > ZERO}),
        positions=[dict(token_id=token, quantity=str(positions[token]),
            inventory_cost_usd=str(position_costs[token]) if position_costs[token] is not None else None)
            for token in sorted(positions)])
    result['snapshot_id'] = hashlib.sha256(json.dumps(result, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return result


def ended_reservation_evidence(intent, session, actions, *, account_id, pool_account_id, read_started_at):
    """Return durable ended-send evidence; missing timing never means absent."""
    if intent.get('state') == 'reserved':
        return None
    explicit_account = str(session.get('account_id') or '').strip().casefold()
    if explicit_account:
        if explicit_account != account_id:
            return None
    elif session.get('idempotency_key') != 'lp-auto:' + str(intent.get('intent_id') or ''):
        return None
    session_wallet = str(session.get('wallet_address') or '').strip().casefold()
    if session_wallet and session_wallet != account_id:
        return None
    if pool_account_id != hashlib.sha256(account_id.encode()).hexdigest():
        return None
    entries = [action for action in actions if action.get('role') == 'entry'
               or str(action.get('action_key') or '').endswith('entry-submit')]
    if any(action.get('state') == 'pending' for action in entries):
        return None
    if session.get('submit_stage') in {'preparing', 'sending'}:
        return None
    evidence = [session, *entries]
    finished = []
    for row in evidence:
        raw = row.get('submit_finished_at') or row.get('submit_receipt_at')
        if raw is not None:
            try:
                stamp = _timestamp(raw, name='submit_finished_at')
            except ValueError:
                return None
            if stamp >= read_started_at:
                return None
            for key in ('submit_requested_at', 'submit_post_started_at'):
                if row.get(key) is not None:
                    try:
                        if _timestamp(row[key], name=key) > stamp:
                            return None
                    except ValueError:
                        return None
            finished.append((stamp, 'ended_send'))
    if not finished:
        # Legacy action state is durable receipt evidence; session updated_at
        # alone can be an unrelated monitor write and is never sufficient.
        for action in entries:
            if action.get('state') not in {'accepted', 'rejected', 'unknown'}:
                return None
            if action.get('submit_stage') in {'preparing', 'sending'}:
                return None
            try:
                stamp = _timestamp(action.get('updated_at'), name='action_updated_at')
            except ValueError:
                return None
            if stamp >= read_started_at:
                return None
            finished.append((stamp, 'legacy_finished_action'))
    if not finished:
        return None
    stamp, basis = max(finished)
    return {'request_finished_at': stamp.isoformat(), 'basis': basis}


def account_position_quantity(facts, token: str) -> Decimal:
    positions = facts.get('positions') or ()
    if isinstance(positions, Mapping):
        quantity = positions.get(token, {}).get('quantity', '0')
    else:
        quantity = next((row.get('quantity') for row in positions
                        if row.get('token_id') == token), '0')
    return _number(quantity, 'account_position_unknown')


def can_resume_covered_management(session, actions, facts) -> bool:
    """Resume only management blocked by the original covered entry receipt.

    Account ownership can prove exposure without resolving which request made
    it. Stop decisions, exit uncertainty and unrelated attention remain intact.
    """
    if (not reservation_is_covered(session, facts.get('account_id'))
            or facts.get('financial_status') != 'known'
            or session.get('state') not in {'needs_attention', 'entry_submit_pending'}
            or session.get('submit_status') not in {'unknown', 'accepted_without_order_id'}
            or session.get('resume_state') not in {None, '', 'entry_submit_pending'}):
        return False
    if any(session.get(key) for key in ('stop_requested', 'stop_loss_latched',
            'entry_cancel_requested', 'passive_cancel_requested', 'order_identity_conflict')):
        return False
    original_uncertainty = {None, '', 'trade_change_pending', 'missing_reliable_order_id',
                            'submission_unknown', 'submission_pending', 'order_receipt_unknown'}
    if (session.get('facts_error') not in original_uncertainty
            or session.get('reconciliation') not in original_uncertainty):
        return False
    if any(session.get(key) in {'pending', 'unknown', 'accepted_without_order_id'}
           for key in ('passive_exit_attempt_state', 'protected_exit_attempt_state')):
        return False
    for action in actions:
        if action.get('role') == 'entry' or str(action.get('action_key') or '').endswith('entry-submit'):
            continue
        if (action.get('state') in {'pending', 'unknown', 'accepted_without_order_id'}
                or action.get('state') == 'accepted' and action.get('side') in {'BUY', 'SELL'}
                and not action.get('order_id') and 'cancel' not in str(action.get('action_key') or '')):
            return False
    history = session.get('order_history') or {}
    if any(row.get('side') == 'SELL' and str(row.get('status') or 'UNKNOWN').upper() == 'UNKNOWN'
           for row in history.values()):
        return False
    own_ids = set(history) | set(session.get('owned_order_ids') or ())
    if not own_ids:
        return False
    active_buy = any(buy.get('session_id') == session.get('session_id')
        and buy.get('order_id') in own_ids and buy.get('state') == 'active' for buy in facts.get('buys', ()))
    token = str(session.get('token_id') or '')
    return active_buy or account_position_quantity(facts, token) > ZERO


def account_cancel_is_pending(session, actions, order_id: str) -> bool:
    """Cancellation intent/acknowledgment stays pending until exact terminal proof."""
    if session.get('entry_cancel_requested') and session.get('entry_order_id') == order_id:
        return True
    if any(order_id in {str(value) for value in _items(session.get(key))}
           for key in ('augment_cancel_requested', 'owned_cancel_requested')):
        return True
    return any(action.get('state') in {'pending', 'unknown', 'accepted'}
        and ('cancel' in str(action.get('action_key') or '') or 'cancel' in str(action.get('role') or ''))
        and (str(action.get('order_id') or '') == order_id
             or order_id in {str(value) for value in _items(action.get('targets'))})
        for action in actions)
