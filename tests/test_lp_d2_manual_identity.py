"""Inactive reliable manual command identity through existing SQLite tables only."""
from datetime import UTC, datetime, timedelta
from decimal import Decimal
import json
import sqlite3
from threading import Barrier, Event, Thread, current_thread

import pytest

from open_trader.prediction_arbitrage_store import PredictionArbitrageStore

NOW = datetime(2026, 10, 16, 1, tzinfo=UTC)
ACCOUNT = '0x' + 'a'*40


def command(**changes):
    return dict(kind='entry', account_id=ACCOUNT, process_epoch='manual-process-1', request_id='R1',
        session_id='G1', idempotency_key='raw-key', market_id='market-1', condition_id='condition-1',
        token_id='token-1', outcome='YES', price='0.29', quantity='20',
        review_at=(NOW+timedelta(minutes=10)).isoformat(), expiration=int((NOW+timedelta(minutes=11)).timestamp()),
        order_type='GTD', post_only=True) | changes


def row_counts(store):
    with sqlite3.connect(store.path) as tx:
        return tuple(tx.execute('SELECT count(*) FROM '+table).fetchone()[0] for table in ('lp_sessions','lp_actions'))


def legacy(store, sid, key, *, state='entry_open', **extra):
    values = {k:v for k,v in command().items() if k not in {'kind','session_id','idempotency_key','process_epoch','request_id','expiration','order_type','post_only'}}
    values.update(entry_expiration=command()['expiration'], entry_order_id='legacy-entry-'+sid,
        augment_order_ids=['legacy-augment-'+sid], buy_cost='99', fees='4', residual_quantity='300',
        stop_loss_latched=True)
    values.update(extra)
    return store.lp_create_session(sid,key,state=state,payload=values)


def test_manual_entry_identity_claim_replays_across_owners(tmp_path, monkeypatch):
    first, second = PredictionArbitrageStore(tmp_path), PredictionArbitrageStore(tmp_path)
    assert first.lp_manual_command(ACCOUNT, 'raw-key') is None
    gate, results, errors = Barrier(2), [], []
    real_connect = sqlite3.connect
    class SimultaneousClaims(sqlite3.Connection):
        def execute(self, sql, *a, **kw):
            if sql == 'BEGIN IMMEDIATE' and current_thread().name.startswith('manual-claim-'):
                gate.wait(timeout=3)
            return super().execute(sql,*a,**kw)
    monkeypatch.setattr(sqlite3,'connect',lambda *a,**kw:real_connect(*a,**(kw|dict(factory=SimultaneousClaims))))
    def claim(store):
        try: results.append(store.lp_claim_manual_command(command()))
        except BaseException as exc: errors.append(exc)
    threads=[Thread(target=claim,args=(store,),name='manual-claim-'+str(i)) for i,store in enumerate((first,second))]
    for t in threads: t.start()
    for t in threads: t.join(4)
    assert all(not t.is_alive() for t in threads) and errors == []
    assert sorted(r['claimed'] for r in results) == [False, True]
    assert all(r['command']==command() and r['role']=='manual_identity' and r['outcome']=='unknown' for r in results)
    assert all(r['response']==dict(state='needs_attention',session_id='G1',submit_status='unknown') for r in results)
    assert row_counts(first)==(1,0)
    rebuilt=PredictionArbitrageStore(tmp_path)
    replay=rebuilt.lp_claim_manual_command(command(kind='start',process_epoch='new-process',request_id='R-new',
        session_id='G-new',price='.27',quantity='99'))
    assert replay['claimed'] is False and replay['command']==command() and replay['outcome']=='unknown'
    assert rebuilt.lp_manual_command('  '+ACCOUNT.upper()+'  ','raw-key')==replay
    replay['command']['quantity']='999'
    replay['response']['state']='complete'
    assert rebuilt.lp_manual_command(ACCOUNT,'raw-key')['command']==command()
    with pytest.raises(ValueError): rebuilt.lp_manual_command('other-account','raw-key')
    for i,changes in enumerate((dict(price=True),dict(quantity='NaN'),dict(quantity='0'),dict(expiration=True),
        dict(post_only=1),dict(review_at='2026-10-16T01:00:00'),dict(process_epoch=''),
        dict(order_type='GTC'),dict(outcome='invalid'),dict(d2_audit_only=True),dict(account_id=''))):
        with pytest.raises(ValueError): rebuilt.lp_claim_manual_command(command(idempotency_key='bad-'+str(i),**changes))
    assert row_counts(rebuilt)==(1,0)
    for state in ('entry_open','entry_rejected','needs_attention'):
        original=legacy(rebuilt,'legacy-'+state,'legacy-key-'+state,state=state,token_id='legacy-'+state)
        result=rebuilt.lp_claim_manual_command(command(idempotency_key='legacy-key-'+state))
        assert result['claimed'] is False and result['role']=='legacy_response'
        assert result['response']==original and result['command']['session_id']==original['session_id']
    for sid,extra in (('foreign',dict(account_id='foreign')),('missing',dict(account_id=None)),
                      ('audit',dict(d2_audit_only=True))):
        legacy(rebuilt,sid,'key-'+sid,state='complete',**extra)
        with pytest.raises(ValueError): rebuilt.lp_claim_manual_command(command(idempotency_key='key-'+sid))
    with sqlite3.connect(rebuilt.path) as tx:
        tx.execute("INSERT INTO n_leg_controls(singleton,mode,breaker_open,active_batch_id,total_unsettled_capital_units,updated_at) VALUES(1,'MANUAL',0,'active',0,'2026-10-16T01:00:00+00:00') ON CONFLICT(singleton) DO UPDATE SET active_batch_id='active'")
    with pytest.raises(ValueError,match='active_n_leg_batch'):
        rebuilt.lp_claim_manual_command(command(session_id='blocked',idempotency_key='blocked'))
    assert rebuilt.lp_manual_command(ACCOUNT,'blocked') is None
    assert rebuilt.lp_trade_generation()==0


def test_manual_preview_consumption_is_atomic_with_identity_claim(tmp_path, monkeypatch):
    first, second=PredictionArbitrageStore(tmp_path),PredictionArbitrageStore(tmp_path)
    payload={k:v for k,v in command().items() if k in {'market_id','condition_id','token_id','outcome','price','quantity','review_at'}}
    preview=first.create_preview(payload,created_at=NOW.isoformat(),expires_at=(NOW+timedelta(seconds=30)).isoformat())
    assert datetime.fromisoformat(first.lp_preview(preview)['expires_at'])==NOW+timedelta(seconds=10)
    gate,results,errors=Barrier(2),[],[]
    # Race the two public invocations before admission. Two native BEGINs
    # cannot rendezvous inside the single-writer permit.
    def claim(store,index):
        try:
            gate.wait(3)
            results.append(store.lp_claim_manual_command(command(kind='start',session_id='G'+str(index),
            request_id='R'+str(index),idempotency_key='key'+str(index)),preview_id=preview,now=NOW+timedelta(seconds=9)))
        except BaseException as exc: errors.append(exc)
    threads=[Thread(target=claim,args=(s,i),name='manual-preview-'+str(i)) for i,s in enumerate((first,second))]
    for t in threads:t.start()
    for t in threads:t.join(4)
    assert all(not t.is_alive() for t in threads)
    assert len(results)==1 and results[0]['claimed'] is True and len(errors)==1 and isinstance(errors[0],ValueError)
    assert row_counts(first)==(1,0) and first.lp_preview(preview)['consumed_at'] is not None
    winner=results[0]['command']
    replay=second.lp_claim_manual_command(winner|dict(price='.28'),preview_id='missing',now=NOW+timedelta(days=1))
    assert replay['claimed'] is False and replay['command']==winner
    for i,(pid,when) in enumerate(((preview,NOW+timedelta(seconds=9)),('missing',NOW))):
        with pytest.raises(ValueError): first.lp_claim_manual_command(command(session_id='invalid'+str(i),
            idempotency_key='invalid'+str(i)),preview_id=pid,now=when)
    for i,(extra,changes,when) in enumerate((({}, {}, NOW+timedelta(seconds=10)),
        ({'session_id':'wrong-group'}, {}, NOW), ({'token_id':'other'}, {}, NOW),
        ({}, {'quantity':'21'}, NOW), ({'account_id':'foreign'}, {}, NOW))):
        pid=first.create_preview(payload|extra,created_at=NOW.isoformat(),expires_at=(NOW+timedelta(seconds=10)).isoformat())
        with pytest.raises(ValueError): first.lp_claim_manual_command(command(session_id='bad'+str(i),
            idempotency_key='bad'+str(i),**changes),preview_id=pid,now=when)
        assert first.lp_preview(pid)['consumed_at'] is None
    assert row_counts(first)==(1,0)
    pid=first.create_preview(payload,created_at=NOW.isoformat(),expires_at=(NOW+timedelta(seconds=10)).isoformat())
    armed=[True]
    class CommitFailure(sqlite3.Connection):
        def execute(self,sql,*a,**kw):
            if sql=='COMMIT' and armed[0]:
                armed[0]=False
                raise sqlite3.OperationalError('actual preview claim commit failure')
            return super().execute(sql,*a,**kw)
    real_connect = sqlite3.connect
    monkeypatch.setattr(sqlite3,'connect',lambda *a,**kw:real_connect(*a,**(kw|dict(factory=CommitFailure))))
    with pytest.raises(sqlite3.OperationalError,match='commit failure'):
        first.lp_claim_manual_command(command(idempotency_key='rollback',session_id='rollback'),preview_id=pid,now=NOW)
    assert first.lp_preview(pid)['consumed_at'] is None and first.lp_manual_command(ACCOUNT,'rollback') is None
    assert row_counts(first)==(1,0) and first.lp_trade_generation()==0
    # The original public consume API still uses its existing one-use CAS.
    first.consume_lp_preview(pid)
    with pytest.raises(ValueError,match='preview_consumed'):first.consume_lp_preview(pid)


def test_manual_named_augment_identity_keeps_group_deadline_and_namespace(tmp_path):
    store=PredictionArbitrageStore(tmp_path)
    store.lp_claim_manual_command(command())
    assert store.lp_manual_group(ACCOUNT,'G1')['identity']==command()
    old=legacy(store,'old-group','old-key',token_id='old-token')
    with sqlite3.connect(store.path) as tx:
        parent_before=tx.execute('SELECT * FROM lp_sessions WHERE session_id=?',('old-group',)).fetchone()
    for sid,token in (('G1','token-1'),('old-group','old-token')):
        addition=command(kind='augment',session_id=sid,token_id=token,idempotency_key='same-augment-key',
            request_id='augment-'+sid,price='0.28')
        claimed=store.lp_claim_manual_command(addition)
        assert claimed['claimed'] is True and claimed['command']==addition
        assert claimed['response']==dict(state='needs_attention',session_id=sid,reason='augment_submit_unknown')
        rebuilt=PredictionArbitrageStore(tmp_path)
        replay=rebuilt.lp_claim_manual_command(addition|dict(price='0.27',quantity='88',request_id='new-attempt'))
        assert replay['claimed'] is False and replay['command']==addition
        assert rebuilt.lp_manual_command(ACCOUNT,'same-augment-key',session_id=sid)==replay
        action,=store.lp_actions(sid)
        assert action['action_key']==sid+':augment-submit:same-augment-key' and action['state']=='unknown'
    assert row_counts(store)==(2,2)
    with sqlite3.connect(store.path) as tx:
        assert tx.execute('SELECT * FROM lp_sessions WHERE session_id=?',('old-group',)).fetchone()==parent_before
    group=store.lp_manual_group(ACCOUNT,'old-group')
    assert group['identity']['review_at']==old['review_at'] and group['identity']['expiration']==old['entry_expiration']
    assert group['entry_order_ids']==['legacy-entry-old-group'] and group['augment_order_ids']==['legacy-augment-old-group']
    assert set(group)=={'role','identity','entry_order_ids','augment_order_ids'}
    assert not {'buy_cost','fees','residual_quantity','stop_loss_latched','state'} & group['identity'].keys()
    for i,changes in enumerate((dict(account_id='foreign'),dict(session_id='missing'),dict(condition_id='wrong'),
        dict(token_id='wrong'),dict(market_id='wrong'),dict(review_at=(NOW+timedelta(hours=1)).isoformat()),
        dict(expiration=command()['expiration']+60))):
        with pytest.raises(ValueError):store.lp_claim_manual_command(command(kind='augment',request_id='invalid-'+str(i),
            idempotency_key='invalid-'+str(i),**changes))
    legacy(store,'automatic','auto-key',state='complete')
    with sqlite3.connect(store.path) as tx:
        tx.execute('INSERT INTO lp_auto_pool(singleton,payload) VALUES(1,?)',
            (json.dumps(dict(intents={'actual-intent':dict(session_id='automatic')})),))
    with pytest.raises(ValueError):store.lp_claim_manual_command(command(kind='augment',session_id='automatic'))
    for sid,extra in (('missing-deadline',dict(review_at=None)),('missing-identity',dict(condition_id=None)),
        ('audit-root',dict(d2_audit_only=True))):
        legacy(store,sid,'key-'+sid,state='complete',**extra)
        with pytest.raises(ValueError):store.lp_manual_group(ACCOUNT,sid)
        with pytest.raises(ValueError):store.lp_claim_manual_command(command(kind='augment',session_id=sid))
    addition=command(kind='augment',session_id='G1',request_id='preview-augment',idempotency_key='preview-augment',price='0.28')
    payload={k:v for k,v in addition.items() if k in {'session_id','market_id','condition_id','token_id','outcome','price','quantity','review_at'}}
    bad=store.create_preview(payload|dict(session_id='old-group'),created_at=NOW.isoformat(),expires_at=(NOW+timedelta(seconds=10)).isoformat())
    with pytest.raises(ValueError):store.lp_claim_manual_command(addition,preview_id=bad,now=NOW)
    assert store.lp_preview(bad)['consumed_at'] is None
    good=store.create_preview(payload,created_at=NOW.isoformat(),expires_at=(NOW+timedelta(seconds=10)).isoformat())
    assert store.lp_claim_manual_command(addition,preview_id=good,now=NOW)['claimed'] is True
    assert store.lp_preview(good)['consumed_at'] is not None
    assert store.lp_trade_generation()==0


def test_manual_result_recording_preserves_exact_identity_and_unknown(tmp_path, monkeypatch):
    store=PredictionArbitrageStore(tmp_path)
    original=command()
    store.lp_claim_manual_command(original)
    unknown=dict(state='needs_attention',session_id='G1',submit_status='unknown',error='actual post timeout')
    assert store.lp_finish_manual_command(original,outcome='unknown',response=unknown)=='applied'
    assert store.lp_finish_manual_command(original,outcome='unknown',response=unknown)=='replay'
    assert store.lp_finish_manual_command(original,outcome='not_sent',response={'state':'rejected'})=='rejected'
    lookup=PredictionArbitrageStore(tmp_path).lp_manual_command(ACCOUNT,'raw-key')
    assert lookup['outcome']=='unknown' and lookup['response']==unknown and lookup['claimed'] is False
    accepted=dict(state='entry_open',session_id='G1',entry_order_id='actual-entry',buy_cost='999',fees='99',stop_loss_latched=True)
    assert store.lp_finish_manual_command(original,outcome='accepted',response=accepted,venue_order_id='actual-entry')=='applied'
    assert store.lp_finish_manual_command(original,outcome='accepted',response=accepted,venue_order_id='actual-entry')=='replay'
    assert store.lp_manual_command(ACCOUNT,'raw-key')['response']==accepted
    group=store.lp_manual_group(ACCOUNT,'G1')
    assert group['entry_order_ids']==['actual-entry'] and group['identity']==original
    assert set(group)=={'role','identity','entry_order_ids','augment_order_ids'}
    for changes in (dict(account_id='foreign'),dict(process_epoch='other'),dict(request_id='other'),
        dict(session_id='other'),dict(idempotency_key='missing'),dict(price='.28'),dict(quantity='21'),
        dict(review_at=(NOW+timedelta(hours=1)).isoformat()),dict(expiration=original['expiration']+60)):
        assert store.lp_finish_manual_command(original|changes,outcome='accepted',response=accepted,
            venue_order_id='actual-entry')=='rejected'
    for outcome,response,oid in (('unknown',unknown,None),('rejected',{'state':'rejected'},'actual-entry'),
        ('accepted',accepted,'different-venue'),('accepted',accepted|dict(fees='0'),'actual-entry')):
        assert store.lp_finish_manual_command(original,outcome=outcome,response=response,venue_order_id=oid)=='rejected'
    addition=command(kind='augment',request_id='augment-R',idempotency_key='augment-key',price='0.28')
    store.lp_claim_manual_command(addition)
    augmented=dict(state='entry_open',session_id='G1',augment_order_id='actual-augment')
    assert store.lp_finish_manual_command(addition,outcome='unknown',response={'state':'needs_attention'},
        venue_order_id='actual-augment')=='applied'
    assert store.lp_finish_manual_command(addition,outcome='accepted',response=augmented,
        venue_order_id='other-augment')=='rejected'
    assert store.lp_finish_manual_command(addition,outcome='accepted',response=augmented,
        venue_order_id='actual-augment')=='applied'
    rebuilt=PredictionArbitrageStore(tmp_path)
    assert rebuilt.lp_manual_command(ACCOUNT,'augment-key',session_id='G1')['response']==augmented
    assert rebuilt.lp_manual_group(ACCOUNT,'G1')['augment_order_ids']==['actual-augment']
    for i,outcome in enumerate(('not_sent','rejected')):
        c=command(session_id='known-'+str(i),request_id='known-R'+str(i),idempotency_key='known-'+str(i))
        store.lp_claim_manual_command(c)
        response=dict(state='rejected',reason='actual-'+outcome)
        assert store.lp_finish_manual_command(c,outcome=outcome,response=response)=='applied'
        assert store.lp_finish_manual_command(c,outcome=outcome,response=response)=='replay'
        assert store.lp_finish_manual_command(c,outcome='accepted',response=accepted,venue_order_id='new-venue')=='rejected'
    legacy(store,'legacy-parent','legacy-key',state='complete')
    store.lp_upsert_action('legacy-parent','legacy-parent:augment-submit:legacy-add',state='unknown',
        payload=dict(role='augment',side='BUY',token_id='token-1',price='.28',quantity='20'))
    legacy(store,'audit-parent','audit-key',state='complete',d2_audit_only=True)
    with sqlite3.connect(store.path) as tx:
        before=tx.execute('SELECT * FROM lp_sessions ORDER BY session_id').fetchall(),tx.execute('SELECT * FROM lp_actions ORDER BY action_id').fetchall()
    for c in (command(session_id='orphan',idempotency_key='orphan'),command(session_id='legacy-parent',idempotency_key='legacy-key'),
        command(kind='augment',session_id='legacy-parent',idempotency_key='legacy-add'),
        command(session_id='audit-parent',idempotency_key='audit-key')):
        assert store.lp_finish_manual_command(c,outcome='accepted',response=accepted,venue_order_id='borrowed')=='rejected'
    with sqlite3.connect(store.path) as tx:
        assert before==(tx.execute('SELECT * FROM lp_sessions ORDER BY session_id').fetchall(),tx.execute('SELECT * FROM lp_actions ORDER BY action_id').fetchall())
    c=command(session_id='commit-failure',request_id='commit-R',idempotency_key='commit-failure')
    store.lp_claim_manual_command(c)
    original_unknown=store.lp_manual_command(ACCOUNT,c['idempotency_key'])
    generation=store.lp_trade_generation()
    armed=[True];real_connect=sqlite3.connect
    class FailedFinish(sqlite3.Connection):
        def execute(self,sql,*a,**kw):
            if sql=='COMMIT' and armed[0]:
                armed[0]=False
                raise sqlite3.OperationalError('actual reliable finish COMMIT failure')
            return super().execute(sql,*a,**kw)
    monkeypatch.setattr(sqlite3,'connect',lambda *a,**kw:real_connect(*a,**(kw|dict(factory=FailedFinish))))
    with pytest.raises(sqlite3.OperationalError,match='finish COMMIT failure'):
        store.lp_finish_manual_command(c,outcome='accepted',response={'state':'entry_open'},venue_order_id='failed-ID')
    assert store.lp_manual_command(ACCOUNT,c['idempotency_key'])==original_unknown
    assert store.lp_claim_manual_command(c)['claimed'] is False
    assert store.lp_manual_group(ACCOUNT,c['session_id'])['entry_order_ids']==[]
    with pytest.raises(ValueError):store.lp_finish_manual_command(c,outcome='unknown',response={'not_json':Decimal('1')})
    assert store.lp_trade_generation()==generation


def test_manual_identity_containers_do_not_enter_legacy_financial_views(tmp_path):
    from tests.test_lp_business_writer_contract import request
    from open_trader.prediction_arbitrage_store import LP_RESERVED_MANUAL_SESSION_ID
    from tests.test_lp_order_registration_contract import (
        _runtime, _SDKPublicClient, _open_order, WALLET, CONDITION_ID, TOKEN_ID, NOW as API_NOW,
    )
    store=PredictionArbitrageStore(tmp_path/'views')
    for sid,state,extra in (('real-complete','complete',{}),('real-active','entry_open',{}),
        (LP_RESERVED_MANUAL_SESSION_ID,'complete',{}),('numeric-marker','complete',dict(d2_manual_identity_only=1)),
        ('text-marker','complete',dict(d2_manual_identity_only='true')),
        ('false-marker','complete',dict(d2_manual_identity_only=False))):
        legacy(store,sid,'key-'+sid,state=state,**extra)
    with sqlite3.connect(store.path) as tx:
        tx.execute("UPDATE lp_sessions SET created_at='2026-10-15T00:00:00+00:00'")
        schema=tx.execute("SELECT type,name,sql FROM sqlite_master WHERE type IN ('table','index') ORDER BY type,name").fetchall()
    def views():
        return (store.lp_latest_session(),store.lp_sessions(),store.lp_active_session(),store.lp_active_sessions(),
            store.lp_active_sessions_with_revisions())
    before=views()
    assert {s['session_id'] for s in before[1]}=={'real-complete','real-active','manual','numeric-marker','text-marker','false-marker'}
    assert before[0]['session_id']!='manual' and before[2]['session_id']=='real-active'
    generation=store.lp_trade_generation();notifications=[];store._lp_trade_change_listener=notifications.append
    claimed=store.lp_claim_manual_command(command())
    assert claimed['claimed'] is True and claimed['response']['state']=='needs_attention'
    assert views()==before, 'strict manual identity-only container is not a legacy business session'
    root=store.lp_session('G1')
    assert root['state']=='complete' and root['d2_manual_identity_only'] is True and 'd2_audit_only' not in root
    assert store.lp_log_business_record(request('collision',session_id='G1',account_id=ACCOUNT,
        process_epoch='manual-process-1',condition_id='condition-1',token_id='token-1')) is False
    assert store.lp_session('G1')==root and store.lp_actions('G1')==[]
    assert store.lp_log_business_record(request('ordinary',session_id='d2:ordinary',account_id=ACCOUNT)) is True
    assert store.lp_session('d2:ordinary')['d2_audit_only'] is True and views()==before
    assert store.lp_trade_generation()==generation and notifications==[]
    store.lp_recover_interrupted_submissions()
    assert store.lp_session('G1')==root and views()==before
    with sqlite3.connect(store.path) as tx:
        assert schema==tx.execute("SELECT type,name,sql FROM sqlite_master WHERE type IN ('table','index') ORDER BY type,name").fetchall()
    entered,release,completed=Event(),Event(),Event()
    class GatedBooks(_SDKPublicClient):
        def get_order_books(self,*,token_ids):
            entered.set()
            assert release.wait(3), 'manual identity registration book release watchdog'
            return super().get_order_books(token_ids=token_ids)
    reg,adapter,account,lp,execution=_runtime(tmp_path/'registration',
        orders=(_open_order('real-current-buy','BUY',price='.40',original='10'),),public_client=GatedBooks(API_NOW))
    results,errors=[],[]
    snapshot=adapter.lp_account_snapshot_shared(max_age_seconds=0,trade_generation_provider=reg.lp_trade_generation)
    assert snapshot['account_id']==WALLET
    assert all(snapshot[k] is True for k in ('balance_complete','open_orders_complete','trades_complete','positions_complete','pagination_complete'))
    generation=reg.lp_trade_generation()
    def register():
        try:results.append(lp.register_account_snapshot(snapshot))
        except BaseException as exc:errors.append(exc)
        finally:completed.set()
    worker=Thread(target=register);worker.start()
    try:
        assert entered.wait(2) and not completed.is_set()
        current=command(account_id=WALLET,session_id='manual-during-registration',idempotency_key='manual-during-registration',
            condition_id=CONDITION_ID,token_id=TOKEN_ID)
        assert reg.lp_claim_manual_command(current)['claimed'] is True
        display=dict(state='entry_open',buy_cost='99999',fees='888',stop_loss_latched=True)
        assert reg.lp_finish_manual_command(current,outcome='accepted',response=display,venue_order_id='old-response-only')=='applied'
        addition=current|dict(kind='augment',request_id='manual-augment-R',idempotency_key='manual-augment',price='0.28')
        reg.lp_claim_manual_command(addition)
        manual_before=reg.lp_session(current['session_id'])
        assert reg.lp_trade_generation()==generation
        release.set()
        assert completed.wait(3), 'public account registration completion watchdog'
        assert errors==[] and results[0]['state']=='registered',results or errors
        dashboard=execution.refresh_lp_dashboard_snapshot()
        assert dashboard['state']=='ready'
        assert {o['order_id'] for o in dashboard['orders']}=={'real-current-buy'}
        real,=reg.lp_sessions()
        assert real['session_id']!=current['session_id'] and real['owned_order_ids']==['real-current-buy']
        assert real['account_id']==WALLET and real['condition_id']==CONDITION_ID and real['token_id']==TOKEN_ID
        reg.lp_recover_interrupted_submissions()
        assert reg.lp_session(current['session_id'])==manual_before and reg.lp_trade_generation()==generation
        with sqlite3.connect(reg.path) as tx:
            document=json.loads(tx.execute('SELECT payload FROM lp_auto_pool').fetchone()[0])
        facts=document['account_financial_facts']
        assert facts['financial_status']=='known' and facts['pending_buy_actions']==[] and facts['retained_reservation_ids']==[]
        assert len(facts['buys'])==1 and facts['buys'][0]['order_id']=='real-current-buy'
        assert Decimal(str(facts['buys'][0]['quantity']))==10 and Decimal(str(facts['buys'][0]['price']))==Decimal('.40')
        assert account.posts==account.cancels==[]
        stale=adapter.lp_account_snapshot_shared(max_age_seconds=0,trade_generation_provider=reg.lp_trade_generation)
        reg.lp_register_trade_change(real['session_id'])
        invalid=lp.register_account_snapshot(stale)
        assert invalid['state']=='skipped' and invalid['reason']=='account_round_invalid'
        assert reg.lp_trade_generation()==generation+1 and reg.lp_session(current['session_id'])==manual_before
    finally:
        release.set();worker.join(3)
        assert not worker.is_alive()
        adapter.close()
