"""Measured pool values. Pure calculations never acquire or send trading facts."""
from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal, InvalidOperation


def income_order(roles, condition_id, order_id):
    """Affiliation is exact-order evidence, never a market/token-wide waiver."""
    role = roles.get(condition_id, {})
    return role.get('pool') == 'income' and order_id == role.get('episode')


def promotion_eligible(role, sample):
    if role.get('evicted'):
        return False
    source = sample['source_order']['order_id']
    if role.get('pool') == 'trial' and role.get('episode') != source:
        return False
    return role.get('consumed_result') != dict(measured_at=sample['measured_at'], order_id=source)


def result_projection(observations, account_id, as_of):
    """Age detached results using their original measurement, never read time."""
    projected = {}
    for condition, observation in observations.items():
        sample = observation.get('pool_result')
        if not isinstance(sample, Mapping) or sample.get('state') != 'known':
            continue
        try:
            measured = sample['measured_at']
            if isinstance(measured, str):
                measured = datetime.fromisoformat(measured.replace('Z', '+00:00'))
            age = Decimal(str((as_of - measured).total_seconds()))
            eligible = (sample.get('account_id') == account_id and sample.get('condition_id') == condition
                and Decimal(0) <= age <= Decimal(120))
        except (ValueError, TypeError, KeyError):
            eligible, age = False, None
        projected[condition] = dict(result=dict(sample), age_seconds=age, eligible=eligible)
    return projected


def select_income_results(results, *, members=()):
    """A fresh measured candidate may change the group; expiry never cancels it."""
    eligible = [cid for cid, value in results.items() if value['eligible']]
    eligible.sort(key=lambda cid: (-Decimal(str(results[cid]['result']['yield_pct_per_hour'])),
        cid not in members, cid))
    chosen = tuple(eligible[:3])
    new = set(chosen) - set(members)
    if members and not new:
        chosen = tuple(members)
    return dict(members=chosen, cancel_members=tuple(members) if new else ())


def _number(value):
    if value is None or isinstance(value, bool):
        raise ValueError('number_unknown')
    result = Decimal(str(value))
    if not result.is_finite() or result < 0:
        raise ValueError('number_unknown')
    return result


def measured_result(account_id, condition_id, orders, rate, *, reward_known,
                    measured_at, reward_checked_at, account_checked_at):
    """Attribute official market rewards only to one exact scoring BUY.

    Inventory is not a maker reward contributor. Multiple live orders (including
    SELLs) cannot be separated using a market-level reward rate.
    """
    unknown = dict(state='unknown', yield_pct_per_hour=None)
    try:
        active = [o for o in orders if str(o.get('status', '')).upper() not in
            {'FILLED', 'CANCELED', 'CANCELLED', 'EXPIRED', 'REJECTED'}]
        if not reward_known or len(active) != 1:
            return {**unknown, 'reason': 'reward_attribution_unknown'}
        row = active[0]
        if (row.get('side') != 'BUY' or row.get('condition_id') != condition_id
                or not row.get('order_id') or not row.get('token_id')
                or row.get('scoring_status') != 'true'):
            return {**unknown, 'reason': 'measured_order_unknown'}
        price = _number(row.get('price'))
        remaining = _number(row.get('remaining_quantity'))
        original = _number(row.get('quantity'))
        filled = _number(row.get('filled_quantity'))
        hourly = _number(rate)
        if not 0 < price <= 1 or remaining <= 0 or original != remaining + filled:
            return {**unknown, 'reason': 'measured_terms_unknown'}
        capital = price * remaining
        return dict(state='known', account_id=account_id, condition_id=condition_id,
            basis='official_hourly_reward_per_measured_buy',
            yield_pct_per_hour=hourly / capital * 100, hourly_reward_usd=hourly,
            buy_capital_usd=capital, measured_at=measured_at,
            reward_checked_at=reward_checked_at, account_checked_at=account_checked_at,
            source_order=dict(order_id=row['order_id'], token_id=row['token_id'],
                side='BUY', price=price, remaining_quantity=remaining,
                original_quantity=original, filled_quantity=filled,
                scoring_checked_at=row.get('scoring_checked_at')))
    except (InvalidOperation, ValueError, TypeError):
        return {**unknown, 'reason': 'measured_terms_unknown'}
