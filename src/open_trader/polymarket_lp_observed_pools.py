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
    reason = (
        'observation_missing' if observation is None else
        'orders_not_current_complete' if not observation.orders_complete else
        next((k+'_not_current_complete' for k in ('balance','open_orders','positions')
            if observation.components[k].state != 'known' or not observation.components[k].complete), None)
        or ('trade_version_changed' if observation.read_ticket.trade_version_at_start != snapshot.trade_version else
            'cycle_generation_changed' if observation.cycle_generation != snapshot.current.cycle_generation else
            'observation_stale' if not 0 <= (as_of-observation.accepted_at).total_seconds() <= 10 else None))
    known = reason is None
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
            order_id=oid, side=order['side'], pool=pool,
            price=order.get('price'), remaining=order.get('remaining'),
            last_change_at=role.get('assigned_at')))
    return dict(state='known' if known else 'unknown', reason=reason, account_id=account_id,
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


def positive_yield(sample):
    try:
        value = Decimal(str(sample.get('yield_pct_per_hour')))
        return value.is_finite() and value > 0
    except (InvalidOperation, ValueError, TypeError):
        return False


def select_income_results(results, *, members=()):
    """A fresh measured candidate may change the group; expiry never cancels it."""
    eligible = [cid for cid, value in results.items() if value['eligible'] and positive_yield(value['result'])]
    eligible.sort(key=lambda cid: (-Decimal(str(results[cid]['result']['yield_pct_per_hour'])),
        cid not in members, cid))
    if not members:
        return dict(members=tuple(eligible[:3]), cancel_members=())
    chosen = list(members)
    entrants = [cid for cid in eligible if cid not in members]
    cancelled = ()
    if len(chosen) < 3:
        chosen.extend(entrants[:3-len(chosen)])
    elif entrants and all(cid in results and results[cid]['eligible'] for cid in members):
        worst = min(members, key=lambda cid: (Decimal(str(results[cid]['result']['yield_pct_per_hour'])), cid))
        best = entrants[0]
        if Decimal(str(results[best]['result']['yield_pct_per_hour'])) > Decimal(str(results[worst]['result']['yield_pct_per_hour'])):
            chosen.remove(worst)
            chosen.append(best)
            cancelled = (worst,)
    chosen.sort(key=lambda cid: (-Decimal(str(results[cid]['result']['yield_pct_per_hour'])), cid)
        if cid in results else (Decimal('Infinity'), cid))
    return dict(members=tuple(chosen), cancel_members=cancelled)


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


def d2_measured_result(account_id, condition_id, orders, pool, shares, *,
                       pool_checked_at, share_checked_at, account_checked_at, as_of):
    """Use the catalog's sponsored increment, never add the combined pool twice."""
    unknown = dict(state='unknown', yield_pct_per_hour=None)
    def fresh(stamp, limit):
        return isinstance(stamp, datetime) and stamp.utcoffset() is not None and 0 <= (as_of-stamp).total_seconds() <= limit
    if not fresh(pool_checked_at, 10):
        return dict(unknown, reason='reward_pool_stale')
    if not isinstance(shares, Mapping) or not fresh(share_checked_at, 60):
        return dict(unknown, reason='reward_share_unknown')
    active = [o for o in orders if str(o.get('status', '')).upper() not in
        {'FILLED', 'CANCELED', 'CANCELLED', 'EXPIRED', 'REJECTED'}]
    if len(active) > 1:return dict(unknown, reason='multiple_orders')
    if not active:return dict(unknown, reason='terms_unknown')
    row = active[0]
    if row.get('scoring_status') != 'true' or not fresh(row.get('scoring_checked_at'), 60):
        return dict(unknown, reason='not_scoring')
    if not fresh(account_checked_at, 10):return dict(unknown, reason='terms_unknown')
    try:
        native = _number(pool['native_daily_pool_usd'])
        sponsored = _number(pool['sponsored_daily_pool_usd'])
        hourly = Decimal(0)
        for source, amount in (('native', native), ('sponsored', sponsored)):
            if not amount:continue
            try:percentage = _number(shares[source])
            except (KeyError, ValueError, InvalidOperation):return dict(unknown, reason='reward_share_unknown')
            if percentage > 100:return dict(unknown, reason='reward_share_unknown')
            hourly += amount * percentage / 2400
        measured_at = min(pool_checked_at, share_checked_at, account_checked_at, row['scoring_checked_at'])
        result = measured_result(account_id, condition_id, active, hourly, reward_known=True,
            measured_at=measured_at, reward_checked_at=share_checked_at,
            account_checked_at=account_checked_at)
        return result if result['state']=='known' else dict(unknown, reason='terms_unknown')
    except (KeyError, ValueError, TypeError, InvalidOperation):
        return dict(unknown, reason='terms_unknown')



def dashboard_affiliation(order, role, actions, *, snapshot, settings_account):
    """Project an existing exact accepted BUY; grant no financial authority."""
    import hashlib

    def account_key(value):
        return value.strip().casefold() if isinstance(value, str) else None

    identity = {key: order.get(key) for key in
        ('account_id', 'condition_id', 'token_id', 'order_id')}
    identity['account_id'] = account_key(identity['account_id'])
    def invalid(reason):
        return dict(state='error', reason=reason, **identity)

    account = account_key(snapshot.get('account_id') or snapshot.get('wallet_address'))
    wallet = snapshot.get('wallet_address')
    if (snapshot.get('authenticated') is not True or not isinstance(account, str) or not account
            or any(not isinstance(value, str) or not value for value in identity.values())
            or identity['account_id'] != account
            or wallet and account_key(wallet) != account
            or hashlib.sha256(account.encode()).hexdigest() != settings_account):
        return invalid('account_identity_mismatch')
    if order.get('side') != 'BUY':
        return invalid('buy_identity_required')
    if role.get('pool') not in ('trial', 'income') or role.get('episode') != identity['order_id']:
        return invalid('exact_pool_role_missing')
    request_id = role.get('request_id')
    matches = [a for a in actions if a.get('request_id') == request_id and request_id]
    if len(matches) != 1:
        return invalid('accepted_buy_request_missing')
    request = matches[0]
    receipt = request.get('receipt') or {}
    keys = ('condition_id', 'token_id')
    if (request.get('d2_record') != 'request' or request.get('kind') != 'BUY'
            or account_key(request.get('account_id')) != identity['account_id']
            or account_key(receipt.get('account_id')) != identity['account_id']
            or any(request.get(key) != identity[key] for key in keys)
            or request.get('order_id') not in (None, identity['order_id'])
            or receipt.get('outcome') != 'accepted'
            or receipt.get('order_id') != identity['order_id']
            or receipt.get('request_id') != request_id
            or any(receipt.get(key) != request.get(key) for key in
                (*keys, 'process_epoch', 'session_id'))):
        return invalid('accepted_buy_identity_mismatch')
    return dict(state='known', pool=role['pool'], **identity, request_id=request_id)
