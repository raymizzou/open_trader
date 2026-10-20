"""Adapt one captured frame for calculation without acquiring new facts."""
from collections.abc import Mapping
from dataclasses import dataclass, replace
from types import MappingProxyType
from datetime import datetime

from .polymarket_lp_market_reader import SourceResult, decimal, freeze, observed_source_error


@dataclass(frozen=True)
class QualifiedPoolCapture:
    capture_id: str
    captured_at: datetime
    inventory_complete: bool
    generation: int | None
    exclusion_revision: int
    rows: Mapping
    error: str | None = None


def _current_account(frame, as_of):
    """Join only independently valid components from this captured raw frame."""
    from .polymarket_lp_risk import _freshness
    required = ('balance', 'open_orders', 'positions')
    stamps = []
    try:
        for name in required:
            source = frame.sources.get(name)
            if source is None or not source.complete or source.error or source.value is None:
                raise ValueError('current_account_' + name + '_unknown')
            _freshness(source.checked_at, as_of, name, max_age=60)
            stamps.append(source.checked_at)
        funds = tuple(decimal(v) for v in frame.sources['balance'].value)
        if (len(funds) != 2 or any(v is None or v < 0 for v in funds)
                or not frame.orders_complete or not frame.positions_complete
                or frame.buy_reserved is None or frame.inventory_cost is None):
            raise ValueError('current_account_facts_unknown')
    except (ValueError, TypeError):
        return SourceResult('', complete=False, error='current_account_facts_unknown')
    return SourceResult('', freeze(dict(authenticated=True, wallet_address=frame.account,
        balance=funds[0], allowance=funds[1], open_orders=frame.orders, positions=frame.positions,
        open_orders_complete=True, positions_complete=True, checked_at=min(stamps))), checked_at=min(stamps))


def _prepare_qualified(frame, capture, policy, as_of):
    from .polymarket_lp_risk import evaluate_lp_entry
    from .polymarket_lp_views import lp_history_eligibility, _next_review_at
    from .polymarket_lp import expiration_for_review
    account = _current_account(frame, as_of)
    # Qualification considers a possible final composition. The calculator
    # reserves its retained orders, and the sender separately requires actual
    # free funds. Keep all order facts for participation/depth gates here.
    selection_account = ({**account.value,
        'balance': account.value['balance'] + frame.buy_reserved,
        'allowance': account.value['allowance'] + frame.buy_reserved}
        if account.complete else None)
    rows, sources, eligibility = [], {}, {}
    for cid, source in capture.rows.items():
        if not source.complete or source.error:
            eligibility[cid] = dict(state='unknown', reason_codes=(source.error or 'qualification_unknown',))
            continue
        facts = source.value.get('facts')
        directions = facts.get('directions') if isinstance(facts, Mapping) else None
        if not isinstance(directions, (tuple, list)) or not directions:
            eligibility[cid] = dict(state='unknown', reason_codes=('qualification_missing',))
            continue
        for direction in directions:
            market = direction.get('market') if isinstance(direction, Mapping) else None
            token = market.get('token_id') if isinstance(market, Mapping) else None
            key = f'{cid}:{token}'
            sources[key] = source
            reason = None
            if not token or market.get('condition_id') != cid:
                reason = 'qualification_identity_mismatch'
            elif not account.complete:
                reason = account.error
            else:
                reason = lp_history_eligibility(direction.get('history_summary'), now=as_of,
                    condition_id=cid, token_id=token)
            evaluated = (dict(state='unknown', reason_codes=(reason,), guidance=None) if reason else
                evaluate_lp_entry(direction, account=selection_account, now=as_of, reservations=(),
                    candidate=True, bid_level=policy.buy_price_level))
            eligibility[key] = evaluated
            guide = evaluated.get('guidance')
            if evaluated.get('state') == 'eligible' and isinstance(guide, Mapping):
                review = _next_review_at(as_of)
                try:
                    expiration = expiration_for_review(review, now=as_of)
                except ValueError as exc:
                    eligibility[key] = dict(state='rejected', reason_codes=(str(exc),), guidance=None)
                    continue
                rows.append(dict(account_id=frame.account, venue='polymarket',
                    condition_id=cid, token_id=token, market_id=market.get('market_id'),
                    outcome=market.get('outcome'), price=guide['price'], quantity=guide['quantity'],
                    direction=direction, evaluation_at=as_of, policy_version=policy.config_version,
                    review_at=review, order_type='GTD', expiration=expiration, post_only=True,
                    send_options=direction.get('send_options', source.value['row'].get('send_options', ()))))
    return dict(current_account=account,
        candidate_inventory=SourceResult(capture.capture_id, tuple(capture.rows),
            complete=capture.inventory_complete, checked_at=capture.captured_at, error=capture.error),
        candidate_sources=SourceResult('', MappingProxyType(sources)),
        candidate_eligibility=SourceResult('', freeze(eligibility)),
        candidates=SourceResult(capture.capture_id, freeze(rows), complete=capture.inventory_complete,
            error=capture.error))


def prepare_current_inputs(frame, captured_candidates, policy, as_of):
    """Return immutable calculation inputs, preserving every captured source.

    Candidates are an already captured SourceResult, or None for UNKNOWN.
    Policy and evaluation time carry no source refresh or acquisition authority.
    Directions are keyed by exact current order ID; their source provenance stays
    in the original market SourceResults alongside the derived values.
    """
    directions, failures = {}, {}
    for order in frame.current_buys:
        condition, token = order['condition_id'], order['token_id']
        source = frame.sources.get(f'market:{condition}:{token}')
        def dropped(code):
            failures[order['order_id']] = dict(order_id=order['order_id'],
                account_id=frame.account, condition_id=condition, token_id=token,
                reason_codes=(code,), source_id=source.source_id if source else None,
                source_error=observed_source_error(source))
        if not source or not source.complete or source.error or not isinstance(source.value, Mapping):
            dropped('market_source_missing' if not source else 'market_source_failed' if source.error
                else 'market_source_incomplete' if not source.complete else 'market_payload_invalid')
            continue
        value = source.value
        metadata, catalog, book = (value.get(k) for k in ('metadata', 'rewards', 'book'))
        if not all(isinstance(v, Mapping) for v in (metadata, catalog, book)):
            dropped(next(code for value, code in ((metadata, 'market_metadata_missing'),
                (catalog, 'market_rewards_missing'), (book, 'market_book_missing'))
                if not isinstance(value, Mapping)))
            continue
        if (metadata.get('condition_id') != condition
                or book.get('condition_id', book.get('market')) != condition
                or book.get('token_id', book.get('asset_id')) != token
                or catalog.get('complete') is not True):
            dropped('metadata_condition_mismatch' if metadata.get('condition_id') != condition
                else 'book_condition_mismatch' if book.get('condition_id', book.get('market')) != condition
                else 'book_token_mismatch' if book.get('token_id', book.get('asset_id')) != token
                else 'reward_catalog_incomplete')
            continue
        rows = [r for r in catalog.get('markets', ())
                if isinstance(r, Mapping) and r.get('condition_id') == condition]
        if len(rows) != 1:
            dropped('reward_row_missing' if not rows else 'reward_rows_multiple')
            continue
        reward = rows[0]
        minimum = metadata.get('reward_min_size')
        if minimum is None:
            minimum = reward.get('rewards_min_size')
        spread = metadata.get('reward_max_spread')
        if spread is None:
            raw = decimal(reward.get('rewards_max_spread'))
            spread = None if raw is None else raw / 100
        directions[order['order_id']] = dict(
            market={**metadata, 'condition_id': condition, 'token_id': token,
                    'reward_min_size': decimal(minimum), 'reward_max_spread': decimal(spread)},
            book=book, daily_pool_usd=reward.get('daily_pool_usd'),
            reward_checked_at=catalog.get('checked_at'),
        )
    if type(captured_candidates) is QualifiedPoolCapture:
        values = _prepare_qualified(frame, captured_candidates, policy, as_of)
        return replace(frame, sources=MappingProxyType({**frame.sources, **values,
            'directions': SourceResult('', freeze(directions)),
            'direction_failures': SourceResult('', freeze(failures))}))
    if captured_candidates is None:
        candidates = SourceResult('', complete=False)
    elif type(captured_candidates) is SourceResult:
        candidates = replace(captured_candidates, value=freeze(captured_candidates.value),
                             error_facts=freeze(captured_candidates.error_facts))
    else:
        raise TypeError('captured_candidates_requires_source_result')
    return replace(frame, sources=MappingProxyType({**frame.sources,
        'directions': SourceResult('', freeze(directions)), 'candidates': candidates,
        'direction_failures': SourceResult('', freeze(failures))}))


def manual_snapshot(frame, request, as_of):
    """Project explicit manual validation from this frame, with no source acquisition."""
    from .polymarket_lp_risk import _freshness
    account = _current_account(frame, as_of)
    if not account.complete:
        raise ValueError('account_facts_unknown')
    cid, token = request['condition_id'], request['token_id']
    source = frame.sources.get(f'market:{cid}:{token}')
    if source is None or not source.complete or source.error or not isinstance(source.value, Mapping):
        raise ValueError('market_unknown')
    metadata, catalog, book = (source.value.get(k) for k in ('metadata', 'rewards', 'book'))
    if not all(isinstance(v, Mapping) for v in (metadata, catalog, book)) or catalog.get('complete') is not True:
        raise ValueError('market_unknown')
    selected = metadata.get('outcomes', {}).get(request['outcome'].lower())
    if (not isinstance(selected, Mapping) or selected.get('token_id') != token
            or str(selected.get('label', '')).upper() != request['outcome']
            or metadata.get('condition_id') != cid or metadata.get('market_id') != request['market_id']
            or book.get('condition_id', book.get('market')) != cid
            or book.get('token_id', book.get('asset_id')) != token):
        raise ValueError('market_identity_mismatch')
    for stamp in (source.checked_at, metadata.get('metadata_checked_at'), metadata.get('fees_checked_at'), catalog.get('checked_at')):
        _freshness(stamp, as_of, 'market_freshness', max_age=60)
    rewards = [v for v in catalog.get('markets', ()) if isinstance(v, Mapping) and v.get('condition_id') == cid]
    if len(rewards) != 1:
        raise ValueError('reward_unknown')
    reward = rewards[0]
    minimum = metadata.get('reward_min_size')
    if minimum is None:
        minimum = decimal(reward.get('rewards_min_size'))
    spread = metadata.get('reward_max_spread')
    if spread is None:
        raw = decimal(reward.get('rewards_max_spread'))
        spread = None if raw is None else raw / 100
    market = {**metadata, 'token_id': selected['token_id'], 'outcome': str(selected['label']).upper(),
        'reward_min_size': minimum, 'reward_max_spread': spread}
    return freeze(dict(account=account.value, market=market, book=book))
