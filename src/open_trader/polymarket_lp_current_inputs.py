"""Adapt one captured frame for calculation without acquiring new facts."""
from collections.abc import Mapping
from dataclasses import replace
from types import MappingProxyType

from .polymarket_lp_market_reader import SourceResult, decimal, freeze


def prepare_current_inputs(frame, captured_candidates, policy, as_of):
    """Return immutable calculation inputs, preserving every captured source.

    Candidates are an already captured SourceResult, or None for UNKNOWN.
    Policy and evaluation time carry no source refresh or acquisition authority.
    Directions are keyed by exact current order ID; their source provenance stays
    in the original market SourceResults alongside the derived values.
    """
    directions = {}
    for order in frame.current_buys:
        condition, token = order['condition_id'], order['token_id']
        source = frame.sources.get(f'market:{condition}:{token}')
        if not source or not source.complete or source.error or not isinstance(source.value, Mapping):
            continue
        value = source.value
        metadata, catalog, book = (value.get(k) for k in ('metadata', 'rewards', 'book'))
        if not all(isinstance(v, Mapping) for v in (metadata, catalog, book)):
            continue
        if (metadata.get('condition_id') != condition
                or book.get('condition_id', book.get('market')) != condition
                or book.get('token_id', book.get('asset_id')) != token
                or catalog.get('complete') is not True):
            continue
        rows = [r for r in catalog.get('markets', ())
                if isinstance(r, Mapping) and r.get('condition_id') == condition]
        if len(rows) != 1:
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
    if captured_candidates is None:
        candidates = SourceResult('', complete=False)
    elif type(captured_candidates) is SourceResult:
        candidates = replace(captured_candidates, value=freeze(captured_candidates.value),
                             error_facts=freeze(captured_candidates.error_facts))
    else:
        raise TypeError('captured_candidates_requires_source_result')
    return replace(frame, sources=MappingProxyType({**frame.sources,
        'directions': SourceResult('', freeze(directions)), 'candidates': candidates}))
