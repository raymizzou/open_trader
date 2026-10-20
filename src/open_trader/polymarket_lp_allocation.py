"""C1 counterfactual allocation. Values only: no order or persistence authority."""
from decimal import Decimal, ROUND_CEILING, ROUND_DOWN

from .polymarket_lp_current_inputs import _current_account
from .polymarket_lp_risk import _levels, _select_bid_level, evaluate_lp_entry, minimum_order_estimate

ZERO = Decimal(0)
QUANTUM = Decimal('.01')
RANK_LIMIT = 20


def shadow_result(state, *, target_count=0, checked_at=None, **values):
    """Keep the diagnostic contract identical on success and every failure boundary."""
    return dict(dict(state=state, error=None, reason_codes=(), checked_at=checked_at,
        target_envelope_usd=None, available_cash_usd=None, leftover_cash_usd=None,
        would_send_count=0, retained_count=0, not_sent_count=0, missing_rank_count=target_count,
        rank_limit=RANK_LIMIT, omitted_ranks=0, ranks=(), excluded_count=0, excluded=(),
        candidates_unknown_count=0), **values)


def rank_weights_shadow(frame, policy, calculation, as_of, *, authority=True, blocked_conditions=()):
    """Use one captured frame; retain ranks when funds or final size block them."""
    weights = tuple(Decimal(v) for v in policy.allocation_weights)
    if len(weights) != policy.target_count:
        raise ValueError('allocation_weights_length_invalid')
    source = frame.sources.get('allocation_candidates', frame.sources.get('candidates'))
    if source is not None and source.error == 'shadow_capture_exception':
        return shadow_result('error', target_count=policy.target_count, checked_at=as_of,
            error=source.diagnostics['exception_type'], reason_codes=('shadow_exception',))
    account = _current_account(frame, as_of)
    reserved, inventory = frame.buy_reserved, frame.inventory_cost
    unknown = bool(not authority or not account.complete or policy.budget is None
        or reserved is None or inventory is None or source is None or source.value is None
        or not source.complete or source.error)
    if unknown:
        return shadow_result('unknown', target_count=policy.target_count, reason_codes=('current_facts_unknown',), ranks=(),
            leftover_cash_usd=None, would_send_count=0, not_sent_count=policy.target_count,
            omitted_ranks=0, rank_limit=RANK_LIMIT, checked_at=as_of)
    wallet = min(account.value['balance'], account.value['allowance'])
    total = max(ZERO, min(policy.budget, wallet))
    free = max(ZERO, min(policy.budget-reserved, wallet-reserved))
    cash = free
    occupied = {row['condition_id'] for row in frame.current_buys}
    ranked, excluded = [], []
    candidates_unknown_count = 0
    for row in frame.current_buys:
        rate = calculation.yields.get(row['order_id'])
        if rate is None or row['status'] in ('CANCELING', 'CANCELLING', 'PENDING_CANCEL'):
            unknown = True
        ranked.append((row, rate, True))
    native_source = frame.sources.get('candidates')
    native_rows = ((native_source.value or ())
        if native_source and native_source.complete and not native_source.error else ())
    native_by_token = {}
    for row in native_rows:
        token = row.get('token_id')
        native_by_token[token] = row if token not in native_by_token else None
    for row in source.value or ():
        if row.get('existing') or row['condition_id'] in occupied:
            continue
        direction = row['direction']
        evaluated = row.get('eligibility')
        if evaluated is None:
            evaluated = evaluate_lp_entry(direction, account=account.value, now=as_of,
                candidate=True, bid_level=policy.buy_price_level)
        codes = tuple(evaluated.get('reason_codes', ()))
        if evaluated['state'] == 'unknown':
            candidates_unknown_count += 1
            excluded.append(dict(condition_id=row['condition_id'], token_id=row['token_id'],
                reason_codes=('candidate_unknown', *codes)))
            continue
        if evaluated['state'] != 'eligible' and codes != ('balance_insufficient',):
            excluded.append(dict(condition_id=row['condition_id'], token_id=row['token_id'], reason_codes=codes))
            continue
        price, _ = _select_bid_level(_levels(direction['book']['bids'], 'bids'), policy.buy_price_level)
        minimum = max(Decimal(str(direction['market'][key])) for key in ('minimum_order_size', 'reward_min_size'))
        quantity = (minimum/QUANTUM).to_integral_value(rounding=ROUND_CEILING)*QUANTUM
        native = native_by_token.get(row['token_id'])
        if (native is not None and not native.get('existing')
                and native.get('condition_id') == row['condition_id']
                and native.get('direction') == direction
                and native.get('price') == price and native.get('quantity') == quantity
                and native.get('evaluation_at', as_of) == as_of
                and row['token_id'] in calculation.candidate_yields):
            rate = calculation.candidate_yields[row['token_id']]
        else:
            estimate = minimum_order_estimate(direction, dict(price=price, quantity=quantity), as_of)
            rate = estimate.get('yield_pct_per_hour') if estimate.get('state') == 'known' else None
        if rate is None:
            candidates_unknown_count += 1
            excluded.append(dict(condition_id=row['condition_id'], token_id=row['token_id'],
                reason_codes=('candidate_unknown',)))
            continue
        ranked.append(({**row, 'price': price, 'quantity': quantity}, rate, False))
    if unknown:
        return shadow_result('unknown', target_count=policy.target_count, reason_codes=('rank_unknown',), ranks=(),
            target_envelope_usd=total, available_cash_usd=free, leftover_cash_usd=free,
            would_send_count=0, not_sent_count=policy.target_count, omitted_ranks=0,
            rank_limit=RANK_LIMIT, checked_at=as_of, excluded=excluded[:RANK_LIMIT],
            excluded_count=len(excluded), candidates_unknown_count=candidates_unknown_count)
    ranked.sort(key=lambda item: (-item[1], not item[2], item[0].get('order_id','') if item[2] else '',
        item[0]['condition_id'], item[0]['token_id']))
    unique, seen = [], set()
    for item in ranked:
        if item[0]['condition_id'] not in seen:
            unique.append(item)
            seen.add(item[0]['condition_id'])
    ranks, rates, sent, retained, blocked = [], [], 0, 0, None
    slots = max(0, policy.target_count-len(frame.current_buys))
    for index, (row, minimum_rate, existing) in enumerate(unique[:policy.target_count]):
        target = total*weights[index]
        price = row['price']
        quantity = row['remaining'] if existing else (target/price/QUANTUM).to_integral_value(rounding=ROUND_DOWN)*QUANTUM
        cost = price*quantity
        detail = dict(rank=index+1, condition_id=row['condition_id'], token_id=row['token_id'],
            market_id=row.get('market_id'), order_id=row.get('order_id'), price_level=policy.buy_price_level,
            price=price, target_usd=target, quantity=quantity, actual_usd=cost,
            estimated_yield_pct_per_hour=None, status='NOT_SENT', reason_codes=())
        if not existing:
            evaluated = evaluate_lp_entry(row['direction'], account=account.value, now=as_of,
                candidate=True, bid_level=policy.buy_price_level, quantity=quantity)
            detail.update(risk_state=evaluated['state'], risk_reason_codes=tuple(evaluated.get('reason_codes', ())))
            estimate = (minimum_order_estimate(row['direction'], dict(price=price, quantity=quantity), as_of)
                if evaluated['state'] == 'eligible' else {})
            rate = estimate.get('yield_pct_per_hour') if estimate.get('state') == 'known' else None
            detail['estimated_yield_pct_per_hour'] = rate
        if existing:
            detail.update(status='RETAINED', estimated_yield_pct_per_hour=minimum_rate)
            retained += 1
            rates.append(minimum_rate)
        elif blocked:
            detail['reason_codes'] = ('prefix_blocked',)
        else:
            if quantity < row['quantity']:
                blocked = 'below_reward_minimum'
            elif cost > cash:
                blocked = 'insufficient_funds'
            elif slots <= 0:
                blocked = 'slots_unavailable'
            elif row['condition_id'] in blocked_conditions:
                blocked = 'opening_blocked'
            elif evaluated['state'] != 'eligible':
                blocked = 'unknown' if evaluated['state'] == 'unknown' else 'risk_rejected'
            if blocked:
                detail['reason_codes'] = (blocked, *evaluated.get('reason_codes', ()))
            else:
                if rate is None:
                    blocked = 'unknown'
                    detail['reason_codes'] = ('unknown',)
                else:
                    detail.update(status='WOULD_SEND', estimated_yield_pct_per_hour=rate)
                    rates.append(rate)
                    sent += 1
                    slots -= 1
                    cash -= cost
        ranks.append(detail)
    # Final-sized yields must justify the original rank; never re-sort or reassign weights.
    unselected = [rate for _, rate, _ in unique[policy.target_count:]]
    rank_proven = (all(a >= b for a,b in zip(rates,rates[1:]))
        and (not rates or not unselected or rates[-1] >= max(unselected)))
    if not rank_proven:
        for detail in ranks:
            if detail['status'] == 'WOULD_SEND':
                detail.update(status='NOT_SENT', reason_codes=('allocation_rank_not_proven',))
        cash, sent, blocked = free, 0, 'allocation_rank_not_proven'
    return shadow_result('known', target_count=policy.target_count, reason_codes=(blocked,) if blocked else (),
        target_envelope_usd=total, available_cash_usd=free, leftover_cash_usd=cash,
        ranks=ranks[:RANK_LIMIT], rank_limit=RANK_LIMIT, omitted_ranks=max(0,len(ranks)-RANK_LIMIT),
        would_send_count=sent, retained_count=retained, not_sent_count=len(ranks)-sent-retained,
        missing_rank_count=max(0,policy.target_count-len(ranks)),
        excluded=excluded[:RANK_LIMIT], excluded_count=len(excluded), checked_at=as_of,
        candidates_unknown_count=candidates_unknown_count)
