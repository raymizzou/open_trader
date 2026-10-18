"""Bounded value-only D2 observations. These values grant no trading authority."""
from collections.abc import Mapping
from datetime import datetime
from .polymarket_lp_market_reader import CURRENT_ORDER_STATES, SourceResult, freeze


_REASONS = frozenset({'producer_fence_changed', 'producer_fence_unknown',
    'qualification_missing', 'qualification_expired', 'qualification_time_unknown',
    'qualification_unknown', 'qualification_identity_mismatch', 'book_freshness_stale',
    'current_account_facts_unknown', 'invalid_identity', 'stale',
    'capacity', 'timeout', 'in_progress', 'closed', 'provider_error',
    'buy_yield_unknown_timeout', 'queue_threshold', 'queue_data_failure_limit',
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
    'event_timing_unknown', 'event_starting_soon', 'event_status_unknown', 'scoring_continuously_lost'})


def bounded_reason_counts(reasons):
    counts = {}
    for reason in reasons:
        key = reason if reason in _REASONS and (reason in counts or len(counts.keys() - {'other'}) < 20) else 'other'
        counts[key] = counts.get(key, 0) + 1
    return counts


def append_outcome(summary, outcome, *, token, reason=None, protection=False):
    """Called once where an actual local attempt completes, never on projection."""
    by_kind = {kind: dict(counts) for kind, counts in summary.get('by_kind', {}).items()}
    subtypes = dict(summary.get('unknown_subtypes', {}))
    kind = 'BUY' if outcome.identity.target.kind.value == 'BUY_NEW' else 'CANCEL'
    raw = outcome.status.value
    disposition = 'UNKNOWN' if raw.startswith('unknown_') else 'NOT_SENT' if raw == 'not_sent' else raw
    subtype = raw.removeprefix('unknown_') if disposition == 'UNKNOWN' else None
    counts = by_kind.setdefault(kind, {})
    counts[disposition] = counts.get(disposition, 0)+1
    if subtype:
        subtypes[subtype] = subtypes.get(subtype, 0)+1
    code = next(iter(bounded_reason_counts((reason,)))) if reason else None
    recent = list(summary.get('recent', ()))
    recent.append(dict(request_id=outcome.identity.request_id, kind=kind, disposition=disposition,
        unknown_subtype=subtype, order_id=outcome.venue_order_id or
            getattr(outcome.identity.target, 'existing_order_id', None),
        generation=token.cycle_generation if token else None, batch_id=token.batch_id if token else None,
        round_number=token.round_number if token else None,
        observation_id=token.observation_id if token else None, reason=code,
        local_attempt_finished=outcome.local_attempt_finished, venue_finality='unavailable'))
    total = summary.get('total', 0)+1
    cancellations = {key: dict(counts) for key, counts in summary.get('protection_cancel_outcomes', {}).items()}
    if protection:
        key = code if code and (code in cancellations or len(cancellations.keys()-{'other'}) < 20) else 'other'
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
            retained_targets=sum(t in {o['order_id'] for o in prepared_frame.current_buys} for t in calculation.target_tokens),
            new_targets=len(fixed_plan.targets) if fixed_plan is not None else None)))
