import os
import subprocess
from pathlib import Path
import pytest


def test_prediction_only_browser_boots_without_stock_requests():
    js = Path(__file__).resolve().parents[1] / 'src/open_trader/dashboard_static/dashboard.js'
    script = r'''
const fs = require('fs'), vm = require('vm'), assert = require('assert');
class Element {
  constructor(){ this.dataset={}; this.hidden=false; this.innerHTML=''; this.style={};
    this.classList={toggle(){},add(){},remove(){}}; }
  addEventListener(){} setAttribute(){} removeAttribute(){}
  querySelector(){return null;} querySelectorAll(){return [];}
}
const nodes={}, requests=[];
let boot;
const body = new Element(); body.dataset.predictionOnly='true';
const document={body, addEventListener(name, fn){if(name==='DOMContentLoaded')boot=fn;},
  getElementById(id){return nodes[id] ||= new Element();}, querySelector(){return new Element();}};
const window={location:{search:'',pathname:'/',hash:''},setInterval(){return 1;},clearInterval(){},
  setTimeout(){return 1;},clearTimeout(){}};
const sandbox={document,window,console,URLSearchParams,AbortController,
  fetch:async (url)=>{requests.push(url); return {ok:true,json:async()=>({state:'ready',n_leg:{status:'paused',code:'N_LEG_PAUSED'},venues:[],orders:[],positions:[],recommendations:[]})};}};
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),sandbox);
boot();
setImmediate(()=>{
  assert.equal(vm.runInContext('state.workspaceView',sandbox),'prediction_market');
  vm.runInContext('setWorkspaceView("portfolio")',sandbox);
  assert.equal(vm.runInContext('state.workspaceView',sandbox),'prediction_market');
  assert(requests.length>0);
  assert(requests.every(url=>url.startsWith('/api/prediction-arbitrage/')),JSON.stringify(requests));
  assert.equal(nodes['return-to-portfolio'].hidden,true);
});
'''
    result = subprocess.run(['node', '-e', script, str(js)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(('service_mode','mutations','n_leg','body_prediction_only','expected_state'), [
    ('production','enabled','running','true','production'),
    ('shadow','prohibited','paused','true','shadow-paused'),
    ('shadow','prohibited','running','true','shadow'),
    ('unknown','unknown','running','true','unknown'),
    ('','', 'running','false','legacy'),
])
def test_prediction_ui_uses_real_service_identity_and_blocks_unknown_writes(
    service_mode, mutations, n_leg, body_prediction_only, expected_state,
):
    js = Path(__file__).resolve().parents[1] / 'src/open_trader/dashboard_static/dashboard.js'
    script = r'''
const fs = require('fs'), vm = require('vm'), assert = require('assert');
class Element {
  constructor(){ this.dataset={}; this.hidden=false; this.innerHTML=''; this.style={};
    this.classList={toggle(){},add(){},remove(){}}; }
  addEventListener(){} setAttribute(){} removeAttribute(){}
  querySelector(){return null;} querySelectorAll(){return {forEach(){}};}
}
const nodes={}, requests=[];
let boot;
const body = new Element(); body.dataset.predictionOnly=process.env.BODY_PREDICTION_ONLY;
const document={body, addEventListener(name, fn){if(name==='DOMContentLoaded')boot=fn;},
  getElementById(id){return nodes[id] ||= new Element();}, querySelector(){return new Element();}};
const window={location:{search:'',pathname:'/',hash:''},scrollY:0,
  setInterval(){return 1;},clearInterval(){},setTimeout(){return 1;},clearTimeout(){}};
const sandbox={document,window,console,URLSearchParams,AbortController,
  fetch:async (url,options={})=>{
    requests.push({url,method:options.method||'GET'});
    if (url.endsWith('/venues')) {
      const payload={n_leg:{status:process.env.N_LEG}};
      if (process.env.SERVICE_MODE !== '') {
        payload.mode=process.env.SERVICE_MODE;
        payload.mutations=process.env.SERVICE_MUTATIONS;
      }
      return {ok:true,json:async()=>payload};
    }
    if (url.endsWith('/lp/dashboard')) {
      if (process.env.EXPECTED_STATE === 'shadow-paused') {
        return {ok:false,status:503,json:async()=>({error:'unavailable'})};
      }
      return {ok:true,json:async()=>({
      state:'ready',stale:false,complete:true,
      lp_orders_today:[{order_id:'order-1',market_id:'market-order',
        condition_id:'condition-order',token_id:'order-token',
        market_title:'LP order fact',outcome:'NO',side:'BUY',status:'LIVE',
        price:'0.40',quantity:'20',filled_quantity:'0',remaining_quantity:'20',
        state:'open'}],
        positions:[],market_rewards:[],recommendations:[],
        lp_sessions:[{session_id:'session-1',state:'entry_open',
          condition_id:'condition-order'}],
        auto:{desired_running:true,scheduler_running:true,ever_enabled:true,
          budget_usd:'10',target_buy_count:'1',pause_confirmed:true,
          slots:{occupied:0},config_version:1},
        candidates:[{market_id:'market-candidate',condition_id:'condition-candidate',
          market_title:'LP candidate fact',outcome:'NO',state:'eligible',
          daily_pool_usd:'150',min_quantity:'20',
          selected_direction:{outcome:'NO',price:'0.40',quantity:'20',
            required_capital:'8.00',estimated_exit_loss:'0.80',
            estimated_exit_loss_ratio:'0.10',
            checked_at:'2026-09-29T00:00:00Z'},
          reference_capital:'8.00',
          competition:{value:'12.5',raw_value:'12.5',
            checked_at:'2026-09-29T00:00:00Z',state:'known',stale:false,updated:true},
          reason:[],summary:[]}]})};
    }
    if ((options.method||'GET') === 'POST') return {ok:true,json:async()=>({ok:true})};
    return {ok:false,status:503,json:async()=>({error:'unavailable'})};
  }};
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),sandbox);
boot();
setTimeout(async()=>{
  await new Promise(resolve=>setImmediate(resolve));
  if (process.env.BODY_PREDICTION_ONLY !== 'true') {
    vm.runInContext('setWorkspaceView("prediction_market");',sandbox);
    await new Promise(resolve=>setImmediate(resolve));
  }
  vm.runInContext('renderPredictionMarket();',sandbox);
  const html=nodes['prediction-market-root'].innerHTML;
  const statusCount=(html.match(/data-(shadow-status|service-identity-status)/g)||[]).length;
  const lpCount=(html.match(/data-lp-realtime-status/g)||[]).length;
  if (process.env.EXPECTED_STATE === 'shadow-paused') {
    assert.equal(statusCount,1,html);
    assert.equal((html.match(/>Shadow 只读</g)||[]).length,1,html);
    assert.equal(lpCount,1,html);
    assert.match(html,/data-lp-realtime-status>LP 实时数据暂不可用</);
    assert.match(html,/data-lp-realtime-note>这不是生产正常交易看板；写操作不可用。</);
  } else if (process.env.EXPECTED_STATE === 'shadow') {
    assert.equal(statusCount,1,html);
    assert.match(html,/data-shadow-status>Shadow 只读</);
    assert.equal(lpCount,0,html);
  } else {
    assert.equal((html.match(/data-shadow-status/g)||[]).length,0,html);
    assert.equal(lpCount,0,html);
  }
  if (process.env.EXPECTED_STATE !== 'shadow-paused') {
    assert.match(html,/LP order fact/);
    assert.match(html,/data-action="lp-order-entry"/);
    assert.match(html,/data-action="lp-augment-entry"/);
    assert.match(html,/data-lp-auto-action=/);
    if (process.env.EXPECTED_STATE === 'shadow' || process.env.EXPECTED_STATE === 'unknown') {
      assert.match(html,/data-action="lp-cancel-all"[^>]*disabled/);
      assert.match(html,/data-action="lp-cancel-order"[^>]*disabled/);
      assert.match(html,/data-action="lp-order-entry"[^>]*disabled/);
      assert.match(html,/data-action="lp-augment-entry"[^>]*disabled/);
      assert.match(html,/data-lp-auto-action="[^"]*"[^>]*disabled/);
    }
  }
  if (process.env.EXPECTED_STATE === 'unknown') {
    assert.match(html,/data-service-identity-status>服务身份 UNKNOWN</);
    vm.runInContext('state.predictionMarket.activeTab="multi_leg";',sandbox);
    vm.runInContext('renderPredictionMarket();',sandbox);
    await assert.rejects(vm.runInContext(
      'predictionPost("/api/prediction-arbitrage/mode",{mode:"auto"})',sandbox),
      /identity UNKNOWN/);
    assert.equal(requests.some(request=>request.method==='POST'),false);
  } else if (process.env.EXPECTED_STATE === 'shadow') {
    vm.runInContext('state.predictionMarket.activeTab="multi_leg";',sandbox);
    vm.runInContext('renderPredictionMarket();',sandbox);
    assert.match(nodes['prediction-market-root'].innerHTML,/Shadow 只读 · 写操作不可用/);
    await assert.rejects(vm.runInContext(
      'predictionPost("/api/prediction-arbitrage/mode",{mode:"auto"})',sandbox),
      /Shadow 只读/);
    assert.equal(requests.some(request=>request.method==='POST'),false);
  } else if (process.env.EXPECTED_STATE === 'production' || process.env.EXPECTED_STATE === 'legacy') {
    vm.runInContext('state.predictionMarket.activeTab="multi_leg";',sandbox);
    vm.runInContext('renderPredictionMarket();',sandbox);
    assert.match(nodes['prediction-market-root'].innerHTML,/data-action="set-mode"/);
    const result=await vm.runInContext(
      'predictionPost("/api/prediction-arbitrage/mode",{mode:"auto"})',sandbox);
    assert.deepEqual(result,{ok:true});
    assert.equal(requests.some(request=>request.method==='POST'),true);
  }
  process.exit(0);
},10);
'''
    result = subprocess.run(['node', '-e', script, str(js)], capture_output=True, text=True, env={
        **os.environ, 'SERVICE_MODE': service_mode, 'SERVICE_MUTATIONS': mutations,
        'N_LEG': n_leg, 'EXPECTED_STATE': expected_state,
        'BODY_PREDICTION_ONLY': body_prediction_only})
    assert result.returncode == 0, result.stderr


def test_known_prediction_identity_fails_closed_on_venues_error_then_recovers():
    js = Path(__file__).resolve().parents[1] / 'src/open_trader/dashboard_static/dashboard.js'
    script = r'''
const fs = require('fs'), vm = require('vm'), assert = require('assert');
class Element {
  constructor(){ this.dataset={}; this.hidden=false; this.innerHTML=''; this.style={};
    this.classList={toggle(){},add(){},remove(){}}; }
  addEventListener(){} setAttribute(){} removeAttribute(){}
  querySelector(){return null;} querySelectorAll(){return [];}
}
const nodes={}, requests=[];
let boot;
const body = new Element(); body.dataset.predictionOnly='true';
const document={body, addEventListener(name, fn){if(name==='DOMContentLoaded')boot=fn;},
  getElementById(id){return nodes[id] ||= new Element();}, querySelector(){return new Element();}};
const window={location:{search:'',pathname:'/',hash:''},setInterval(){return 1;},
  clearInterval(){},setTimeout(){return 1;},clearTimeout(){}};
const production=()=>({ok:true,json:async()=>({mode:'production',mutations:'enabled',
  csrf_token:'test-token',n_leg:{status:'running'}})});
let venuePhase=0;
const sandbox={document,window,console,URLSearchParams,AbortController,
  fetch:async (url,options={})=>{
    requests.push({url,method:options.method||'GET'});
    if (url.endsWith('/venues')) {
      venuePhase += 1;
      if (venuePhase === 2 || venuePhase === 3) {
        return {ok:false,status:503,json:async()=>({error:'unavailable'})};
      }
      return production();
    }
    if (url.endsWith('/lp/dashboard')) return {ok:true,json:async()=>({state:'ready'})};
    if ((options.method||'POST') === 'POST') return {ok:true,json:async()=>({ok:true})};
    return {ok:false,status:503,json:async()=>({error:'unavailable'})};
  }};
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),sandbox);
boot();
setTimeout(async()=>{
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(vm.runInContext('predictionServiceIdentity().state',sandbox),'production');
  vm.runInContext('state.predictionMarket.activeTab="multi_leg";',sandbox);
  await vm.runInContext('predictionPost("/api/prediction-arbitrage/mode",{mode:"auto"})',sandbox);
  assert.equal(requests.filter(request=>request.method==='POST').length,1);
  await vm.runInContext('fetchPredictionVenues()',sandbox);
  assert.equal(vm.runInContext('predictionServiceIdentity().state',sandbox),'unknown');
  assert.deepEqual(vm.runInContext('state.predictionMarket.venuesPayload',sandbox),
    {mode:'unknown',mutations:'unknown'});
  await assert.rejects(vm.runInContext(
    'predictionPost("/api/prediction-arbitrage/mode",{mode:"auto"})',sandbox),
    /identity UNKNOWN/);
  assert.equal(requests.filter(request=>request.method==='POST').length,1);

  await vm.runInContext('fetchPredictionVenues()',sandbox);
  assert.equal(vm.runInContext('predictionServiceIdentity().state',sandbox),'unknown');
  await assert.rejects(vm.runInContext(
    'predictionPost("/api/prediction-arbitrage/mode",{mode:"auto"})',sandbox),
    /identity UNKNOWN/);
  assert.equal(requests.filter(request=>request.method==='POST').length,1);

  await vm.runInContext('fetchPredictionVenues()',sandbox);
  assert.equal(vm.runInContext('predictionServiceIdentity().state',sandbox),'production');
  await vm.runInContext('predictionPost("/api/prediction-arbitrage/mode",{mode:"auto"})',sandbox);
  assert.equal(requests.filter(request=>request.method==='POST').length,2);
  process.exit(0);
},10);
'''
    result = subprocess.run(['node', '-e', script, str(js)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
