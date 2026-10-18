"""Value-only calculations; results carry no execution or publication authority."""
from dataclasses import dataclass, field, replace
from decimal import Decimal
from .polymarket_lp_market_reader import decimal, freeze
from .polymarket_lp_risk import minimum_order_estimate


@dataclass(frozen=True)
class CalculationPolicy:
    budget: Decimal | None
    target_count: int = 0
    selection_wallet: Decimal | None = None
    selection_capacity: Decimal | None = None
    buy_price_level: int = 1
    config_version: int = 0
    trading_config_version: int = 0
    api_retry_interval_seconds: int = 60
    order_check_interval_seconds: int = 10
    round_interval_seconds: int = 30


@dataclass(frozen=True)
class CalculationResult:
    buy_reserved: Decimal | None
    inventory_cost: Decimal | None
    new_capacity: Decimal | None
    yields: object = field(default_factory=lambda: freeze({}))
    targets: tuple[str, ...] = ()
    target_tokens: tuple[str, ...] = ()
    candidate_yields: object = field(default_factory=lambda: freeze({}))
    policy_version: int = 0
    selection_state: str = 'NO_DECISION'
    selection_reasons: tuple[str, ...] = ()
    victim_order_ids: tuple[str, ...] = ()
    decision_states: object = field(default_factory=lambda: freeze({}))
    retained_order_ids: tuple[str, ...] = ()
    new_target_keys: tuple[tuple[str, str], ...] = ()


def calculate_current(frame, policy, as_of) -> CalculationResult:
    balance = frame.sources.get('balance')
    funds = tuple(map(decimal, balance.value)) if balance and balance.complete and not balance.error and balance.value else (None, None)
    reserved, inventory = frame.buy_reserved, frame.inventory_cost
    available = None
    account = frame.sources.get('current_account')
    if (policy.budget is not None and all(v is not None for v in (*funds, reserved, inventory))
            and (account is None or account.complete and not account.error)):
        available = max(Decimal(0), min(policy.budget-inventory-reserved, min(funds)-reserved))
    directions = frame.sources.get('directions')
    estimates = {}
    for row in frame.current_buys:
        estimate = None
        direction = directions.value.get(row['order_id']) if directions and directions.complete else None
        if direction:
            try:
                estimate = minimum_order_estimate(direction, {'price': row['price'],
                    'quantity': row['remaining']}, as_of, resting_quantity=row['remaining'])
            except (ValueError, TypeError, KeyError):
                pass
        estimates[row['order_id']] = (estimate.get('yield_pct_per_hour')
            if estimate and estimate.get('state') == 'known' else None)
    candidates = frame.sources.get('candidates')
    candidate_yields = {}
    if candidates and candidates.complete and not candidates.error:
        for row in candidates.value or ():
            if row.get('existing'):
                continue  # Only the exact current order supplies incumbent terms/yield.
            try:
                quantity, price = decimal(row['quantity']), decimal(row['price'])
                estimate = minimum_order_estimate(row['direction'], {'price': price, 'quantity': quantity},
                    row.get('evaluation_at', as_of))
                rate = estimate.get('yield_pct_per_hour')
                candidate_yields[row['token_id']] = rate if estimate.get('state') == 'known' else None
            except (KeyError, TypeError, ValueError):
                continue
    result = CalculationResult(reserved, inventory, available, freeze(estimates),
        candidate_yields=freeze(candidate_yields), policy_version=policy.config_version)
    return select_current_composition(frame, policy, result)


def select_current_composition(frame, policy, calculation, *, orders_accepted=True,
                               positions_accepted=True, financial_authority=True):
    """Re-gate values against accepted authority without recalculating yields."""
    reserved, inventory = calculation.buy_reserved, calculation.inventory_cost
    estimates = calculation.yields
    balance = frame.sources.get('balance')
    funds = tuple(map(decimal, balance.value)) if balance and balance.complete and not balance.error and balance.value else (None, None)
    account = frame.sources.get('current_account')
    candidates = frame.sources.get('candidates')
    reasons = []
    if any(v is None for v in funds): reasons.append('balance_unknown')
    if policy.budget is None: reasons.append('budget_unknown')
    if not frame.orders_complete or reserved is None: reasons.append('orders_unknown')
    if not frame.positions_complete or inventory is None: reasons.append('positions_unknown')
    if account is not None and (not account.complete or account.error): reasons.append('financial_authority_unknown')
    if candidates is None or not candidates.complete or candidates.error: reasons.append('candidates_incomplete')
    # Observational comparison callers supply already bounded capacities and
    # no account frame. These value-only overrides confer no send authority.
    comparison_capacity = (policy.selection_wallet is not None and policy.selection_capacity is not None
        and not frame.current_buys and candidates is not None and candidates.complete and not candidates.error)
    if not orders_accepted: reasons.append('orders_not_accepted')
    if not positions_accepted or not financial_authority: reasons.append('financial_authority_unknown')
    replacement_valid = (not reasons or comparison_capacity) and orders_accepted and positions_accepted and financial_authority
    count_reduction = (frame.orders_complete and orders_accepted and reserved is not None
        and len(frame.current_buys) > policy.target_count)
    budget_reduction = (frame.orders_complete and orders_accepted and frame.positions_complete and positions_accepted
        and reserved is not None and inventory is not None and policy.budget is not None
        and reserved + inventory > policy.budget)
    selection_valid = replacement_valid or count_reduction or budget_reduction
    ranked = []
    if replacement_valid:
        for row in candidates.value or ():
            if row.get('existing'): continue
            rate = calculation.candidate_yields.get(row.get('token_id'))
            if rate is not None:
                ranked.append((row, rate, decimal(row['quantity']) * decimal(row['price'])))
    for row in frame.current_buys:
        rate = estimates[row['order_id']]
        if rate is not None:
            ranked.append(({**row, 'existing': True}, rate, row['remaining'] * row['price']))
    ranked.sort(key=lambda x: (-x[1], not x[0].get('existing', False),
        x[0].get('order_id', '') if x[0].get('existing') else '',
        x[0]['condition_id'], x[0]['token_id']))
    remaining = min(funds) if all(v is not None for v in funds) else None
    capacity = (max(Decimal(0), policy.budget - inventory)
        if policy.budget is not None and inventory is not None else None)
    if policy.selection_wallet is not None:
        remaining = policy.selection_wallet
    if policy.selection_capacity is not None:
        capacity = policy.selection_capacity
    if not replacement_valid:
        remaining = reserved
        if not budget_reduction:
            capacity = reserved
    rejected = set()
    protected = [({**row, 'existing': True}, None, row['remaining'] * row['price'])
        for row in frame.current_buys if estimates[row['order_id']] is None]
    protected_cost = sum((item[2] for item in protected), Decimal(0))
    selected = protected[:]
    while selection_valid and remaining is not None and capacity is not None:
        selected = protected[:]
        markets = {row['condition_id'] for row, _, _ in protected}
        used = protected_cost
        for item in ranked:
            row, rate, cost = item
            identity = (row['condition_id'], row['token_id'])
            if (not row.get('existing') and identity in rejected
                    or row['condition_id'] in markets):
                continue
            if len(selected) >= policy.target_count:
                break
            if used + cost <= capacity:
                selected.append(item)
                markets.add(row['condition_id'])
                used += cost
        retained = sum((cost for row, _, cost in selected if row.get('existing')), Decimal(0))
        new = [item for item in selected if not item[0].get('existing')]
        if sum((item[2] for item in new), Decimal(0)) <= max(Decimal(0), remaining - retained):
            break
        # Remove the lowest ranked new choice permanently for this decision,
        # then let the ranked incumbent alternatives compete again.
        row = new[-1][0]
        rejected.add((row['condition_id'], row['token_id']))
    targets = [row['condition_id'] for row, _, _ in selected]
    tokens = [row['order_id'] if row.get('existing') else row['token_id'] for row, _, _ in selected]
    retained_ids = tuple(row['order_id'] for row, _, _ in selected if row.get('existing'))
    new_keys = tuple((row['condition_id'], row['token_id']) for row, _, _ in selected if not row.get('existing'))
    victims = tuple(row['order_id'] for row in frame.current_buys
        if selection_valid and estimates[row['order_id']] is not None and row['order_id'] not in retained_ids)
    return replace(calculation, targets=tuple(targets), target_tokens=tuple(tokens),
        selection_state='DECISION' if selection_valid else 'NO_DECISION',
        selection_reasons=tuple(dict.fromkeys(reasons)), victim_order_ids=victims,
        retained_order_ids=retained_ids, new_target_keys=new_keys,
        decision_states=freeze(dict(replacement='DECISION' if replacement_valid else 'NO_DECISION',
            over_target='DECISION' if count_reduction else 'NO_DECISION',
            over_budget='DECISION' if budget_reduction else 'NO_DECISION')))
