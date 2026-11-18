// Render-level smoke for the drill page with a mocked fetch and minimal DOM.
import {readFileSync} from 'node:fs';

const session = {token: 't', role: 'operator', email: 'operator@beiling.test'};
const storage = {'lsm-session': JSON.stringify(session)};
globalThis.localStorage = {getItem: k => storage[k] ?? null, setItem: (k, v) => storage[k] = v, removeItem: k => delete storage[k]};

const listeners = {};
function el(tag = 'div') {
  const node = {
    tag, innerHTML: '', textContent: '', hidden: false, dataset: {}, style: {},
    _children: [], _handler: {}, elements: {},
    classList: {add() {}, remove() {}, toggle() {}},
    appendChild(c) { this._children.push(c); },
    addEventListener(t, fn) { this._handler[t] = fn; },
    querySelector(sel) { return bind(el(sel)); },
    querySelectorAll(sel) { return this._sel = sel; return this._qall ?? []; },
    set onclick(fn) { this._handler.click = fn; }, get onclick() { return this._handler.click; },
    set onsubmit(fn) { this._handler.submit = fn; }, get onsubmit() { return this._handler.submit; },
  };
  return node;
}
function bind(node) {
  node.querySelectorAll = sel => {
    const found = (node._qallCache || []);
    return found;
  };
  return node;
}
const root = el();
globalThis.document = {querySelector: () => el(), createElement: () => el()};
globalThis.location = {hash: '#drill'};
globalThis.window = {addEventListener() {}, console, setTimeout, clearTimeout};
globalThis.URL = {createObjectURL: () => 'blob:x', revokeObjectURL() {}};
globalThis.Blob = class {};
globalThis.FormData = class {
  constructor() { this.m = {}; }
  append(k, v) { this.m[k] = v; }
};
// Object.fromEntries needs entries(); our page only does Object.fromEntries(new FormData(...))
globalThis.FormData.prototype.entries = function* () { for (const [k, v] of Object.entries(this.m)) yield [k, v]; };

const sessions = [{id: 1, name: '演练场A', description: 'd', dataset_id: 1, updated_at: 'now', transaction_count: 2, commit_seq: 7, segment_count: 1}];
const detail = {
  id: 1, name: '演练场A', description: 'd', dataset_id: 1, commit_seq: 7,
  live_keyspace: {'sensor-01': '11', 'sensor-04': '乙写入'},
  segments: [{id: 4, kind: 'compacted', records: {'sensor-01': {version: 1, value: '11', deleted: false}}}],
  events: [{seq: 0, event: 'session_init', detail: 'init'}, {seq: 7, event: 'commit', detail: 'T2 ok'}],
  transactions: {
    T1: {id: 'T1', label: '甲', state: 'active', snapshot: {'sensor-01': '11'}, read_ranges: [{lo: 'sensor-01', hi: 'sensor-03'}], read_predicates: {'sensor-01': 1}, staged: {'sensor-02': {op: 'put', value: '暂存', base_version: 2}}, result: null, started_seq: 5, ended_seq: null},
    T2: {id: 'T2', label: '乙', state: 'rolled_back', snapshot: {}, read_ranges: [{lo: 'sensor-05', hi: 'sensor-05'}], read_predicates: {'sensor-05': 5}, staged: {}, result: {ok: false, state: 'rolled_back', transaction_id: 'T2', conflict_keys: ['sensor-05'], commit_seq: 7, message: '冲突'}, started_seq: 6, ended_seq: 7},
  },
};
const datasets = [{id: 1, name: '批次1', operation_count: 5, distinct_keys: 5}];
globalThis.fetch = async (url, opts = {}) => {
  const path = url.replace('/api', '');
  const body = opts.body ? JSON.parse(opts.body) : {};
  let payload;
  if (path === '/drill/sessions' && !opts.method) payload = sessions;
  else if (path === '/drill/sessions/1') payload = detail;
  else if (path === '/datasets') payload = datasets;
  else if (path.endsWith('/begin')) payload = detail;
  else if (path.endsWith('/commit')) payload = {ok: true, state: 'committed', transaction_id: 'T1', conflict_keys: [], commit_seq: 8, message: 'ok', applied_writes: 1, session: detail};
  else payload = {...detail, session: detail};
  return {ok: true, status: 200, json: async () => payload};
};

const {drillPage} = await import('../src/pages/drill.js');

// list view
globalThis.location.hash = '#drill';
await drillPage(root);
if (!root.innerHTML.includes('事务演练') || !root.innerHTML.includes('演练场A')) throw new Error('list render failed');
console.log('ok  list page rendered');

// detail view with active + rolled-back txn
globalThis.location.hash = '#drill/1';
// emulate querySelectorAll returning real form nodes for binding
const forms = [el(), el()];
forms.forEach(f => { f.dataset = {}; f.querySelector = () => ({hidden: false, textContent: ''}); });
root.querySelectorAll = sel => {
  if (sel === 'form.drill-read') return forms;
  if (sel === 'form.drill-write') return forms;
  return [];
};
await drillPage(root);
const html = root.innerHTML;
for (const needle of ['T1', 'T2', '整笔退回', 'sensor-05', '进行中的事务', '物理合并', 'drill-commit', 'drill-read', '操作轨迹']) {
  if (!html.includes(needle)) throw new Error(`detail render missing ${needle}`);
}
console.log('ok  detail page rendered with active + rolled-back txn, conflict badge and forms');

// viewer branch: no write controls
storage['lsm-session'] = JSON.stringify({token: 't', role: 'viewer', email: 'viewer@beiling.test'});
await drillPage(root);
if (root.innerHTML.includes('drill-commit')) throw new Error('viewer must not see commit controls');
console.log('ok  viewer view is read-only');
console.log('\nFRONTEND RENDER SMOKE OK');
