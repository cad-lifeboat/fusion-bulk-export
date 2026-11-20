// Tree logic of FusionBulkExport/ui/tree.js; run with node (tests/test_tree_js.py does).
const assert = require('assert');
const t = require('../FusionBulkExport/ui/tree.js');

const roots = t.build([{ id: 'h', name: 'Hub', projects: [{ id: 'p1', name: 'P1' }, { id: 'p2', name: 'P2' }] }]);
const [p1, p2] = roots[0].children;
assert.deepStrictEqual(t.selection(roots), {});

// Folders load on demand; ticking one makes its project partial and selects just that path.
t.addFolders(p1, ['Top', 'Other']);
const [top, other] = p1.children;
t.addFolders(top, ['Sub']);
t.toggle(top);
assert.strictEqual(p1.state, t.SOME);
assert.strictEqual(roots[0].state, t.SOME);
assert.strictEqual(top.children[0].state, t.ON);
assert.deepStrictEqual(t.selection(roots), { p1: [['Top']] });

// Nested folder only: the path goes deep, and Top stays partial (its own files are not ticked).
t.toggle(top);
t.toggle(top.children[0]);
assert.strictEqual(top.state, t.SOME);
assert.deepStrictEqual(t.selection(roots), { p1: [['Top', 'Sub']] });

// Every folder ticked still leaves the project's own files out: it counts as whole only when ticked.
t.toggle(other);
assert.strictEqual(p1.state, t.SOME);
assert.deepStrictEqual(t.selection(roots), { p1: [['Top', 'Sub'], ['Other']] });
t.toggle(p1);
assert.deepStrictEqual(t.selection(roots), { p1: null });

// A partly ticked hub becomes fully ticked when clicked; clicking again clears everything.
assert.strictEqual(roots[0].state, t.SOME);
t.toggle(roots[0]);
assert.strictEqual(p2.state, t.ON);
assert.deepStrictEqual(t.selection(roots), { p1: null, p2: null });
t.toggle(roots[0]);
assert.deepStrictEqual(t.selection(roots), {});

// Folders opened under a ticked project arrive ticked.
t.toggle(p2);
t.addFolders(p2, ['A']);
assert.strictEqual(p2.children[0].state, t.ON);
console.log('tree logic ok');

// Own files: ticking every subfolder does not tick the parent's own files.
{
  const r = t.build([{ id: 'h', name: 'Hub', projects: [{ id: 'p', name: 'P' }] }]);
  const p = r[0].children[0];
  t.addFolders(p, ['Only']);
  t.toggle(p.children[0]);
  assert.strictEqual(p.state, t.SOME);
  assert.deepStrictEqual(t.selection(r), { p: [['Only']] });
  // Unticking one folder of a fully ticked project keeps the project's own files.
  t.toggle(p);           // SOME -> ON (whole)
  t.addFolders(p.children[0], []);
  t.toggle(p.children[0]);
  assert.strictEqual(p.state, t.SOME);
  assert.deepStrictEqual(t.selection(r), { p: [['']] });
}
console.log('own files ok');

// A failed "folders" request is an error with Retry, never "No folders"; Retry then works.
{
  const r = t.build([{ id: 'h', name: 'Hub', projects: [{ id: 'p', name: 'P' }] }]);
  const p = r[0].children[0];
  t.startLoading(p);
  assert.strictEqual(p.loading, true);
  assert.strictEqual(p.open, true);
  t.applyFolders(p, { error: 'Network down' });
  assert.strictEqual(p.loading, false);
  assert.strictEqual(p.error, 'Network down');
  assert.strictEqual(p.children, null);
  t.applyFolders(p, null);  // no answer at all
  assert.strictEqual(p.error, 'No answer from Fusion.');
  t.applyFolders(p, 'garbage');
  assert.ok(p.error);
  t.startLoading(p);       // Retry
  assert.strictEqual(p.error, null);
  t.applyFolders(p, { folders: [{ id: 'f1', name: 'Twin' }, { id: 'f2', name: 'Twin' }], files: 7 });
  assert.strictEqual(p.error, null);
  assert.strictEqual(p.files, 7);
  assert.deepStrictEqual(p.children.map(function (c) { return c.ids; }), [['f1'], ['f2']]);
  // Folders with ids are sent by id: the second twin alone, not both.
  t.toggle(p.children[1]);
  assert.deepStrictEqual(t.selection(r), { p: [[{ id: 'f2', name: 'Twin' }]] });
  t.applyFolders(p.children[1], { folders: [{ id: 'f3', name: 'In' }, 'Old'], files: 1 });
  t.toggle(p.children[1]);
  t.toggle(p.children[1].children[1]);
  // A folder without an id (older answer) is addressed by name below its parent's id.
  assert.deepStrictEqual(t.selection(r), { p: [[{ id: 'f2', name: 'Twin' }, 'Old']] });
  t.toggle(p.children[1].children[1]);
  t.applyFolders(p.children[0], { folders: [], files: 0 });
  assert.deepStrictEqual(p.children[0].children, []);
}
console.log('errors and retry ok');

// Filter over project names; hubs without a match disappear.
{
  const r = t.build([{ id: 'h1', name: 'One', projects: [{ id: 'a', name: 'Gearbox' }, { id: 'b', name: 'Clamp' }] },
                     { id: 'h2', name: 'Two', projects: [{ id: 'c', name: 'Bracket' }] }]);
  r.forEach(function (h) { h.open = true; });
  const names = function (f) { return t.visibleRows(r, f).map(function (x) { return x.node.name; }); };
  assert.deepStrictEqual(names(''), ['One', 'Gearbox', 'Clamp', 'Two', 'Bracket']);
  assert.deepStrictEqual(names('GEAR'), ['One', 'Gearbox']);
  assert.deepStrictEqual(names('  cla '), ['One', 'Clamp']);
  assert.deepStrictEqual(names('zzz'), []);
  // Opened folders appear under their project, one level deeper.
  t.addFolders(r[0].children[0], ['Sub']);
  r[0].children[0].open = true;
  const rows = t.visibleRows(r, 'gear');
  assert.deepStrictEqual(rows.map(function (x) { return [x.node.name, x.depth]; }),
                         [['One', 0], ['Gearbox', 1], ['Sub', 2]]);
}
console.log('filter ok');

// Keyboard: arrows move and open/close, space ticks, enter opens, left goes to the parent.
{
  const r = t.build([{ id: 'h', name: 'Hub', projects: [{ id: 'p', name: 'P' }, { id: 'q', name: 'Q' }] }]);
  r[0].open = true;
  let rows = t.visibleRows(r, '');
  assert.deepStrictEqual(t.keyAction(rows, 0, 'ArrowDown'), { focus: 1, action: null });
  assert.deepStrictEqual(t.keyAction(rows, 2, 'ArrowDown'), { focus: 2, action: null });
  assert.deepStrictEqual(t.keyAction(rows, 0, 'ArrowUp'), { focus: 0, action: null });
  assert.deepStrictEqual(t.keyAction(rows, 1, ' '), { focus: 1, action: 'toggle' });
  assert.deepStrictEqual(t.keyAction(rows, 1, 'Enter'), { focus: 1, action: 'open' });
  assert.deepStrictEqual(t.keyAction(rows, 1, 'ArrowRight'), { focus: 1, action: 'open' });
  assert.deepStrictEqual(t.keyAction(rows, 1, 'ArrowLeft'), { focus: 0, action: null });
  assert.deepStrictEqual(t.keyAction(rows, 0, 'ArrowLeft'), { focus: 0, action: 'close' });
  assert.deepStrictEqual(t.keyAction(rows, 1, 'End'), { focus: 2, action: null });
  const p = r[0].children[0];
  t.addFolders(p, ['A']);
  p.open = true;
  rows = t.visibleRows(r, '');
  assert.deepStrictEqual(t.keyAction(rows, 1, 'ArrowRight'), { focus: 2, action: null });
  assert.deepStrictEqual(t.keyAction(rows, 1, 'Enter'), { focus: 1, action: 'close' });
  t.addFolders(p.children[0], []);  // a folder known to have no subfolders cannot open
  assert.deepStrictEqual(t.keyAction(rows, 2, 'Enter'), { focus: 2, action: null });
  assert.deepStrictEqual(t.keyAction(rows, 2, 'ArrowLeft'), { focus: 1, action: null });
}
console.log('keyboard ok');

// Projects already in the output folder are marked, and the marks follow the folder.
{
  const r = t.build([{ id: 'h', name: 'Hub', projects: [{ id: 'p', name: 'P', exported: 3 }, { id: 'q', name: 'Q' }] }]);
  assert.deepStrictEqual(r[0].children.map(function (p) { return p.exported; }), [3, 0]);
  t.markExported(r, { q: 2 });
  assert.deepStrictEqual(r[0].children.map(function (p) { return p.exported; }), [0, 2]);
  t.markExported(r, null);
  assert.deepStrictEqual(r[0].children.map(function (p) { return p.exported; }), [0, 0]);
  assert.strictEqual(t.BRIDGE_TIMEOUT_MS, 5000);
}
console.log('exported marks ok');

// Page wiring smoke test with a minimal stand-in for the browser and Fusion's bridge: the page
// loads hubs, opens a project, shows an error row with Retry, retries, ticks by keyboard.
{
  const fs = require('fs');
  const vm = require('vm');
  const path = require('path');
  function El(tag) {
    this.tagName = tag; this.children = []; this.attrs = {}; this.style = {}; this.listeners = {};
    this.className = ''; this._text = '';
  }
  El.prototype.appendChild = function (c) { this.children.push(c); c.parentNode = this; return c; };
  El.prototype.append = function () { for (let i = 0; i < arguments.length; i++) this.appendChild(arguments[i]); };
  El.prototype.setAttribute = function (k, v) { this.attrs[k] = String(v); };
  El.prototype.getAttribute = function (k) { return this.attrs[k]; };
  El.prototype.addEventListener = function (k, f) { this.listeners[k] = f; };
  El.prototype.focus = function () { doc.activeElement = this; };
  Object.defineProperty(El.prototype, 'textContent', {
    get: function () { return this._text + this.children.map(function (c) { return c.textContent; }).join(''); },
    set: function (v) { this._text = String(v); this.children = []; } });
  El.prototype.all = function (out) {
    out = out || [];
    this.children.forEach(function (c) { out.push(c); c.all(out); });
    return out;
  };
  El.prototype.querySelectorAll = function (sel) {
    return this.all().filter(function (e) { return sel === '[role="treeitem"]' && e.attrs.role === 'treeitem'; });
  };
  const tree = new El('ul'), filterBox = new El('input');
  const doc = { createElement: function (t) { return new El(t); },
                getElementById: function (id) { return id === 'tree' ? tree : filterBox; } };
  let failNext = true;
  const requests = [];
  const bridge = { fusionSendData: function (action, data) {
    requests.push([action, JSON.parse(data)]);
    if (action === 'hubs') return JSON.stringify({ hubs: [{ id: 'h', name: 'Hub', projects: [{ id: 'p', name: 'P', exported: 2 }] }] });
    if (action === 'folders') {
      if (failNext) { failNext = false; return JSON.stringify({ error: 'Offline' }); }
      return Promise.resolve(JSON.stringify({ folders: [{ id: 'f', name: 'Parts' }], files: 4 }));
    }
    return JSON.stringify({ ok: true });
  } };
  const timers = [];
  const win = { addEventListener: function (k, f) { this.onload = f; } };
  const context = { document: doc, window: win, adsk: bridge, setTimeout: function (f) { timers.push(f); },
                    Date: Date, Promise: Promise, JSON: JSON, String: String, Array: Array, Math: Math, Error: Error };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '..', 'FusionBulkExport', 'ui', 'tree.js'), 'utf8'), context);
  win.onload();
  const text = function () { return tree.textContent; };
  const items = function () { return tree.querySelectorAll('[role="treeitem"]'); };
  const key = function (k) { tree.listeners.keydown({ key: k, preventDefault: function () {} }); };
  const settle = function () { return new Promise(function (r) { setImmediate(r); }); };
  (async function () {
    await settle();
    assert.ok(text().indexOf('2 exported') >= 0, text());
    assert.strictEqual(items().length, 2);
    assert.strictEqual(items()[0].attrs['aria-level'], '1');
    assert.strictEqual(items()[0].tabIndex, 0);
    key('ArrowDown');
    key('Enter');           // open P: the request fails
    await settle();
    assert.ok(text().indexOf('Could not list the folders: Offline') >= 0, text());
    assert.ok(text().indexOf('No folders') < 0);
    const retry = tree.all().filter(function (e) { return e.className === 'retry'; })[0];
    retry.onclick({ stopPropagation: function () {} });
    await settle(); await settle();
    assert.ok(text().indexOf('Parts') >= 0, text());
    assert.ok(text().indexOf('4 files') >= 0, text());
    const last = requests.filter(function (r) { return r[0] === 'folders'; }).pop();
    assert.deepStrictEqual(last[1], { project: 'p', path: [], ids: [] });
    key('ArrowRight');      // to Parts
    key(' ');               // tick it
    const sent = requests.filter(function (r) { return r[0] === 'selection'; }).pop();
    assert.deepStrictEqual(sent[1], { p: [[{ id: 'f', name: 'Parts' }]] });
    filterBox.listeners.input({ target: { value: 'zzz' } });
    assert.ok(text().indexOf('No project name contains') >= 0);
    context.window.fusionJavaScriptHandler.handle('exported', '{}');
    console.log('page wiring ok');
  })().catch(function (error) { console.error(error); process.exit(1); });
}
