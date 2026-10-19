#!/usr/bin/env python3
"""Read-only LP yield baseline sampling and offline JSONL analysis."""
from collections import Counter
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
import json
import math


def _time(value):
    if not isinstance(value,str):
        return None
    try:
        result=datetime.fromisoformat(value.replace('Z','+00:00'))
        return result.astimezone(UTC) if result.tzinfo is not None else None
    except (ValueError,OverflowError):
        return None


def _order_key(row):
    values=tuple(row.get(k) for k in ('account_id','condition_id','token_id','order_id'))
    return values if all(isinstance(v,str) and v for v in values) else None


def _reason(row):
    reasons=row.get('reason_codes')
    return next((v for v in reasons if isinstance(v,str) and v), 'reason_unavailable') if isinstance(reasons,(list,tuple)) else 'reason_unavailable'


def _number(value):
    if isinstance(value,bool):
        return None
    try:
        number=float(value)
        return number if math.isfinite(number) else None
    except (TypeError,ValueError,OverflowError):
        return None


def _mapping(value):
    return value if isinstance(value,Mapping) else {}


def _rows(bucket):
    values=_mapping(bucket).get('samples',[])
    return [r for r in values if isinstance(r,Mapping)] if isinstance(values,(list,tuple)) else []


def _observable(row):
    return bool(_order_key(row) and row.get('accepted_input') is not False
        and row.get('state') in ('known','unknown')
        and (row.get('state')!='known' or _number(row.get('yield_pct_per_hour')) is not None))


def _share(value):
    try:
        number=Decimal(str(value))
        return number if number.is_finite() and 0<=number<=1 else None
    except (InvalidOperation,ValueError,TypeError):
        return None


def _observation_rows(observation, diagnostics, *, latest=False):
    rows={_order_key(r):r for r in _rows(observation.get('incumbents')) if _order_key(r)}
    if not latest:
        return list(rows.values())
    observation_id=observation['identity']['observation_id']
    unknown=_mapping(diagnostics.get('unknown_buy_yields')).get('recent',[])
    for row in unknown if isinstance(unknown,(list,tuple)) else ():
        if (isinstance(row,Mapping) and row.get('reason_is_current') is True
                and row.get('reason_observation_id')==observation_id and _order_key(row)):
            rows.setdefault(_order_key(row),dict(row,state='unknown'))
    rotation=_mapping(diagnostics.get('rotation'))
    if _mapping(rotation.get('identity')).get('observation_id')==observation_id:
        for value in _rows(rotation.get('incumbent_yields')):
            identity=value.get('identity')
            if value.get('kind')!='incumbent' or not isinstance(identity,(list,tuple)) or len(identity)!=4:
                continue
            row=dict(_mapping(value.get('observation')),
                **dict(zip(('account_id','condition_id','token_id','order_id'),identity)),
                state=value.get('state'),yield_pct_per_hour=value.get('yield_pct_per_hour'))
            if _order_key(row): rows.setdefault(_order_key(row),row)
    return list(rows.values())


def summarize_samples(samples):
    """Summarize saved sample envelopes without network or database access."""
    counts,durations=Counter(),Counter()
    coverage=Counter({name:0 for name in ('samples_total','usable_samples','invalid_lines',
        'poll_errors','missing_diagnostics','truncated_rows','ring_resets','out_of_order_samples',
        'duplicate_samples','invalid_estimates','unmeasured_intervals','unmeasured_seconds')})
    coverage.update(missed_rounds=0,left_censored_rounds=0)
    seen=set()
    seen_rounds=set()
    no_decision=0
    causes,error_classes,http_status=Counter(),Counter(),Counter()
    retry_hits=capacity_events=0
    inflight_max=retry_max=None
    orders={}
    placements={}
    unavailable_placements=0
    flaps=dict(total=0,unknown_to_known=0,known_to_unknown=0)
    previous={};previous_at=None
    last_sample_at=last_instance=None
    last_total=None
    gap=False
    for raw in samples:
        coverage['samples_total']+=1
        try:
            envelope=json.loads(raw) if isinstance(raw,str) else raw
        except (ValueError,TypeError):
            coverage['invalid_lines']+=1;gap=True;continue
        if not isinstance(envelope,Mapping):
            coverage['invalid_lines']+=1;gap=True;continue
        at=_time(envelope.get('sampled_at'))
        if at is None:
            coverage['invalid_lines']+=1;gap=True;continue
        if last_sample_at is not None and at<=last_sample_at:
            coverage['duplicate_samples' if at==last_sample_at else 'out_of_order_samples']+=1
            gap=True;continue
        last_sample_at=at
        if envelope.get('error'):
            coverage['poll_errors']+=1;gap=True;continue
        diagnostics=_mapping(_mapping(envelope.get('state')).get('diagnostics'))
        history=_mapping(diagnostics.get('yield_rounds'))
        instance=history.get('instance_id')
        if not isinstance(instance,str): instance=None
        if instance and last_instance and instance!=last_instance:
            coverage['ring_resets']+=1;gap=True;last_total=None
        if instance: last_instance=instance
        values=history.get('recent',[])
        recent=[r for r in values if isinstance(r,Mapping) and
            isinstance(_mapping(r.get('identity')).get('observation_id'),str) and
            _mapping(r.get('identity')).get('observation_id') and
            all(isinstance(_mapping(r.get('identity')).get(k),(str,type(None)))
                for k in ('account_id','generation'))] if isinstance(values,(list,tuple)) else []
        if not instance or not recent:
            coverage['missing_diagnostics']+=1;gap=True;continue
        total_observations=history.get('total_observations')
        if type(total_observations) is int:
            new_count=sum((instance,r['identity'].get('account_id'),r['identity'].get('generation'),
                r['identity']['observation_id']) not in seen_rounds for r in recent)
            if last_total is None:
                coverage['left_censored_rounds']+=max(0,total_observations-new_count)
            elif total_observations<last_total:
                coverage['ring_resets']+=1;gap=True
            elif total_observations-last_total>new_count:
                coverage['missed_rounds']+=total_observations-last_total-new_count;gap=True
            last_total=total_observations
        coverage['usable_samples']+=1
        for index,observation in enumerate(recent):
            identity=observation['identity']
            round_key=(history['instance_id'],identity.get('account_id'),identity.get('generation'),identity['observation_id'])
            if round_key not in seen_rounds:
                seen_rounds.add(round_key)
                for placement in _rows(observation.get('placement_estimates')):
                    key=_order_key(placement)
                    if placement.get('disposition')!='accepted' or key is None:
                        unavailable_placements+=1
                        continue
                    placements.setdefault(key,dict(placement,
                        _estimate_at=_time(placement.get('checked_at')) or _time(observation.get('evaluated_at')),
                        observed_share=None))
                for name in ('incumbents','source_errors','placement_estimates'):
                    omitted=_mapping(observation.get(name)).get('omitted',0)
                    if type(omitted) is int:
                        coverage['truncated_rows']+=max(0,omitted)
                source_keys=set();round_causes=set()
                for error in _rows(observation.get('source_errors')):
                    source=error.get('source')
                    if not isinstance(source,str): continue
                    source_key=(source,*(error.get(k) if isinstance(error.get(k),str) else None
                        for k in ('condition_id','token_id')))
                    if source_key in source_keys: continue
                    source_keys.add(source_key)
                    error_class=error.get('error_class')
                    if not isinstance(error_class,str) or not error_class:
                        error_class='unattributed'
                    error_classes[error_class]+=1
                    retry=error.get('retry_cache_hit') is True
                    retry_hits+=retry
                    capacity_events+=error_class=='capacity'
                    round_causes.add('retry_cache' if retry else error_class)
                    if type(error.get('http_status')) is int:
                        http_status[str(error['http_status'])]+=1
                    remaining=_number(error.get('remaining_retry_seconds'))
                    if remaining is not None:
                        retry_max=max(retry_max or 0,remaining)
                    age=_number(error.get('inflight_age_seconds'))
                    if age is not None:
                        inflight_max=max(inflight_max or 0,age)
                jobs=_mapping(observation.get('reader')).get('jobs',[])
                for job in jobs if isinstance(jobs,(list,tuple)) else ():
                    if not isinstance(job,Mapping): continue
                    age=_number(job.get('age_seconds'))
                    if age is not None:
                        inflight_max=max(inflight_max or 0,age)
                if observation.get('selection_state')=='NO_DECISION':
                    no_decision+=1
                    causes.update(round_causes or ('unattributed',))
            for row in _observation_rows(observation,diagnostics,latest=index==len(recent)-1):
                key=_order_key(row)
                event=(history['instance_id'],identity.get('generation'),identity['observation_id'],key)
                if key and event not in seen:
                    seen.add(event)
                    if not _observable(row):
                        coverage['invalid_estimates']+=row.get('state')=='known'
                        continue
                    if row.get('state')=='unknown': counts[_reason(row)]+=1
                    observed_at=_time(observation.get('evaluated_at')) or at
                    venue_placed=_time(row.get('placed_at'))
                    receipt_placed=_time(placements.get(key,{}).get('placed_at'))
                    placement_basis='order_created_at' if venue_placed else 'accepted_receipt' if receipt_placed else 'first_sighting_proxy'
                    record=orders.setdefault(key,dict(zip(('account_id','condition_id','token_id','order_id'),key),
                        first_seen_at=observed_at,placed_at=venue_placed or receipt_placed,
                        placement_time_basis=placement_basis,
                        first_known_at=None,left_censored=row.get('state')=='known',right_censored=False,
                        coverage_gap=False))
                    record['coverage_gap']=record['coverage_gap'] or gap
                    if record['placed_at'] is None:
                        record['placed_at']=venue_placed or receipt_placed
                        record['placement_time_basis']=placement_basis
                    if row.get('state')=='known' and record['first_known_at'] is None:
                        record['first_known_at']=observed_at
                    record['right_censored']=row.get('state')=='unknown'
                    placement=placements.get(key)
                    share=_share(row.get('estimated_share')) if row.get('state')=='known' else None
                    if placement and share is not None and placement['observed_share'] is None:
                        estimate_at=placement['_estimate_at']
                        if estimate_at is not None and observed_at>estimate_at:
                            placement['observed_share']=share
        current={_order_key(r):r for r in _observation_rows(recent[-1],diagnostics,latest=True) if _observable(r)}
        if previous_at is not None:
            if gap or current.keys()!=previous.keys():
                coverage['unmeasured_intervals']+=1
                coverage['unmeasured_seconds']+=(at-previous_at).total_seconds()
            for key in (() if gap else current.keys() & previous.keys()):
                if previous[key].get('state')=='unknown':
                    durations[_reason(previous[key])]+=(at-previous_at).total_seconds()
                old,new=previous[key].get('state'),current[key].get('state')
                if old in ('known','unknown') and new in ('known','unknown') and old!=new:
                    flaps['total']+=1
                    flaps['unknown_to_known' if old=='unknown' else 'known_to_unknown']+=1
        previous,previous_at=current,at
        gap=False
    total=sum(counts.values())
    order_rows=[]
    for key,record in orders.items():
        if gap or key not in previous:
            record.update(coverage_gap=True,right_censored=True)
        first,placed,seen_at=(record[k] for k in ('first_known_at','placed_at','first_seen_at'))
        order_rows.append({**record,
            'latency_seconds':(first-placed).total_seconds() if first and placed and first>=placed else None,
            'placement_time_conflict':bool(first and placed and first<placed),
            'first_seen_proxy_seconds':max(0,(first-seen_at).total_seconds()) if first and placed is None else None})
    for record in order_rows:
        for key in ('first_seen_at','placed_at','first_known_at'):
            record[key]=record[key].isoformat() if record[key] else None
    comparisons=[]
    for placement in placements.values():
        estimated,observed=_share(placement.get('estimated_share')),placement['observed_share']
        available=estimated is not None and observed is not None
        comparisons.append(dict(
            **{k:placement.get(k) for k in ('account_id','condition_id','token_id','order_id',
                'request_id','capital_usd','yield_pct_per_hour')},
            available=available,estimated_share=str(estimated) if estimated is not None else None,
            observed_share=str(observed) if observed is not None else None,
            difference_fraction=str(observed-estimated) if available else None,
            ratio=str(observed/estimated) if available and estimated>0 else None,
            units='share fractions; .01 difference = one percentage point'))
    return dict(version=1,coverage=dict(coverage),flaps=flaps,
        placement_comparisons=comparisons,
        placement_summary=dict(unavailable_attempts=unavailable_placements,
            accepted_orders=len(placements),matched_orders=sum(r['available'] for r in comparisons)),
        no_decision=dict(rounds=no_decision,cause_counts=dict(causes),
            cause_shares={reason:count/no_decision for reason,count in causes.items()},
            denominator='unique NO_DECISION rounds; causes may overlap'),
        reader=dict(retry_cache_hits=retry_hits,capacity_events=capacity_events,
            http_status_counts=dict(http_status),error_class_counts=dict(error_classes),
            inflight_max_age_seconds=inflight_max,retry_remaining_max_seconds=retry_max),
        first_known=dict(orders=order_rows,latency_basis='first observed known; polling upper bound'),
        unknown_reasons=dict(counts=dict(counts),
        shares={reason:count/total for reason,count in counts.items()},
        durations_seconds=dict(durations),observations=total,duration_seconds=sum(durations.values())))


STATE_PATH='/api/prediction-arbitrage/lp/auto/state'
DASHBOARD_PATH='/api/prediction-arbitrage/lp/dashboard'


def build_parser():
    import argparse
    parser=argparse.ArgumentParser(description=(
        'GET-only LP yield baseline and saved JSONL analysis. Placement estimates '
        'and yield_rounds are in-memory and lost on restart; ring resets and '
        'missing observations are coverage gaps, never zero events.'),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    def positive(value):
        try:
            number=float(value)
        except ValueError as exc:
            raise argparse.ArgumentTypeError('must be a finite positive number') from exc
        if not math.isfinite(number) or number<=0:
            raise argparse.ArgumentTypeError('must be a finite positive number')
        return number
    parser.add_argument('--url',help='explicit HTTP(S) service origin to poll; omitted for offline analysis')
    parser.add_argument('--input',action='append',default=[],help='saved JSONL file (repeatable; - for stdin)')
    parser.add_argument('--duration',type=positive,default=900,help='poll duration in seconds')
    parser.add_argument('--interval',type=positive,default=10,help='seconds between GET samples')
    parser.add_argument('--timeout',type=positive,default=5,help='per-GET timeout in seconds')
    parser.add_argument('--dashboard',action='store_true',help='also GET the LP dashboard')
    parser.add_argument('--samples',help='append polled envelopes to this JSONL file')
    parser.add_argument('--json',dest='json_path',help='write JSON report here instead of stdout')
    return parser


def _origin(value):
    from urllib.parse import urlsplit
    parsed=urlsplit(value)
    parsed.port  # Validate a supplied port before any transport is invoked.
    if (parsed.scheme not in ('http','https') or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment or parsed.path not in ('','/')):
        raise ValueError('expected HTTP(S) origin without credentials, query or path')
    return value.rstrip('/')


def get_json(request,*,timeout):
    """The transport boundary rejects mutations, other paths and redirects."""
    from urllib.parse import urlsplit
    from urllib.request import HTTPRedirectHandler, build_opener
    parsed=urlsplit(request.full_url)
    if (request.get_method()!='GET' or request.data is not None
            or parsed.path not in (STATE_PATH,DASHBOARD_PATH) or parsed.query or parsed.fragment):
        raise ValueError('only allowed LP GET endpoints may be sampled')
    _origin(parsed.scheme+'://'+parsed.netloc)
    if _number(timeout) is None or timeout<=0:
        raise ValueError('finite positive timeout required')
    class NoRedirect(HTTPRedirectHandler):
        def redirect_request(self,*args,**kwargs):
            return None
    with build_opener(NoRedirect()).open(request,timeout=timeout) as response:
        raw=response.read(4*1024*1024+1)
        if len(raw)>4*1024*1024:
            raise ValueError('response exceeds sample size limit')
    def nonfinite(value):
        raise ValueError('nonfinite JSON')
    value=json.loads(raw,parse_constant=nonfinite)
    if not isinstance(value,Mapping):
        raise ValueError('expected JSON object')
    return value


def _poll(args,*,transport,clock,sleep,utc_now,sink):
    from urllib.request import Request
    deadline=clock()+args.duration
    endpoints=((STATE_PATH,'state'),)
    if args.dashboard:
        endpoints+=((DASHBOARD_PATH,'dashboard'),)
    while clock()<deadline:
        envelope=dict(sampled_at=utc_now().isoformat())
        for path,key in endpoints:
            try:
                value=transport(Request(args.url+path,method='GET',headers={'Accept':'application/json'}),timeout=args.timeout)
                if not isinstance(value,Mapping):
                    raise ValueError('expected JSON object')
                envelope[key]=value
            except Exception as exc:
                envelope['error' if key=='state' else 'dashboard_error']=dict(error_type=type(exc).__name__[:64])
        if sink is not None:
            sink.write(json.dumps(envelope,ensure_ascii=False,allow_nan=False)+'\n')
            sink.flush()
        yield envelope
        delay=min(args.interval,max(0,deadline-clock()))
        if delay>0:
            sleep(delay)


def main(argv=None,*,transport=None,clock=None,sleep=None,utc_now=None,stdout=None,stderr=None):
    from contextlib import ExitStack
    from itertools import chain
    import sys
    import time
    parser=build_parser()
    args=parser.parse_args(argv)
    if not args.url and not args.input:
        parser.error('provide --url to poll or --input for offline analysis')
    if args.dashboard and not args.url:
        parser.error('--dashboard requires --url')
    if args.url:
        try:
            args.url=_origin(args.url)
        except ValueError:
            parser.error('expected HTTP(S) origin without credentials, query or path')
    stdout=stdout if stdout is not None else sys.stdout
    stderr=stderr if stderr is not None else sys.stderr
    with ExitStack() as stack:
        streams=[sys.stdin if path=='-' else stack.enter_context(open(path,encoding='utf-8')) for path in args.input]
        sink=stack.enter_context(open(args.samples,'a',encoding='utf-8')) if args.samples and args.url else None
        polled=_poll(args,transport=transport or get_json,clock=clock or time.monotonic,
            sleep=sleep or time.sleep,utc_now=utc_now or (lambda:datetime.now(UTC)),sink=sink) if args.url else ()
        report=summarize_samples(chain(chain.from_iterable(streams),polled))
        target=stack.enter_context(open(args.json_path,'w',encoding='utf-8')) if args.json_path else stdout
        json.dump(report,target,ensure_ascii=False,allow_nan=False,indent=2)
        target.write('\n')
    stderr.write('Samples: {samples}; UNKNOWN observations: {unknown}; NO_DECISION rounds: {no_decision}; coverage gaps: {gaps}.\n'.format(
        samples=report['coverage']['usable_samples'],unknown=report['unknown_reasons']['observations'],
        no_decision=report['no_decision']['rounds'],gaps=report['coverage']['unmeasured_intervals']))
    return 0


if __name__=='__main__':
    raise SystemExit(main())
