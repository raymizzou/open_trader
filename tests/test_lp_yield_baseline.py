"""Hand-counted JSONL baseline cases; all transport boundaries are offline."""
from copy import deepcopy
from datetime import UTC,datetime,timedelta
from decimal import Decimal
import io
import json

import pytest

from scripts import lp_yield_baseline as baseline

NOW=datetime(2026,10,19,tzinfo=UTC)


def sample(seconds,state='unknown',reason='capacity',*,order='o1',instance='process-1',placed=0,rows=None,round_id=None):
    stamp=(NOW+timedelta(seconds=seconds)).isoformat()
    row=dict(account_id='A',condition_id='c',token_id='t',order_id=order,state=state,
        yield_pct_per_hour=None if state=='unknown' else '.1',reason_codes=[reason] if state=='unknown' else [],
        accepted_input=True,placed_at=None if placed is None else (NOW+timedelta(seconds=placed)).isoformat())
    round_row=dict(identity=dict(account_id='A',generation='g',observation_id=round_id or 'r'+str(seconds)),
        evaluated_at=stamp,acceptance='accepted',selection_state='DECISION',
        incumbents=dict(total=1,samples=rows if rows is not None else [row],omitted=0),
        source_errors=dict(total=0,samples=[],omitted=0),reader=dict(inflight_count=0,jobs=[]),
        placement_estimates=dict(total=0,samples=[],omitted=0))
    return dict(sampled_at=stamp,state=dict(diagnostics=dict(yield_rounds=dict(instance_id=instance,recent=[round_row]))))


def rounds(value): return value['state']['diagnostics']['yield_rounds']['recent']


def test_summary_reason_counts_and_observed_durations():
    samples=[sample(0),sample(10),sample(20,reason='own_order_depth_unknown'),sample(30,'known')]
    # Snapshots can repeat the same earlier ring rounds without creating events.
    for index in range(1,len(samples)):
        rounds(samples[index])[:0]=deepcopy(rounds(samples[index-1]))
    result=baseline.summarize_samples(json.dumps(v) for v in samples)
    reasons=result['unknown_reasons']
    assert reasons['counts']==dict(capacity=2,own_order_depth_unknown=1)
    assert reasons['shares']==dict(capacity=2/3,own_order_depth_unknown=1/3)
    assert reasons['durations_seconds']==dict(capacity=20,own_order_depth_unknown=10)
    assert reasons['observations']==3 and reasons['duration_seconds']==30
    assert result['coverage']['usable_samples']==4
    repeated=sample(40,'known',round_id='r30')
    rounds(repeated)[:0]=deepcopy(rounds(samples[-1])[:-1])
    after=baseline.summarize_samples([*samples,repeated])
    assert after['unknown_reasons']==reasons


def test_summary_flaps_and_placement_to_first_known():
    values=[sample(0),sample(10),sample(20,'known'),sample(30),sample(40,'known')]
    result=baseline.summarize_samples(values)
    assert result['flaps']==dict(total=3,unknown_to_known=2,known_to_unknown=1)
    order,=result['first_known']['orders']
    assert order['order_id']=='o1' and order['latency_seconds']==20
    assert order['first_known_at']==(NOW+timedelta(seconds=20)).isoformat()
    assert order['left_censored'] is False and order['right_censored'] is False
    proxy=baseline.summarize_samples([sample(0,placed=None),sample(20,'known',placed=None)])['first_known']['orders'][0]
    assert proxy['latency_seconds'] is None and proxy['first_seen_proxy_seconds']==20
    separate=baseline.summarize_samples([*values,sample(50,order='o2',placed=50)])['first_known']['orders']
    assert {r['order_id']:r['latency_seconds'] for r in separate}==dict(o1=20,o2=None)
    assert next(r for r in separate if r['order_id']=='o2')['right_censored'] is True


def test_summary_deduplicates_no_decision_retry_and_inflight_causes():
    values=[sample(t) for t in (0,10,20,30)]
    errors=[dict(source='balance',error_class='capacity',source_error='capacity'),
        dict(source='positions',error_class='exception',source_error='HTTPError',http_status=429,retry_cache_hit=True,remaining_retry_seconds=45),
        dict(source='open_orders',error_class='timeout',source_error='timeout')]
    for index,error in enumerate(errors):
        row=rounds(values[index])[0];row['selection_state']='NO_DECISION'
        row['source_errors']=dict(total=1,samples=[error],omitted=0)
        row['reader']=dict(inflight_count=2,jobs=[dict(age_seconds=25)])
    rounds(values[0])[0]['source_errors']['samples'].append(dict(errors[0],role='position'))
    for index in range(1,len(values)): rounds(values[index])[:0]=deepcopy(rounds(values[index-1]))
    result=baseline.summarize_samples(values)
    assert result['no_decision']['rounds']==3
    assert result['no_decision']['cause_counts']==dict(capacity=1,retry_cache=1,timeout=1)
    assert result['no_decision']['cause_shares']==dict(capacity=1/3,retry_cache=1/3,timeout=1/3)
    reader=result['reader']
    assert reader['retry_cache_hits']==1 and reader['capacity_events']==1
    assert reader['http_status_counts']=={'429':1}
    assert reader['inflight_max_age_seconds']==25 and reader['retry_remaining_max_seconds']==45
    assert reader['error_class_counts']==dict(capacity=1,exception=1,timeout=1)


def test_summary_handles_bad_lines_missing_diagnostics_and_truncation():
    initial=sample(0)
    poll_error=dict(sampled_at=(NOW+timedelta(seconds=5)).isoformat(),error=dict(error_type='TimeoutError'))
    old=dict(sampled_at=(NOW+timedelta(seconds=10)).isoformat(),state={})
    truncated=sample(20,rows=[])
    rounds(truncated)[0]['incumbents'].update(total=1,omitted=1)
    reset=sample(25,instance='process-2')
    rounds(reset).clear()
    recovered=sample(30,'known',instance='process-2')
    invalid_number=sample(40,'known',instance='process-2')
    rounds(invalid_number)[0]['incumbents']['samples'][0]['yield_pct_per_hour']='NaN'
    result=baseline.summarize_samples([initial,'not JSON',poll_error,old,truncated,reset,recovered,
        sample(15,instance='process-2'),deepcopy(recovered),invalid_number])
    coverage=result['coverage']
    assert coverage['invalid_lines']==1 and coverage['poll_errors']==1
    assert coverage['missing_diagnostics']>=2 and coverage['truncated_rows']==1
    assert coverage['ring_resets']==1 and coverage['out_of_order_samples']==1
    assert coverage['duplicate_samples']==1 and coverage['invalid_estimates']==1
    assert coverage['unmeasured_intervals']>0
    assert result['flaps']['total']==0
    assert result['unknown_reasons']['duration_seconds']==0
    assert result['unknown_reasons']['counts']==dict(capacity=1)
    assert json.loads(json.dumps(result,allow_nan=False))==result
    empty=baseline.summarize_samples([])
    assert empty['coverage']['usable_samples']==0 and empty['unknown_reasons']['observations']==0
    # Current twenty-row diagnostics can recover orders outside the small ring
    # sample. Match their exact observation identity; never reuse an old view.
    hidden=sample(0,rows=[])
    rounds(hidden)[0]['incumbents'].update(total=1,omitted=1)
    unknown=deepcopy(rounds(sample(0))[0]['incumbents']['samples'][0])
    unknown.update(reason_observation_id='r0',reason_is_current=True)
    hidden['state']['diagnostics']['unknown_buy_yields']=dict(recent=[unknown])
    known=sample(10,'known',rows=[])
    rounds(known)[0]['incumbents'].update(total=1,omitted=1)
    known['state']['diagnostics']['rotation']=dict(identity=dict(observation_id='r10'),
        incumbent_yields=dict(samples=[dict(kind='incumbent',identity=['A','c','t','o1'],state='known',
            yield_pct_per_hour='.1',observation=dict(placed_at=NOW.isoformat(),reason_codes=[]))]))
    supplemented=baseline.summarize_samples([hidden,known])
    assert supplemented['unknown_reasons']['counts']==dict(capacity=1)
    assert supplemented['first_known']['orders'][0]['latency_seconds']==10
    assert supplemented['flaps']['total']==1
    lost=[sample(0),sample(20,'known')]
    lost[0]['state']['diagnostics']['yield_rounds']['total_observations']=1
    lost[1]['state']['diagnostics']['yield_rounds']['total_observations']=10
    missing_rounds=baseline.summarize_samples(lost)
    assert missing_rounds['coverage']['missed_rounds']==8
    assert missing_rounds['flaps']['total']==0


def test_summary_compares_placement_and_later_share_by_exact_order():
    values=[sample(0),sample(10,'known'),sample(20,'known',order='o2',placed=20)]
    placement=dict(account_id='A',condition_id='c',token_id='t',order_id='o1',request_id='buy-1',
        disposition='accepted',estimated_share='.05',yield_pct_per_hour='.2',capital_usd='8',checked_at=NOW.isoformat(),
        placed_at=NOW.isoformat(),placement_time_basis='accepted_receipt')
    rounds(values[0])[0]['placement_estimates']=dict(total=4,omitted=0,samples=[placement,
        dict(placement,request_id='unknown',disposition='unknown_error',order_id=None),
        dict(placement,request_id='not-sent',disposition='not_sent',order_id='o2'),
        dict(placement,request_id='rejected',disposition='rejected')])
    rounds(values[1])[0]['incumbents']['samples'][0]['estimated_share']='.04'
    rounds(values[2])[0]['incumbents']['samples'][0]['estimated_share']='.99'
    result=baseline.summarize_samples(values)
    row,=result['placement_comparisons']
    assert row['order_id']=='o1' and row['available'] is True
    assert Decimal(row['difference_fraction'])==Decimal('-.01') and Decimal(row['ratio'])==Decimal('.8')
    assert Decimal(row['estimated_share'])==Decimal('.05') and Decimal(row['observed_share'])==Decimal('.04')
    assert row['capital_usd']=='8' and row['yield_pct_per_hour']=='.2'
    assert result['placement_summary']['unavailable_attempts']==3
    missing=deepcopy(values)
    rounds(missing[1])[0]['incumbents']['samples'][0].pop('estimated_share')
    unavailable,=baseline.summarize_samples(missing)['placement_comparisons']
    assert unavailable['available'] is False and unavailable['difference_fraction'] is None
    assert unavailable['observed_share'] is None
    receipt_anchor=deepcopy(values)
    for envelope in receipt_anchor:
        for row in rounds(envelope)[0]['incumbents']['samples']:
            row['placed_at']=None
    anchored=baseline.summarize_samples(receipt_anchor)['first_known']['orders'][0]
    assert anchored['latency_seconds']==10 and anchored['placement_time_basis']=='accepted_receipt'


def test_polling_cli_is_get_only_and_offline_mode_never_connects(tmp_path,capsys):
    defaults=baseline.build_parser().parse_args(['--url','http://offline.invalid'])
    assert defaults.duration==900 and defaults.interval==10 and defaults.timeout>0
    with pytest.raises(SystemExit) as help_exit:
        baseline.main(['--help'])
    assert help_exit.value.code==0
    help_text=capsys.readouterr().out.lower()
    assert 'restart' in help_text and 'in-memory' in help_text
    clock=[0.0];requests=[];output=io.StringIO();text=io.StringIO()
    def transport(request,*,timeout):
        assert request.get_method()=='GET' and request.data is None
        assert timeout==2
        requests.append(request.full_url)
        if request.full_url.endswith('/auto/state'):
            if clock[0]==0: raise TimeoutError('COOKIE_SECRET')
            return sample(int(clock[0]))['state']
        assert request.full_url.endswith('/lp/dashboard')
        return {'state':'ready'}
    def sleep(seconds): clock[0]+=seconds
    saved=tmp_path/'samples.jsonl'
    assert baseline.main(['--url','http://offline.invalid','--duration','3','--interval','1','--timeout','2','--dashboard','--samples',str(saved)],
        transport=transport,clock=lambda:clock[0],sleep=sleep,utc_now=lambda:NOW+timedelta(seconds=clock[0]),stdout=output,stderr=text)==0
    report=json.loads(output.getvalue())
    assert len(requests)==6 and report['coverage']['poll_errors']==1 and report['coverage']['usable_samples']==2
    assert 'COOKIE_SECRET' not in output.getvalue()+text.getvalue()+saved.read_text()
    assert len(saved.read_text().splitlines())==3 and text.getvalue().strip()
    def forbidden(*a,**kw): pytest.fail('offline mode attempted network')
    offline=io.StringIO()
    assert baseline.main(['--input',str(saved)],transport=forbidden,stdout=offline,stderr=io.StringIO())==0
    assert json.loads(offline.getvalue())==report
    for option,value in [('--interval','0'),('--duration','-1'),('--timeout','nan')]:
        with pytest.raises(SystemExit) as invalid:
            baseline.main(['--url','http://offline.invalid',option,value],transport=forbidden)
        assert invalid.value.code==2
    from urllib.request import Request
    with pytest.raises(ValueError):
        baseline.get_json(Request('http://offline.invalid/api/prediction-arbitrage/lp/auto/state',method='POST'),timeout=1)
    with pytest.raises(ValueError):
        baseline.get_json(Request('http://offline.invalid/mutate',method='GET'),timeout=1)
