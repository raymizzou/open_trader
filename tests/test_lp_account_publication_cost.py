"""Account publication keeps one atomic view without unrelated history work."""
import json
from types import SimpleNamespace
from open_trader.polymarket_lp import PolymarketLPService
from open_trader.prediction_arbitrage_store import PredictionArbitrageStore
from tests.test_lp_account_coverage_ledger import NOW,WALLET,build,seed,snapshot,read_pool,order,trade


def test_account_registration_reads_session_catalog_once_for_all_tokens(tmp_path,monkeypatch):
    store=PredictionArbitrageStore(tmp_path)
    fills=[]
    for i in range(12):
        oid,token,condition=f'order-{i}',f'token-{i}',f'condition-{i}'
        history=order(oid,token_id=token,condition_id=condition,status='FILLED',size_matched='3',original_size='3')
        store.lp_create_session(f'session-{i}',f'key-{i}',state='complete',payload={
            'account_id':WALLET,'token_id':token,'condition_id':condition,'entry_order_id':oid,
            'owned_order_ids':[oid],'order_history':{oid:history}})
        fill=trade(f'fill-{i}',oid)
        fill.update(market=condition,asset_id=token)
        fill['maker_orders'][0]['asset_id']=token
        fills.append(fill)
    scans=[]
    connect=store._connection
    def traced():
        c=connect()
        c.set_trace_callback(lambda sql:scans.append(sql) if sql=='SELECT * FROM lp_sessions ORDER BY updated_at,session_id' else None)
        return c
    monkeypatch.setattr(store,'_connection',traced)
    lp=PolymarketLPService(store,SimpleNamespace(config=SimpleNamespace(wallet_address=WALLET)),clock=lambda:NOW)
    scans.clear()
    result=lp.register_account_snapshot(snapshot(raw_trades=fills))
    assert result['state']=='registered',result
    assert len(result['tokens'])==12
    facts=read_pool(store)['account_financial_facts']
    assert facts['financial_status']=='known'
    assert facts['buys']==[] and facts['inventory_cost_usd']=='0'
    assert len(scans)==1


def test_account_publication_does_not_resanitize_unmodified_history(tmp_path,monkeypatch):
    from open_trader import prediction_arbitrage_store as module
    store=PredictionArbitrageStore(tmp_path)
    document=seed(store)
    document.update(rounds={'old':{'state':'unknown','optional':None}},custom_history={'kept':[None,1,'old']})
    with store._transaction() as c:
        c.execute('UPDATE lp_auto_pool SET payload=?',(json.dumps(document),))
    visited=[]
    safe=module._safe_value
    def counted(value,**kwargs):
        if kwargs.get('key') in ('events','rounds','custom_history'):visited.append(kwargs['key'])
        return safe(value,**kwargs)
    monkeypatch.setattr(module,'_safe_value',counted)
    published=store.lp_publish_account_financial_facts(build(),expected_generation=0)
    saved=read_pool(store)
    assert published['financial_status']=='known'
    for key in ('events','rounds','custom_history'):assert saved[key]==document[key]
    assert saved['intents']['intent']['state']=='unknown'
    assert saved['intents']['intent']['reservation_coverage']['state']=='covered'
    assert visited==[]


def test_later_token_sees_new_owner_and_conflict_rolls_back_whole_account(tmp_path):
    store=PredictionArbitrageStore(tmp_path)
    lp=PolymarketLPService(store,SimpleNamespace(config=SimpleNamespace(wallet_address=WALLET)),clock=lambda:NOW)
    orders=[order('duplicate',token_id=f'token-{i}',condition_id=f'condition-{i}',
                  market_id=f'market-{i}',outcome='YES') for i in range(2)]
    result=lp.register_account_snapshot(snapshot(open_orders=orders))
    assert result['state']=='failed' and result['reason']=='order_identity_conflict'
    assert store.lp_sessions()==[]
    with store._read_connection() as c:
        assert c.execute('SELECT payload FROM lp_auto_pool').fetchone() is None
    orders[1]['order_id']='different'
    result=lp.register_account_snapshot(snapshot(open_orders=orders))
    assert result['state']=='registered' and result['created']==2
    assert len(read_pool(store)['account_financial_facts']['buys'])==2
    owners={s['entry_order_id']:s['session_id'] for s in store.lp_sessions()}
    repeated=lp.register_account_snapshot(snapshot(open_orders=orders))
    assert repeated['state']=='registered' and repeated['created']==0
    assert {s['entry_order_id']:s['session_id'] for s in store.lp_sessions()}==owners
