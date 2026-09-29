import subprocess
from pathlib import Path


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
