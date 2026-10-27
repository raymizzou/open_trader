"""Bounded value-only D2 observations. These values grant no trading authority."""
from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import Enum
import json
from collections import OrderedDict
from contextlib import contextmanager
import hashlib
import hmac
import logging
import re
from threading import Lock, local
from time import monotonic
import uuid
from .polymarket_lp_market_reader import CURRENT_ORDER_STATES, SourceResult, freeze, observed_source_error


def diagnostic_json_copy(value):
    """Detach diagnostic values and reject unsupported data at this boundary."""
    def encode(item):
        if isinstance(item, (datetime, date)):
            return item.isoformat()
        if isinstance(item, Decimal):
            return str(item)
        if isinstance(item, Enum):
            return item.value
        if is_dataclass(item) and not isinstance(item, type):
            return {f.name: getattr(item, f.name) for f in fields(item)}
        if isinstance(item, Mapping):
            return dict(item)
        raise TypeError(type(item).__name__)
    return json.loads(json.dumps(value, default=encode, allow_nan=False))


# Final caps include room for the existing twenty unknown/rotation samples.
# The full-cap UTF-8 payload regression enforces the combined 256 KiB budget.
YIELD_ROW_LIMIT = 2
YIELD_ROUND_LIMIT = 2
YIELD_STRING_LIMIT = 96


def yield_diagnostic_value(value, *, depth=0):
    """Bound and detach only observational values; never coerce trading facts."""
    if depth > 10:
        return None
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, str):
        return value.encode('utf-8')[:YIELD_STRING_LIMIT].decode('utf-8',errors='ignore')
    if isinstance(value, float):
        import math
        return value if math.isfinite(value) else None
    if isinstance(value, Decimal):
        text = str(value)
        return text if value.is_finite() and len(text)<=YIELD_STRING_LIMIT else None
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(k)[:64]: yield_diagnostic_value(
            v[:8] if k in ('reason_codes', 'attempted_reason_codes', 'selection_reasons')
                and isinstance(v, (tuple, list)) else v, depth=depth+1)
            for k, v in list(value.items())[:40]}
    if isinstance(value, (list, tuple)):
        return [yield_diagnostic_value(v, depth=depth+1) for v in value[:20]]
    raise TypeError('unsupported_yield_diagnostic')


def bounded_yield_rows(rows, *, limit=YIELD_ROW_LIMIT):
    rows = tuple(rows)
    return dict(total=len(rows), samples=yield_diagnostic_value(rows[:limit]),
        omitted=max(0, len(rows)-limit))


def summarize_yield_observation(frame, calculation, ticket, acceptance, evaluated_at,
                                *, round_start=None, outcomes=(), fixed_plan=None, receipt_orders=()):
    """Attribute an attempted observation using only its captured sources."""
    errors = []
    names = tuple(dict.fromkeys(('balance', 'open_orders', 'positions', 'candidates',
        'candidate_inventory', *(n for n in frame.sources if n.startswith('market:')))))
    for name in names:
        source = frame.sources.get(name)
        if source is None and name not in ('balance', 'open_orders', 'positions', 'candidates'):
            continue
        invalid = name == 'open_orders' and not frame.orders_complete and bool(frame.errors)
        if source and source.complete and not source.error and source.value is not None and not invalid:
            continue
        error = ('invalid_order_identity_or_terms' if invalid else source.error if source else None)
        error = error or ('source_incomplete' if source else 'source_missing')
        if source and source.error and source.error not in _REASONS:
            error = observed_source_error(source)
        detail = source.diagnostics if source else {}
        parts = name.split(':', 2) if name.startswith('market:') else ()
        errors.append(dict(source=name, source_id=source.source_id if source else None,
            condition_id=parts[1] if len(parts)==3 else None,
            token_id=parts[2] if len(parts)==3 else None,
            source_error=str(error)[:64], error_class='input_invalid' if invalid else
                error if error in ('capacity', 'timeout', 'in_progress', 'closed') else
                'exception' if source and source.error else 'incomplete' if source else 'missing',
            http_status=source.error_facts.get('status') if source and type(source.error_facts.get('status')) is int else None,
            retry_cache_hit=detail.get('retry_cache_hit', False),
            remaining_retry_seconds=detail.get('remaining_retry_seconds'),
            inflight_age_seconds=detail.get('inflight_age_seconds')))
    candidates = frame.sources.get('candidates')
    supplied = tuple(r for r in candidates.value or () if not r.get('existing')) if candidates else ()
    ranked = sorted((dict(r) for r in calculation.candidate_observations
        if r.get('state') == 'known' and r.get('yield_pct_per_hour') is not None),
        key=lambda r: (-r['yield_pct_per_hour'], r['condition_id'], r['token_id']))
    candidate_sources = frame.sources.get('candidate_sources')
    for rank, row in enumerate(ranked, 1):
        per_direction = (candidate_sources.value.get(row['condition_id']+':'+row['token_id'])
            if candidate_sources else None)
        row.update(rank=rank, candidate_count=len(supplied), computed_candidate_count=len(ranked),
            estimate_source_id=per_direction.source_id if per_direction else candidates.source_id)
        # A fraction such as 1/20 can acquire last-place Decimal noise from
        # weighted depth division. Bound reporting precision, never the value
        # used by the estimator or selection.
        if isinstance(row.get('estimated_share'), Decimal):
            row['estimated_share'] = row['estimated_share'].quantize(Decimal('1e-24'))
            row['share_precision_decimal_places'] = 24
    placements = []
    for outcome in outcomes:
        target = outcome.identity.target
        if target.kind.value != 'BUY_NEW':
            continue
        matches = [r for r in ranked if (r['token_id'], r.get('market_id')) ==
            (target.token_id, target.market_id)]
        receipts = [order for order in receipt_orders
            if order.key[3] == outcome.venue_order_id and order.side == 'BUY'
            and order.source.source_id == outcome.identity.request_id]
        placed_at = (receipts[0].source.checked_at if outcome.status.value == 'accepted'
            and len(receipts) == 1 else None)
        placements.append(dict(matches[0] if len(matches) == 1 else {'available': False},
            request_id=outcome.identity.request_id,
            batch_id=round_start.token.batch_id if round_start and round_start.token else None,
            observation_id=ticket.observation_id, disposition=outcome.status.value,
            order_id=outcome.venue_order_id, venue_finality='unavailable',
            placed_at=placed_at, placement_time_basis='accepted_receipt' if placed_at else 'unavailable'))
    incumbents = []
    accepted_orders = {o.key[3]: o for o in acceptance.observation.orders} if acceptance.observation else {}
    for row in frame.current_buys:
        detail = dict(calculation.incumbent_observations.get(row['order_id'], {}))
        accepted_order = accepted_orders.get(row['order_id'])
        detail.update(reason_observation_id=ticket.observation_id,
            order_source_id=frame.sources['open_orders'].source_id,
            accepted_input=bool(accepted_order and accepted_order.presence == 'present'
                and accepted_order.price.value == row['price']
                and accepted_order.remaining.value == row['remaining']))
        incumbents.append(detail)
    by_id = {r['order_id']: r for r in frame.current_buys}
    def order_identity(order_id):
        row = by_id.get(order_id, {})
        return dict(order_id=order_id, condition_id=row.get('condition_id'), token_id=row.get('token_id'))
    return yield_diagnostic_value(dict(available=True,
        identity=dict(account_id=frame.account, generation=ticket.cycle_generation,
            observation_id=ticket.observation_id),
        read_ticket=dict(observation_id=ticket.observation_id, read_sequence=ticket.read_sequence,
            generation=ticket.cycle_generation, started_at=ticket.started_at),
        evaluated_at=evaluated_at, acceptance=acceptance.status,
        selection_state=calculation.selection_state,
        selection_reasons=calculation.selection_reasons,
        decision_states=calculation.decision_states,
        source_errors=bounded_yield_rows(errors), reader=frame.reader_diagnostics,
        candidate_count=len(supplied), computed_candidate_count=len(ranked),
        candidates=bounded_yield_rows(ranked), placement_estimates=bounded_yield_rows(placements),
        incumbents=bounded_yield_rows(incumbents),
        unknown_incumbents=bounded_yield_rows((r for r in incumbents if r.get('state') == 'unknown'), limit=20),
        selected=bounded_yield_rows(order_identity(o) for o in calculation.retained_order_ids),
        victims=bounded_yield_rows(order_identity(o) for o in calculation.victim_order_ids),
        new_targets=bounded_yield_rows(dict(condition_id=c, token_id=t) for c, t in calculation.new_target_keys),
        fixed_plan='absent' if fixed_plan is None else 'empty' if not fixed_plan.targets else 'nonempty'))


_REASONS = frozenset({'producer_fence_changed', 'producer_fence_unknown',
    'qualification_missing', 'qualification_expired', 'qualification_time_unknown',
    'qualification_unknown', 'qualification_identity_mismatch', 'book_freshness_stale',
    'current_account_facts_unknown', 'invalid_identity', 'stale',
    'capacity', 'timeout', 'in_progress', 'closed', 'provider_error',
    'buy_yield_unknown_timeout', 'queue_threshold', 'bid_level_changed', 'queue_data_failure_limit',
    'book_identity_mismatch', 'book_unknown', 'market_metadata_stale', 'market_fees_stale',
    'history_summary_unknown', 'history_summary_expired', 'history_time_unknown',
    'history_identity_mismatch', 'history_latest_refresh_failed', 'history_amplitude_unknown',
    'history_amplitude_exceeded', 'market_facts_unknown', 'market_identity_unknown',
    'market_metadata_time_unknown', 'market_fees_time_unknown', 'reward_time_unknown',
    'reward_data_stale', 'reward_deadline_unknown', 'reward_expired', 'reward_inactive',
    'reward_status_unknown', 'reward_pool_unknown', 'reward_pool_empty', 'market_not_accepting_orders',
    'account_facts_unknown', 'control_not_current', 'plan_identity', 'unsupported_send_option',
    'unsupported_order_shape', 'ambiguous_send_options', 'invalid_builder_code', 'control_or_native_not_current',
    'market_already_participating', 'balance_insufficient', 'orders_not_current_complete', 'review_at_too_soon',
    'order_identity', 'screen_time_unknown', 'market_status_unknown', 'book_invalid', 'book_crossed',
    'market_rules_unknown', 'market_rules_invalid', 'price_off_tick', 'midpoint_unknown', 'midpoint_out_of_range',
    'reward_distance_invalid', 'reward_score_zero', 'account_auth_unknown', 'account_identity_unknown',
    'account_identity_mismatch', 'account_funds_unknown', 'exit_liquidity_insufficient', 'exit_fee_unknown',
    'stress_loss_threshold', 'stress_loss_exceeded', 'account_freshness_stale', 'account_freshness_unknown',
    'book_freshness_unknown', 'book_freshness_invalid', 'account_freshness_invalid', 'bid_level_invalid',
    'second_bid_insufficient', 'event_in_progress', 'event_recovery_pending', 'event_start_time_unknown',
    'event_end_time_unknown', 'event_end_time_in_future', 'post_event_screening_unknown',
    'event_timing_unknown', 'event_starting_soon', 'event_status_unknown', 'scoring_continuously_lost',
    'mutation_not_admitted', 'mutation_lease_not_current', 'independent_cancel_identity',
    'independent_cancel_not_current', 'independent_cancel_inflight', 'rotation_not_current',
    'protection_cancel_owned', 'protection_cancel_not_current', 'protection_evidence_not_current', 'native_capacity'})


def bounded_reason_counts(reasons):
    counts = {}
    for reason in reasons:
        key = reason if reason in _REASONS and (reason in counts or len(counts.keys() - {'other'}) < 20) else 'other'
        counts[key] = counts.get(key, 0) + 1
    return counts


def append_outcome(summary, outcome, *, token=None, record=None, reason=None, protection=False):
    """Called once where an actual local attempt completes, never on projection."""
    by_kind = {kind: dict(counts) for kind, counts in summary.get('by_kind', {}).items()}
    subtypes = dict(summary.get('unknown_subtypes', {}))
    if record is None:
        kind = 'BUY' if outcome.identity.target.kind.value == 'BUY_NEW' else 'CANCEL'
        raw = outcome.status.value
        request_id = outcome.identity.request_id
        order_id = outcome.venue_order_id or getattr(outcome.identity.target, 'existing_order_id', None)
        generation = token.cycle_generation if token else None
        local_finished = outcome.local_attempt_finished
    else:
        # Protective results have no ActionIdentity/LocalStatus. Their original
        # immutable RequestRecord supplies kind and provenance, never fresh state.
        kind, raw = record.kind, outcome.status
        request_id, order_id = outcome.request_id, outcome.order_id or record.order_id
        generation, local_finished = record.cycle_generation, True
        token = record
    disposition = 'UNKNOWN' if raw == 'unknown' or raw.startswith('unknown_') else 'NOT_SENT' if raw == 'not_sent' else raw
    subtype = raw.removeprefix('unknown_') if raw in ('unknown_timeout', 'unknown_error') else None
    counts = by_kind.setdefault(kind, {})
    counts[disposition] = counts.get(disposition, 0)+1
    if disposition == 'UNKNOWN':
        subtype_key = subtype or 'unavailable'
        subtypes[subtype_key] = subtypes.get(subtype_key, 0)+1
    trigger_code = next(iter(bounded_reason_counts((reason,)))) if reason else None
    actual_reason = getattr(outcome, 'reason_code', None) if disposition == 'NOT_SENT' else None
    code = next(iter(bounded_reason_counts((actual_reason,)))) if actual_reason else trigger_code
    recent = list(summary.get('recent', ()))
    recent.append(dict(request_id=request_id, kind=kind, disposition=disposition,
        unknown_subtype=subtype, order_id=order_id,
        generation=generation, batch_id=token.batch_id if token else None,
        round_number=token.round_number if token else None,
        observation_id=token.observation_id if token else None, reason=code,
        local_attempt_finished=local_finished, venue_finality='unavailable'))
    if protection and actual_reason and trigger_code:
        recent[-1]['trigger_reason'] = trigger_code
    total = summary.get('total', 0)+1
    cancellations = {key: dict(counts) for key, counts in summary.get('protection_cancel_outcomes', {}).items()}
    if protection:
        key = trigger_code if trigger_code and (trigger_code in cancellations or len(cancellations.keys()-{'other'}) < 20) else 'other'
        counts = cancellations.setdefault(key, {})
        counts[raw] = counts.get(raw, 0)+1
    return freeze(dict(total=total, omitted=max(0, total-20), by_kind=by_kind,
        unknown_subtypes=subtypes, recent=recent[-20:], protection_cancel_outcomes=cancellations))


def summarize_reader_coverage(frame, *, started_at):
    """Requested identities can overlap roles; the union counts each once.

    Offsets measure source completion relative to the ticket, never SDK latency
    or proof that a timed-out native request actually started.
    """
    roles = {name: set() for name in ('current_buy', 'current_sell', 'nonzero_position', 'other_related')}
    for order in frame.orders:
        current = order['status'] in CURRENT_ORDER_STATES and order['remaining'] > 0
        name = ('current_buy' if order['side'] == 'BUY' else 'current_sell') if current else 'other_related'
        roles[name].add((order['condition_id'], order['token_id']))
    for position in frame.positions:
        name = 'nonzero_position' if position['quantity'] > 0 else 'other_related'
        roles[name].add((position['condition_id'], position['token_id']))

    def summarize(names, known):
        complete = failed = missing = fresh = reused = untimed = 0
        offsets, reasons = [], []
        for name in names:
            source = frame.sources.get(name)
            if source is None:
                missing += 1
                continue
            success = source.complete and not source.error and source.value is not None
            if success:
                complete += 1
                stamp = source.checked_at
            else:
                failed += 1
                reason = source.error if source.error in ('capacity', 'timeout', 'in_progress', 'closed') else 'provider_error'
                reasons.append(reason)
                stamp = source.failed_at
                if stamp is not None and started_at is not None:
                    if stamp >= started_at: fresh += 1
                    elif source.retry_at is not None: reused += 1
                    else: untimed += 1
                else:
                    untimed += 1
            if isinstance(stamp, datetime) and started_at is not None and stamp >= started_at:
                offsets.append((stamp-started_at).total_seconds())
        return dict(requested=len(names) if known else None, observed_identities=len(names),
            complete=complete, failed=failed, missing=missing, reasons=bounded_reason_counts(reasons),
            fresh_failures=fresh, reused_backoff_failures=reused, untimed_failures=untimed,
            completion_offset_seconds=dict(min=min(offsets) if offsets else None, max=max(offsets) if offsets else None))

    both = frame.orders_complete and frame.positions_complete
    summaries = {name: summarize(tuple(f'market:{c}:{t}' for c, t in sorted(identities)),
        frame.orders_complete if name in ('current_buy', 'current_sell') else
        frame.positions_complete if name == 'nonzero_position' else both)
        for name, identities in roles.items()}
    union = set().union(*roles.values())
    return dict(roles=summaries, union=summarize(tuple(f'market:{c}:{t}' for c, t in sorted(union)), both),
        financial={name: summarize((name,), True) for name in ('balance', 'open_orders', 'positions')})


def _source_summary(sources):
    sources = tuple(sources)
    stamps = [s.checked_at for s in sources if isinstance(s.checked_at, datetime)]
    return dict(source_id=sources[0].source_id if len(sources) == 1 else None,
        total=len(sources), timestamped=len(stamps), missing_timestamps=len(sources)-len(stamps),
        checked_at=min(stamps) if stamps and len(stamps) == len(sources) else None,
        oldest_checked_at=min(stamps) if stamps else None, newest_checked_at=max(stamps) if stamps else None,
        complete=all(s.complete and not s.error for s in sources) if sources else None,
        reason=next(iter(bounded_reason_counts(s.error for s in sources if s.error)), None),
        reasons=bounded_reason_counts(s.error for s in sources if s.error))


def _accepted_sources(frame, capture):
    result = {name: _source_summary((frame.sources[name],) if name in frame.sources else ())
        for name in ('balance', 'open_orders', 'positions')}
    qualification = frame.sources.get('candidate_sources')
    qualified = (tuple(capture.rows.values()) if capture is not None and hasattr(capture, 'rows') else
        tuple(qualification.value.values()) if qualification and qualification.value else ())
    result['qualification'] = _source_summary(qualified)
    components = {name: [] for name in ('book', 'metadata', 'fees', 'rewards', 'history')}

    def stamp(value):
        if isinstance(value, datetime): return value
        if isinstance(value, str):
            try: return datetime.fromisoformat(value.replace('Z', '+00:00'))
            except ValueError: pass
        return None

    def direction_sources(direction, source):
        if not isinstance(direction, Mapping): return
        market, book, history = (direction.get(k) for k in ('market', 'book', 'history_summary'))
        values = dict(book=book.get('received_at') if isinstance(book, Mapping) else None,
            metadata=market.get('metadata_checked_at') if isinstance(market, Mapping) else None,
            fees=market.get('fees_checked_at') if isinstance(market, Mapping) else None,
            rewards=direction.get('reward_checked_at'),
            history=history.get('checked_at', history.get('updated_at')) if isinstance(history, Mapping) else None)
        for name, value in values.items():
            components[name].append(SourceResult(source.source_id, complete=source.complete,
                checked_at=stamp(value), error=source.error))

    for source in qualified:
        value = source.value
        facts = value.get('facts') if isinstance(value, Mapping) else None
        for direction in facts.get('directions', ()) if isinstance(facts, Mapping) else ():
            direction_sources(direction, source)
    for name, source in frame.sources.items():
        if not name.startswith('market:') or not isinstance(source.value, Mapping): continue
        value = source.value
        rewards = value.get('rewards')
        direction_sources(dict(market=value.get('metadata'), book=value.get('book'),
            reward_checked_at=rewards.get('checked_at') if isinstance(rewards, Mapping) else None), source)
    result.update({name: _source_summary(sources) for name, sources in components.items()})
    return result


def project_diagnostics(summary, *, as_of):
    """Copy a stored value summary and calculate age without acquiring facts."""
    def copy(value):
        if isinstance(value, Mapping): return {k: copy(v) for k, v in value.items()}
        if isinstance(value, tuple): return [copy(v) for v in value]
        return value
    result = copy(summary)
    accepted = result.get('accepted_observation')
    if accepted:
        accepted['age_seconds'] = (as_of-accepted['accepted_at']).total_seconds()
        for source in accepted['sources'].values():
            stamp = source['checked_at']
            source['age_seconds'] = (as_of-stamp).total_seconds() if stamp else None
            oldest, newest = source['oldest_checked_at'], source['newest_checked_at']
            source['age_range_seconds'] = dict(min=(as_of-newest).total_seconds() if newest else None,
                max=(as_of-oldest).total_seconds() if oldest else None)
    return result


def summarize_iteration(*, attempted_at, ticket, prepared_frame, calculation,
                        acceptance, round_start, outcomes, fixed_plan=None, capture=None,
                        evaluated_at=None, round_number=None, accepted_capacity=None):
    sources = prepared_frame.sources
    inventory = sources.get('candidate_inventory')
    eligibility = sources.get('candidate_eligibility')
    values = eligibility.value if eligibility and eligibility.value else {}
    identities = inventory.value if inventory and inventory.value is not None else None
    invalid = [values[cid] for cid in identities or () if cid in values]
    directions = [value for key, value in values.items() if identities is None or key not in identities]
    eligible = [key for key, value in values.items() if value.get('state') == 'eligible']
    local = {}
    for outcome in outcomes:
        local = append_outcome(local, outcome, token=round_start.token if round_start else None)
    observation = acceptance.observation
    accepted = None
    if observation is not None:
        accepted = dict(observation_id=observation.observation_id,
            generation=observation.cycle_generation, accepted_at=observation.accepted_at,
            evaluated_at=evaluated_at,
            read_ticket=dict(observation_id=ticket.observation_id, read_sequence=ticket.read_sequence,
                generation=ticket.cycle_generation, started_at=ticket.started_at,
                trade_version_at_start=ticket.trade_version_at_start), sources=_accepted_sources(prepared_frame, capture))
    capture_view = (dict(capture_id=capture.capture_id, generation=capture.generation,
        captured_at=capture.captured_at, complete=capture.inventory_complete,
        reason=next(iter(bounded_reason_counts((capture.error,))), None) if capture.error else None)
        if capture is not None and hasattr(capture, 'capture_id') else None)
    return freeze(dict(version=1,
        reader_coverage=dict(summarize_reader_coverage(prepared_frame, started_at=ticket.started_at),
            observation_id=ticket.observation_id, generation=ticket.cycle_generation),
        latest_attempt=dict(attempted_at=attempted_at, stage='local_attempts' if outcomes else 'round_start' if round_start else 'acceptance',
            disposition=acceptance.status, reason=(next(iter(bounded_reason_counts((acceptance.status,))))
                if observation is None else None), capture=capture_view),
        accepted_observation=accepted, admission=dict(
        observation_id=ticket.observation_id, generation=ticket.cycle_generation,
        samples=dict(total=len(values), omitted=max(0, len(values)-20), recent=[dict(
            identity=key, state=value.get('state') if value.get('state') in ('eligible', 'rejected', 'unknown') else 'unknown',
            reasons=bounded_reason_counts(value.get('reason_codes', ()))) for key, value in list(values.items())[:20]]),
        capacity=dict(state='known' if observation is not None and accepted_capacity is not None
            and accepted_capacity.state == 'known' else 'unknown',
            value=str(accepted_capacity.value) if observation is not None and accepted_capacity is not None
            and accepted_capacity.state == 'known' else None),
        inventory=dict(total=len(inventory.value) if inventory and inventory.value is not None else None,
            valid=len(identities)-len(invalid) if identities is not None else None,
            invalid=len(invalid) if identities is not None else None,
            complete=bool(inventory and inventory.complete)),
        evaluated_directions=len(directions) if eligibility else None,
        unknown_directions=sum(v.get('state') == 'unknown' for v in directions) if eligibility else None,
        rejected_directions=sum(v.get('state') == 'rejected' for v in directions) if eligibility else None,
        direction_reasons=bounded_reason_counts(r for value in directions for r in value.get('reason_codes', ())),
        market_reasons=bounded_reason_counts(r for value in invalid for r in value.get('reason_codes', ())),
        eligible_directions=len(eligible) if eligibility else None,
        eligible_markets=len({k.rsplit(':', 1)[0] for k in eligible}) if eligibility else None),
        local_outcomes=local,
        round_start=dict(status=round_start.status.value if round_start else None,
            reason=next(iter(bounded_reason_counts((round_start.reason,)))) if round_start and round_start.reason else None,
            generation=round_start.token.cycle_generation if round_start and round_start.token else ticket.cycle_generation,
            batch_id=round_start.token.batch_id if round_start and round_start.token else None,
            round_number=round_start.token.round_number if round_start and round_start.token else round_number,
            observation_id=ticket.observation_id,
            fixed_buy_plan='absent' if fixed_plan is None else 'empty' if not fixed_plan.targets else 'nonempty',
            selected_targets=len(calculation.target_tokens),
            retained_targets=len(calculation.retained_order_ids),
            new_targets=len(fixed_plan.targets) if fixed_plan is not None else None)))


# Service-log episodes are separate from the existing value-only D2 views.
logger = logging.getLogger(__name__)
_STAGES = frozenset({'reward_native', 'reward_sponsored', 'account_read',
    'account_publish', 'entry_validate', 'entry_prepare', 'entry_send', 'receipt_apply'})
_ERROR_REASONS = frozenset({
    'reward_read_failed', 'reward_read_cancelled', 'reward_identity_unknown',
    'reward_config_unknown', 'reward_asset_unknown', 'reward_total_unknown',
    'reward_totals_inconsistent', 'account_snapshot_unknown',
    'account_snapshot_incomplete', 'account_identity_mismatch', 'account_round_invalid',
    'account_snapshot_stale', 'account_open_orders_unknown', 'account_order_identity_unknown',
    'account_raw_trades_unknown', 'account_order_sync_unknown', 'account_read_failed',
    'account_read_in_progress', 'account_read_capacity', 'account_read_timeout',
    'account_read_cooling_down', 'account_history_unknown', 'account_balance_unknown',
    'account_positions_unknown', 'account_trades_unknown', 'session_changed',
    'order_original_quantity_conflict', 'order_limit_price_conflict', 'baseline_unknown',
    'balance_insufficient', 'allowance_insufficient', 'best_bid_changed',
    'candidate_bid_level_changed', 'candidate_bid_level_unavailable',
    'market_read_in_progress', 'market_read_capacity', 'market_read_timeout',
    'market_read_cooling_down', 'market_closed', 'prepare_failed', 'pre_send_rejected',
    'send_unknown', 'exchange_rejected', 'receipt_apply_pending', 'receipt_received',
    'accepted_without_order_id', 'idempotency_key_required', 'mutation_blocked',
    'lp_session_market_active', 'entry_audit_registration_failed', 'account_identity_unknown',
    'not_enough_balance', 'not_enough_allowance', 'venue_denied',
    'order_identity_unknown', 'order_identity_conflict', 'order_fill_exceeds_quantity',
    'legacy_order_ambiguous', 'account_order_conflict', 'account_order_owner_unknown',
    'account_read_order_invalid', 'account_financial_facts_unknown', 'credential_invalid',
    'history_summary_unknown', 'review_at_too_soon', 'execution_lock',
})


def safe_reason(value):
    return value if type(value) is str and len(value) <= 96 and value in _ERROR_REASONS else 'unknown'


def _exception_types(exc):
    types, seen = [], set()
    while isinstance(exc, BaseException) and id(exc) not in seen and len(types) < 8:
        seen.add(id(exc))
        name = type(exc).__name__
        types.append(name if re.fullmatch(r'[A-Za-z_][A-Za-z_0-9]{0,63}', name) else 'unknown')
        exc = exc.__cause__ if exc.__cause__ is not None else None if exc.__suppress_context__ else exc.__context__
    return types


class LPDiagnostics:
    """One owner, at most 256 scopes, one short state lock, no output under it."""
    def __init__(self):
        self._salt = uuid.uuid4().bytes
        self._owner_id = uuid.uuid4().hex
        self._scopes = OrderedDict()
        self._lock = Lock()
        self._association = local()
        self._sequence = 0
        self._evictions = 0

    def opaque(self, value):
        if type(value) is not str or not value or len(value) > 4096:
            return 'unknown'
        return hmac.new(self._salt, value.encode(), hashlib.sha256).hexdigest()[:24]

    def begin(self, scope, identity='scope', *, read_started_at=None):
        try:
            if scope not in {'account_full', 'account_planning', 'reward_native', 'reward_sponsored', 'entry'}:
                return None
            key = self.opaque(scope + ':' + identity)
            if key == 'unknown':
                return None
            with self._lock:
                self._sequence += 1
                attempt = self._sequence
                if key not in self._scopes:
                    if len(self._scopes) >= 256:
                        self._scopes.popitem(last=False)
                        self._evictions += 1
                    self._scopes[key] = dict(latest=attempt, episode=None, scope=scope)
                state = self._scopes[key]
                state['latest'] = attempt
                state['read_admitted_at'] = (read_started_at.astimezone(UTC)
                    if isinstance(read_started_at, datetime) and read_started_at.tzinfo is not None else None)
                self._scopes.move_to_end(key)
            return (key, attempt)
        except Exception:
            return None

    @contextmanager
    def associate(self, attempt, *, scope, recovery=True, round_id=None):
        """Explicit call-scoped propagation; thread identity is never correlation."""
        previous = getattr(self._association, 'current', None)
        self._association.current = dict(attempt=attempt, scope=scope,
            recovery=recovery, round_id=round_id)
        try:
            yield
        finally:
            self._association.current = previous

    def associated(self, scope):
        current = getattr(self._association, 'current', None)
        return current if current and current['scope'] == scope else {}

    def pending(self, scope, identity):
        """Only an existing episode can be completed by an applied receipt."""
        try:
            key = self.opaque(scope + ':' + identity)
            with self._lock:
                state = self._scopes.get(key)
                return (key, state['latest']) if state and state['episode'] else None
        except Exception:
            return None

    def record(self, attempt, *, event, stage, cause=None, summary=None, exc=None,
               event_at=None, request=None, task=None, round_id=None, post_started=None,
               result=None, error_facts=None, component=None, account=None):
        try:
            if attempt is None or stage not in _STAGES or event not in {'failed', 'waiting', 'complete', 'sent'}:
                return
            key, number = attempt
            cause = safe_reason(cause)
            types = _exception_types(exc)
            if error_facts is None and exc is not None:
                error_facts = getattr(exc, 'response_facts', None)
            if isinstance(error_facts, Mapping):
                supplied = error_facts.get('error_chain')
                if isinstance(supplied, (list, tuple)):
                    captured = [name for name in supplied[:8] if type(name) is str
                        and len(name) <= 64 and re.fullmatch(r'[A-Za-z_][A-Za-z_0-9]{0,63}', name)]
                    types = list(dict.fromkeys([*types, *captured]))[:8]
            stamp = event_at.isoformat() if isinstance(event_at, datetime) else datetime.now(UTC).isoformat()
            now = monotonic()
            if task is None:
                from .polymarket_trading import _lp_read_local
                active_task = getattr(_lp_read_local, 'task', None)
                task = active_task.get('task_id') if isinstance(active_task, dict) else None
            row = dict(event_at=stamp, event=event, stage=stage, cause_code=cause,
                summary_reason=safe_reason(summary), exception_types=types,
                owner_id=self._owner_id, scope_id=key, attempt_id=self.opaque(str(number)),
                request_id=self.opaque(request), task_id=self.opaque(task), round_id=self.opaque(round_id))
            row['output_continuity'] = 'unknown'
            if isinstance(account, Mapping):
                for field in ('read_started_at', 'read_ended_at', 'checked_at'):
                    value = account.get(field)
                    if type(value) is str and len(value) <= 64:
                        try:
                            value = datetime.fromisoformat(value.replace('Z', '+00:00'))
                        except ValueError:
                            value = None
                    if isinstance(value, datetime) and value.tzinfo is not None:
                        row[field] = value.astimezone(UTC).isoformat()
                generation = account.get('trade_generation')
                if type(generation) is int and 0 <= generation <= 2**63 - 1:
                    row['trade_generation'] = generation
            if component in {'balance', 'open_orders', 'positions'}:
                row['component'] = component
            if isinstance(error_facts, Mapping) and type(error_facts.get('status')) is int and 100 <= error_facts['status'] <= 599:
                row['status'] = error_facts['status']
            if type(post_started) is bool:
                row['post_started'] = post_started
            if result in {'unknown', 'accepted', 'rejected'}:
                row['result'] = result
            with self._lock:
                state = self._scopes.get(key)
                current = state is not None and state['latest'] == number
                row.update(current_attempt=current, continuity='incomplete' if self._evictions or state is None else 'process_local', evicted_scopes=self._evictions)
                episode = state['episode'] if state else None
                row['scope'] = state['scope'] if state else 'unknown'
                if event == 'complete' and episode and row['scope'] == 'account_full':
                    boundary = episode.get('latest_read_admitted_at')
                    source = row.get('read_started_at')
                    source = datetime.fromisoformat(source) if source else None
                    source_current = boundary is None or source is not None and source >= boundary
                    row['source_current'] = source_current
                    if not source_current:
                        current = False
                        row['current_attempt'] = False
                if not current:
                    # Observe a late completion without changing the new state.
                    if episode is None:
                        return
                    row['episode_id'] = episode['id']
                elif event == 'complete':
                    if episode is None:
                        return
                    for field in ('request_id', 'round_id', 'task_id'):
                        if row[field] == 'unknown':
                            row[field] = episode['context'][field]
                    row.update(event='recovered', first_cause_code=episode['first'],
                        latest_cause_code=episode['cause'], first_event_at=episode['first_at'],
                        latest_event_at=episode['latest_at'], failure_count=episode['count'],
                        episode_id=episode['id'], first_exception_types=episode['first_types'],
                        latest_exception_types=episode['latest_types'], first_stage=episode['first_stage'],
                        latest_stage=episode['latest_stage'], first_attempt_id=episode['first_attempt'],
                        latest_attempt_id=episode['latest_attempt'])
                    state['episode'] = None
                elif event in {'failed', 'waiting'}:
                    signature = (event, stage, cause, tuple(types), component)
                    if episode is None:
                        episode = dict(first=cause, first_at=stamp, count=0, id=uuid.uuid4().hex,
                            channels={}, first_types=types, first_stage=stage,
                            first_attempt=row['attempt_id'], context={field: row[field] for field in ('request_id', 'round_id', 'task_id')})
                        state['episode'] = episode
                    admitted = state.get('read_admitted_at')
                    if admitted is not None:
                        episode['latest_read_admitted_at'] = admitted
                    episode['count'] += 1
                    episode.update(cause=cause, latest_at=stamp, latest_types=types, latest_stage=stage, latest_attempt=row['attempt_id'])
                    # Both keys are controlled allowlists (8 stages x at most
                    # 4 component values), so interleaving cannot defeat the
                    # window or create an unbounded channel map.
                    channel_key = (stage, row.get('component'))
                    channel = episode['channels'].setdefault(channel_key,
                        dict(signature=None, last_emit=now, repeats=0))
                    repeated = channel['signature'] == signature
                    channel['signature'] = signature
                    channel['repeats'] = channel['repeats'] + 1 if repeated else 0
                    if repeated and now - channel['last_emit'] < 60:
                        return
                    channel['last_emit'] = now
                    row.update(episode_id=episode['id'], first_cause_code=episode['first'],
                        first_event_at=episode['first_at'], latest_cause_code=cause,
                        failure_count=episode['count'], repeat_count=channel['repeats'])
            # Queue32 and its single existing consumer isolate every handler.
            from .polymarket_trading import _lp_capture_read_log
            level = logging.WARNING if row['event'] == 'failed' else logging.INFO
            _lp_capture_read_log(lambda: logger.log(level, 'lp_diagnostic %s', json.dumps(row, separators=(',', ':'))))
        except Exception:
            pass
