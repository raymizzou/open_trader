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


def current_result_eligible(roles, sample, orders):
    """Only an exact managed current episode can admit a measured source."""
    cid = sample['condition_id']
    role = roles.get(cid, {})
    source = sample['source_order']
    trials = [r for r in orders if r['side'] == 'BUY'
        and roles.get(r['condition_id'], {}).get('pool') == 'trial'
        and roles[r['condition_id']].get('episode') == r['order_id']
        and not roles[r['condition_id']].get('evicted')]
    if role.get('pool') == 'trial' and len(trials) > 3:
        return False
    return (role.get('pool') in ('trial', 'income')
        and role.get('episode') == source['order_id']
        and promotion_eligible(role, sample)
        and any(r['side'] == 'BUY' and r['condition_id'] == cid
            and r['order_id'] == source['order_id']
            and r['token_id'] == source['token_id'] for r in orders))


def current_membership(snapshot, roles, transfers, *, account_id, settings_account, epoch, as_of):
    """A read-only view of one accepted snapshot; history never creates rows."""
    observation = snapshot.observation
    known = (observation is not None and observation.orders_complete
        and all(observation.components[k].state == 'known'
            and observation.components[k].complete for k in ('balance','open_orders','positions'))
        and observation.read_ticket.trade_version_at_start == snapshot.trade_version
        and observation.cycle_generation == snapshot.current.cycle_generation
        and 0 <= (as_of - observation.accepted_at).total_seconds() <= 10)
    promotions = []
    for cid, transfer in transfers.items():
        if transfer.get('accepted_order_id'):
            continue  # accepted income is current membership, never pending promotion
        sample = transfer['sample']
        fresh = result_projection({cid:dict(pool_result=sample)}, settings_account, as_of)
        if (transfer['epoch'] == epoch
                and transfer['generation'] == snapshot.current.cycle_generation
                and fresh.get(cid, {}).get('eligible')):
            promotions.append(dict(account_id=account_id, condition_id=cid,
                **transfer.get('source_order',sample['source_order'])))
    orders = []
    for order in observation.source_frame.orders if observation else ():
        if str(order.get('status', '')).upper() in ('FILLED','CANCELED','CANCELLED','EXPIRED','REJECTED'):
            continue
        cid, token, oid = order['condition_id'], order['token_id'], order['order_id']
        role = roles.get(cid, {})
        pool = 'unclassified'
        if role.get('episode') == oid and role.get('pool') in ('trial', 'income'):
            pool = role['pool']
        if any(p['condition_id'] == cid and p['token_id'] == token
                and p['order_id'] == oid for p in promotions):
            pool = 'promotion'
        orders.append(dict(account_id=account_id, condition_id=cid, token_id=token,
            order_id=oid, side=order['side'], pool=pool))
    return dict(state='known' if known else 'unknown', account_id=account_id,
        observation_id=observation.observation_id if observation else None,
        observed_at=observation.accepted_at if observation else None,
        orders=orders, pending_promotions=promotions)


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
