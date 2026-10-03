// Controlled transport/body completion for the actual Dashboard request functions.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const { waitForPredictionPoll } = require(process.argv[5] || '../e2e/prediction-poll-barrier.cjs');
const [mode, lastResponse, fault] = process.argv.slice(2);
const split = mode === 'split';
const deferred = () => {
  let resolve, reject;
  const promise = new Promise((done, fail) => { resolve = done; reject = fail; });
  return { promise, resolve, reject };
};
const checkpoint = () => new Promise(resolve => setImmediate(resolve));
class Element {
  constructor() { this.dataset = {}; this.innerHTML = ''; this.style = {}; this.classList = { toggle() {}, add() {}, remove() {} }; }
  addEventListener() {} setAttribute() {} removeAttribute() {}
  querySelector() { return null; } querySelectorAll() { return []; }
}
const nodes = {};
const body = new Element();
body.dataset.predictionOnly = 'true';
body.dataset.predictionSplit = String(split);
const sandbox = {
  console, URLSearchParams, AbortController,
  document: { body, addEventListener() {}, getElementById(id) { return nodes[id] ||= new Element(); }, querySelector() { return null; } },
  window: { location: { search: '' }, setInterval() {}, clearInterval() {}, setTimeout() {}, clearTimeout() {} },
};
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync('src/open_trader/dashboard_static/dashboard.js', 'utf8'), sandbox);
vm.runInContext(`
  elements['prediction-market-root'] = document.getElementById('prediction-market-root');
  state.workspaceView = 'prediction_market';
  state.predictionMarket.activeTab = 'multi_leg';
  state.predictionMarket.nLegStatus = 'paused';
  const originalIdentity = fetchPredictionExecutionIdentity;
  fetchPredictionExecutionIdentity = () => globalThis.identityTask = originalIdentity();
`, sandbox);
if (fault === 'nleg-read') {
  vm.runInContext(`
    const originalUpdate = updatePredictionNLegStatus;
    updatePredictionNLegStatus = payload => {
      originalUpdate(payload);
      if (globalThis.injectRead) fetch('/api/prediction-arbitrage/state');
    };
  `, sandbox);
}

const requestWaiters = [];
const conditionWaiters = [];
const requests = [];
const nLegReads = [];
const pendingNLegReads = new Set();
const page = {
  waitForFunction(predicate, arg) {
    const done = deferred();
    conditionWaiters.push({ predicate, arg, done });
    checkConditions();
    return done.promise;
  },
  waitForRequest(predicate) {
    const done = deferred();
    requestWaiters.push({ predicate, done });
    return done.promise;
  },
};
function checkConditions() {
  for (const waiter of [...conditionWaiters]) {
    sandbox.predicateArg = waiter.arg;
    if (vm.runInContext(`(${waiter.predicate.toString()})(predicateArg)`, sandbox)) {
      conditionWaiters.splice(conditionWaiters.indexOf(waiter), 1);
      waiter.done.resolve();
    }
  }
}
sandbox.fetch = async url => {
  if (url === '/api/prediction-arbitrage/state') {
    nLegReads.push(url);
    pendingNLegReads.add(url);
    pendingNLegReads.delete(url);
    return { ok: true, json: async () => ({}) };
  }
  assert(['/api/prediction-arbitrage/venues', '/api/prediction-arbitrage/execution/identity'].includes(url), url);
  const headers = deferred(), bodyRead = deferred(), finished = deferred();
  const kind = url.endsWith('/venues') ? 'venues' : 'identity';
  const payload = { mode: split && kind === 'venues' ? 'shadow' : 'production',
    mutations: split && kind === 'venues' ? 'prohibited' : 'enabled', n_leg: { status: 'paused' }, venues: [] };
  const response = { ok: () => true, finished: () => finished.promise };
  const request = {
    kind, url: () => `http://fixture.invalid${url}`, response: () => headers.promise,
    headers() { headers.resolve(response); },
    finish() { finished.resolve(null); },
    apply() {
      if (fault === 'invalid-json' && sandbox.injectRead && (kind === 'identity' || !split)) {
        bodyRead.reject(new SyntaxError('fixture malformed identity body'));
      } else bodyRead.resolve(payload);
    },
  };
  requests.push(request);
  for (const waiter of [...requestWaiters]) {
    if (waiter.predicate(request)) {
      requestWaiters.splice(requestWaiters.indexOf(waiter), 1);
      waiter.done.resolve(request);
    }
  }
  await headers.promise;
  return { ok: true, json: () => bodyRead.promise };
};
function beginCycle() {
  const offset = requests.length;
  const venuesTask = vm.runInContext('fetchPredictionVenues()', sandbox);
  return { requests: requests.slice(offset), tasks: { venues: venuesTask, identity: sandbox.identityTask } };
}
async function apply(cycle, request) {
  request.apply();
  await cycle.tasks[request.kind];
  checkConditions();
  await checkpoint();
}
const watchdog = setTimeout(() => {
  console.error('fixture watchdog: polling barrier never completed');
  process.exit(2);
}, 5000);
(async () => {
  // Tab-triggered Air identity must drain before next-poll observers are armed.
  const initial = beginCycle();
  let completed = false;
  const barrier = waitForPredictionPoll(page, split).then(() => { completed = true; });
  const venues = initial.requests.find(request => request.kind === 'venues');
  venues.headers(); venues.finish();
  await apply(initial, venues);
  if (split) {
    assert.equal(requestWaiters.length, 0, 'poll observers armed before initial Air identity applied');
    const identity = initial.requests.find(request => request.kind === 'identity');
    identity.headers(); identity.finish();
    await apply(initial, identity);
  }
  await checkpoint();
  assert.equal(requestWaiters.length, split ? 2 : 1, 'each next-poll request needs an observer');
  sandbox.injectRead = true;
  const poll = beginCycle();
  const held = poll.requests.find(request => request.kind === lastResponse) || poll.requests[0];
  for (const request of poll.requests.filter(request => request !== held)) {
    request.headers(); request.finish();
    await apply(poll, request);
  }
  await checkpoint();
  assert.equal(completed, false, 'barrier completed before the last response');
  held.headers();
  await checkpoint();
  assert.equal(completed, false, 'response headers are not completion');
  held.finish();
  await checkpoint();
  assert.equal(completed, false, 'transport completion is not Dashboard application');
  await apply(poll, held);
  await barrier;
  assert.equal(vm.runInContext('state.predictionMarket.nLegStatus', sandbox), 'paused', 'paused N-leg state was not applied');
  assert.match(nodes['prediction-market-root'].innerHTML, /多腿套利已暂停/);
  assert.deepEqual(nLegReads, [], 'paused N-leg emitted a read');
  assert.equal(pendingNLegReads.size, 0);
  console.log('poll barrier applied');
})().catch(error => { console.error(error); process.exitCode = 1; }).finally(() => clearTimeout(watchdog));
